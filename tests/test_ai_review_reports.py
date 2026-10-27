"""Offline report provenance checks using independent, temporary review fixtures."""

import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import Config
from evaluation.cli import main
from evaluation.control import PersistentRequestBudget, RunControl
from evaluation.data import DATASET_FIELDS, EvaluationSample, load_dataset
from evaluation.metrics import compute_metrics
from evaluation.reports import (
    CONTROL_METADATA_KEYS, CONTROL_PREDICTION_KEYS, load_report, metrics_json,
    predictions_csv, report_markdown, save_report, validate_report,
)
from evaluation.review import save_ai_review, save_review
from evaluation.runner import compare_reports, run_evaluation
from tests.helpers import fake_client


def sample_rows(statuses):
    return [EvaluationSample(
        sample_id=f"dev-{number:03d}", text=f"独立报告测试反馈第 {number} 项。",
        gold_sentiment="neutral", gold_category="other", gold_priority="low",
        split="dev", review_status=status,
    ) for number, status in enumerate(statuses, 1)]


class AIReviewReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix=".ai-report-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_patch = patch.object(Config, "API_MODE", "responses")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def control(self, maximum=180):
        return RunControl(maximum, budget=PersistentRequestBudget(self.root / "budget.json"))

    def completed_fixture(self):
        """Create real review events on a new fixture, never the user's dataset."""
        source, evidence = self.root / "feedback.csv", self.root / "review.json"
        rows = [EvaluationSample(
            sample_id=f"{split}-{number:03d}",
            text=f"独立门禁测试 {split} 第 {number} 条反馈。",
            gold_sentiment="neutral", gold_category="other", gold_priority="low",
            split=split, review_status="draft",
        ).model_dump(mode="json") for split, count in (("dev", 40), ("test", 20))
            for number in range(1, count + 1)]
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=DATASET_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        labels = {row["sample_id"]: {key: row[key] for key in
                  ("gold_sentiment", "gold_category", "gold_priority")} for row in rows}
        human_ids = [f"dev-{number:03d}" for number in range(1, 6)]
        save_review(source, evidence, updates={key: labels[key] for key in human_ids},
                    reviewer="test human fixture", reasons="测试人工确认流程，原文无实质问题。",
                    confirm_ids=human_ids, human_confirmed=True)
        ai_labels = {key: value for key, value in labels.items() if key not in human_ids}
        result = save_ai_review(source, evidence, updates=ai_labels,
                                reasons={key: "独立测试原文无情绪、无具体产品对象、无影响，保留中性其他低。"
                                         for key in ai_labels})
        return source, evidence, load_dataset(source), result["ledger"]

    def test_completed_ai_reports_roundtrip_and_export_distinct_provenance(self):
        rows = sample_rows(["reviewed", "ai_reviewed", "ai_reviewed"])
        with patch("evaluation.runner.get_openai_client") as factory, patch("evaluation.runner.analyze_feedback") as analyzer:
            report = run_evaluation(rows)
            paths = save_report(report, self.root)
        factory.assert_not_called()
        analyzer.assert_not_called()
        self.assertEqual(load_report(paths["report"]), report)
        metadata = report["metadata"]
        self.assertEqual(metadata["review_status_counts"], {"draft": 0, "reviewed": 1, "ai_reviewed": 2})
        self.assertTrue(metadata["all_labels_reviewed"])
        self.assertEqual(metadata["result_status"], "ai_assisted_reference")
        self.assertEqual(metadata["review_provenance"], "ai_assisted")
        self.assertIn("并非全人工确认", metadata["interpretation"])
        self.assertIn("不能作为业务准确率", metadata["interpretation"])
        self.assertEqual(json.loads(metrics_json(report))["metadata"]["review_status_counts"]["ai_reviewed"], 2)
        exported = list(csv.DictReader(io.StringIO(predictions_csv(report).decode("utf-8-sig"))))
        self.assertEqual([row["review_status"] for row in exported], ["reviewed", "ai_reviewed", "ai_reviewed"])
        markdown = report_markdown(report)
        self.assertIn("ai_assisted", markdown)
        self.assertIn("并非全人工确认", markdown)

    def test_remaining_drafts_keep_ai_counts_and_provisional_result(self):
        report = run_evaluation(sample_rows(["draft", "ai_reviewed", "reviewed"]))
        self.assertEqual(report["metadata"]["review_status_counts"], {"draft": 1, "reviewed": 1, "ai_reviewed": 1})
        self.assertFalse(report["metadata"]["all_labels_reviewed"])
        self.assertEqual(report["metadata"]["result_status"], "provisional")
        self.assertIn("并非全人工确认", report["metadata"]["interpretation"])
        self.assertEqual(validate_report(report), report)

    def test_no_ai_preserves_historical_metadata_shape_and_status(self):
        for statuses, expected in ((["reviewed"] * 3, "reviewed_reference"), (["draft"] * 3, "provisional")):
            with self.subTest(statuses=statuses):
                report = run_evaluation(sample_rows(statuses))
                self.assertEqual(set(report["metadata"]["review_status_counts"]), {"draft", "reviewed"})
                self.assertNotIn("review_provenance", report["metadata"])
                self.assertEqual(report["metadata"]["result_status"], expected)
                self.assertEqual(validate_report(report), report)

    def test_ai_outside_selected_rows_does_not_change_selected_provenance(self):
        report = run_evaluation(sample_rows(["reviewed", "ai_reviewed", "draft"]), limit=1)
        self.assertEqual(report["metadata"]["review_status_counts"], {"draft": 0, "reviewed": 1})
        self.assertEqual(report["metadata"]["result_status"], "reviewed_reference")
        self.assertNotIn("review_provenance", report["metadata"])
        validate_report(report)

    def test_validator_rejects_hidden_ai_counts_and_all_human_claims(self):
        original = run_evaluation(sample_rows(["reviewed", "ai_reviewed", "ai_reviewed"]))
        mutations = {
            "missing_ai_count": lambda meta: meta.update(review_status_counts={"draft": 0, "reviewed": 3}),
            "incorrect_ai_count": lambda meta: meta["review_status_counts"].update(ai_reviewed=1),
            "boolean_count": lambda meta: meta["review_status_counts"].update(reviewed=True),
            "unknown_count": lambda meta: meta["review_status_counts"].update(automated=2),
            "human_result": lambda meta: meta.update(result_status="reviewed_reference"),
            "wrong_completion": lambda meta: meta.update(all_labels_reviewed=False),
            "human_provenance": lambda meta: meta.update(review_provenance="human"),
            "missing_provenance": lambda meta: meta.pop("review_provenance"),
            "missing_notice": lambda meta: meta.update(interpretation="DEMO 仅验证评测流程，预测及耗时均为 Mock；不代表模型效果或性能。"),
        }
        for name, mutate in mutations.items():
            changed = copy.deepcopy(original)
            mutate(changed["metadata"])
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_report(changed)
        human = run_evaluation(sample_rows(["reviewed"] * 3))
        human["metadata"]["review_provenance"] = "ai_assisted"
        with self.assertRaises(ValueError):
            validate_report(human)

    def test_schema_one_report_validates_ai_provenance_and_legacy_human_shape(self):
        for statuses in (["reviewed", "ai_reviewed", "ai_reviewed"], ["reviewed"] * 3):
            report = run_evaluation(sample_rows(statuses))
            report["schema_version"] = 1
            for key in CONTROL_METADATA_KEYS:
                report["metadata"].pop(key)
            for row in report["predictions"]:
                for key in CONTROL_PREDICTION_KEYS:
                    row.pop(key)
            report["errors"] = [copy.deepcopy(row) for row in report["predictions"] if row["error_fields"]]
            report["metrics"] = compute_metrics(report["predictions"], mode="demo")
            self.assertEqual(validate_report(report), report)

    def test_live_ai_report_and_budget_stop_keep_label_and_execution_meanings(self):
        client = fake_client()
        rows = sample_rows(["reviewed", "ai_reviewed", "ai_reviewed"])
        control = self.control(1)
        report = run_evaluation(rows, mode="live", client=client, control=control,
                                max_workers=1, journal_dir=self.root / "journals")
        self.assertEqual(client.responses.parse.call_count, 1)
        self.assertEqual(report["metadata"]["request_attempts_actual"], 1)
        self.assertEqual(report["metadata"]["cumulative_request_attempts"], 1)
        self.assertEqual(report["metadata"]["stop_reason"], "request_limit")
        self.assertEqual(report["metadata"]["run_status"], "incomplete")
        self.assertTrue(report["metadata"]["all_labels_reviewed"])
        self.assertEqual(report["metadata"]["result_status"], "ai_assisted_reference")
        self.assertEqual([row["status"] for row in report["predictions"]], ["success", "not_executed", "not_executed"])
        self.assertEqual(report["metrics"]["sample_count"], 1)
        self.assertEqual(report["metrics"]["failed_count"], 0)
        self.assertEqual(report["metrics"]["not_executed_count"], 2)
        self.assertFalse(report["metrics"]["assessment_complete"])
        self.assertIn("运行未完成", report["metadata"]["interpretation"])
        self.assertIn("并非全人工确认", report["metadata"]["interpretation"])
        paths = save_report(report, self.root)
        self.assertEqual(load_report(paths["report"]), report)
        with self.assertRaises(ValueError):
            compare_reports([report, report])

    def test_full_dev_gate_accepts_mixed_saved_evidence_and_rejects_forgery(self):
        source, evidence_path, rows, evidence = self.completed_fixture()
        client = fake_client()
        with patch("evaluation.runner.get_openai_client") as factory:
            report = run_evaluation(rows, mode="live", client=client, control=self.control(),
                                    journal_dir=self.root / "journals", review_ledger=evidence)
        factory.assert_not_called()
        self.assertEqual(client.responses.parse.call_count, 40)
        self.assertEqual(report["metadata"]["review_status_counts"], {"draft": 0, "reviewed": 5, "ai_reviewed": 35})
        self.assertEqual(report["metadata"]["result_status"], "ai_assisted_reference")
        self.assertEqual(validate_report(report), report)
        forged = copy.deepcopy(evidence)
        forged["records"][5]["review_status"] = "reviewed"
        with patch("evaluation.runner.get_openai_client") as factory, patch("evaluation.runner.analyze_feedback") as analyzer:
            for ledger in (None, forged):
                with self.subTest(ledger="missing" if ledger is None else "forged"), self.assertRaises(ValueError):
                    run_evaluation(rows, mode="live", review_ledger=ledger)
            labels = evidence["records"][-1]["current_labels"]
            reopened = save_review(source, evidence_path, updates={"test-020": labels},
                                   reviewer="test fixture", reasons="重新打开草稿以测试全量门禁。")
            with self.assertRaisesRegex(ValueError, "尚未完成复核"):
                run_evaluation(load_dataset(source), mode="live", review_ledger=reopened["ledger"])
        factory.assert_not_called()
        analyzer.assert_not_called()

    def test_comparison_and_validate_cli_disclose_ai_review(self):
        rows = sample_rows(["reviewed", "ai_reviewed", "ai_reviewed"])
        comparison = compare_reports([run_evaluation(rows, prompt_version=version) for version in ("v1", "v2")])
        self.assertEqual(comparison["review_provenance"], "ai_assisted")
        self.assertIn("并非全人工确认", comparison["interpretation"])
        for statuses, counts in ((["reviewed", "ai_reviewed", "ai_reviewed"], {"draft": 0, "reviewed": 1, "ai_reviewed": 2}),
                                 (["draft"] * 3, {"draft": 3, "reviewed": 0})):
            with patch("evaluation.cli.load_dataset", return_value=sample_rows(statuses)), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(["validate"]), 0)
                summary = json.loads(output.getvalue())
            self.assertEqual(summary["review_status_counts"], counts)
            if "ai_reviewed" in counts:
                self.assertIn("并非全人工确认", summary["message"])


if __name__ == "__main__":
    unittest.main()
