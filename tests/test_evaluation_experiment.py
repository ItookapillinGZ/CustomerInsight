"""Entirely offline experiment fixtures; no production human review or requests."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from openai import APIStatusError

from ai.prompts import PROMPT_REGISTRY, available_prompt_versions, get_prompt, prompt_hash, register_prompt
from core.config import Config
from evaluation.control import PersistentRequestBudget, RunControl
from evaluation.data import DATASET_FIELDS, dataset_hash, load_dataset
from evaluation.experiment import (
    SELECTION_RULE, compare_development_reports, comparison_markdown,
    freeze_experiment_manifest, save_development_comparison, save_test_summary,
    summarize_test_reports, validate_experiment_manifest, validate_test_report,
)
from evaluation.review import load_review_ledger, save_review
from evaluation.runner import run_evaluation
from tests.helpers import offline_temp_root, write_draft_dataset


class EvaluationExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.dataset = self.directory / "feedback.csv"
        self.review = self.directory / "feedback.review.json"
        write_draft_dataset(self.dataset)
        samples = load_dataset(self.dataset)
        # A UI confirmation is simulated only in an isolated test fixture.
        # This must never touch evaluation/data or its actual review ledger.
        save_review(
            self.dataset, self.review,
            updates={sample.sample_id: {key: sample.model_dump(mode="json")[key] for key in DATASET_FIELDS if key.startswith("gold_")} for sample in samples},
            reviewer="离线测试夹具（不是用户实际复核）", reasons="仅测试显式确认流程。",
            confirm_ids=[sample.sample_id for sample in samples], human_confirmed=True,
        )
        self.samples = load_dataset(self.dataset)
        self.run_counter = 0
        self.current_manifest = None

    def report(self, version="v1", *, split="dev", changes=None, failure=None, **kwargs):
        """Go through actual Pydantic analysis/control using a local SDK double."""
        if split == "test" and self.current_manifest is None:
            self.current_manifest = freeze_experiment_manifest(
                [self.report("v1", changes={"dev-001": {"category": "other"}}), self.report("v2")],
                dataset_path=self.dataset, review_path=self.review,
                output_path=self.directory / "fixture_manifest.json",
            )
        self.run_counter += 1
        by_text = {sample.text: sample for sample in self.samples}
        client = MagicMock(name="offline_experiment_sdk")

        def response(**parameters):
            content = parameters["input"][-1]["content"]
            text = content.split("<feedback>", 1)[1].split("</feedback>", 1)[0].strip()
            sample = by_text[text]
            if failure and sample.sample_id == failure[0]:
                raise failure[1]
            payload = {
                "sentiment": sample.gold_sentiment.value,
                "category": sample.gold_category.value,
                "priority": sample.gold_priority.value,
                "summary": "离线实验测试响应。", "actionable_items": [],
            }
            payload.update((changes or {}).get(sample.sample_id, {}))
            return SimpleNamespace(output_parsed=payload, status="completed", usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

        client.responses.parse.side_effect = response
        max_attempts = kwargs.pop("max_attempts", 180)
        control = RunControl(max_attempts=max_attempts, budget=PersistentRequestBudget(self.directory / f"budget_{self.run_counter}.json"))
        with patch.object(Config, "API_MODE", "responses"):
            report = run_evaluation(self.samples, prompt_version=version, split=split, mode="live", max_workers=2, max_retries=0, control=control, client=client, journal_dir=self.directory / "journals", review_ledger=load_review_ledger(self.dataset, self.review), experiment_manifest=self.current_manifest if split == "test" else None, **kwargs)
        return report

    def freeze(self, reports):
        path = self.directory / "experiment.json"
        frozen = freeze_experiment_manifest(reports, dataset_path=self.dataset, review_path=self.review, output_path=path)
        self.current_manifest = frozen
        return frozen, path

    def test_equal_primary_retains_v1_and_exports_full_support_and_evidence(self):
        a, b = self.report("v1"), self.report("v2")
        comparison = compare_development_reports([b, a], self.samples)
        self.assertEqual(comparison["candidate_version"], "v1")
        self.assertEqual(comparison["selection_rule"], SELECTION_RULE)
        self.assertEqual(len(comparison["sample_differences"]), 40)
        self.assertEqual(comparison["error_analysis"]["representative_cases"], [])
        self.assertEqual(comparison["error_analysis"]["repeated_development_patterns"], [])
        self.assertEqual(sum(comparison["scores"][0]["support"]["category"].values()), 40)
        paths = save_development_comparison(comparison, self.directory / "comparison.json")
        self.assertEqual(json.loads(paths["comparison"].read_text(encoding="utf-8")), comparison)
        self.assertIn("support", paths["markdown"].read_text(encoding="utf-8"))
        self.assertIn("不编造案例", comparison_markdown(comparison))

    def test_primary_improvement_selects_v2_and_reports_sample_improvement(self):
        a = self.report("v1", changes={"dev-001": {"category": "other"}, "dev-002": {"category": "other"}})
        b = self.report("v2")
        result = compare_development_reports([a, b], self.samples)
        self.assertEqual(result["candidate_version"], "v2")
        self.assertEqual(result["change_counts"]["v2"]["category_improved"], 2)
        self.assertEqual(result["change_counts"]["v2"]["improved"], 2)
        patterns = result["error_analysis"]["repeated_development_patterns"]
        self.assertEqual(patterns[0]["type"], "category_confusion")
        self.assertEqual(patterns[0]["count"], 2)
        self.assertEqual(result["error_analysis"]["representative_cases"][0]["sample_id"], "dev-001")
        self.assertIn("hypothesis_only", result["error_analysis"]["representative_cases"][0]["reason_status"])

    def test_secondary_guard_prevents_selection_when_sentiment_degrades(self):
        a = self.report("v1", changes={"dev-001": {"category": "other"}})
        b = self.report("v2", changes={"dev-001": {"sentiment": "positive"}})
        result = compare_development_reports([a, b], self.samples)
        self.assertEqual(result["candidate_version"], "v1")
        check = next(row for row in result["selection_checks"] if row["prompt_version"] == "v2")
        self.assertTrue(check["category_strictly_better"])
        self.assertFalse(check["guards"]["sentiment_macro_f1"])

    def test_priority_direction_structure_and_service_failures_have_observed_evidence(self):
        request = httpx.Request("POST", "https://offline.invalid")
        failure = APIStatusError("offline service failure", response=httpx.Response(503, request=request), body=None)
        a = self.report("v1", changes={"dev-003": {"priority": "high"}, "dev-001": {"priority": "low"}, "dev-004": {"summary": ""}})
        b = self.report("v2", failure=("dev-002", failure))
        result = compare_development_reports([a, b], self.samples)
        observed = {row["type"] for row in result["error_analysis"]["error_counts"]}
        self.assertTrue({"priority_too_high", "priority_too_low", "structure_failure", "service_failure"} <= observed)
        cases = result["error_analysis"]["representative_cases"]
        self.assertLessEqual(len(cases), 10)
        self.assertTrue(all(case["text"] for case in cases))
        self.assertEqual(a["metrics"]["sample_count"], 40)
        self.assertEqual(b["metrics"]["sample_count"], 40)

    def test_dev_selection_rejects_partial_incomplete_test_demo_draft_and_different_hash(self):
        a, b = self.report("v1"), self.report("v2")
        invalid = [
            self.report("v2", limit=5),
            self.report("v2", max_attempts=2),
            self.report("v2", split="test"),
            run_evaluation(self.samples, mode="demo", prompt_version="v2", max_workers=2),
        ]
        changed = copy.deepcopy(b)
        changed["metadata"]["dataset_hash"] = "a" * 64
        invalid.append(changed)
        for report in invalid:
            with self.subTest(mode=report["metadata"]["mode"], split=report["metadata"]["split"], count=report["metadata"]["sample_count"]), self.assertRaises(ValueError):
                compare_development_reports([a, report], self.samples)
        with self.assertRaises(ValueError):
            compare_development_reports([a, b], load_dataset(write_draft_dataset(self.directory / "draft.csv")))
        with self.assertRaises(ValueError):
            compare_development_reports([a, a], self.samples)

    def test_fixed_split_and_changed_prompt_hash_are_rejected(self):
        a, b = self.report("v1"), self.report("v2")
        samples = list(self.samples)
        samples[0] = samples[0].model_copy(update={"split": "test"})
        with self.assertRaises(ValueError):
            compare_development_reports([a, b], samples)
        b["metadata"]["prompt_hash"] = "b" * 64
        with self.assertRaises(ValueError):
            compare_development_reports([a, b], self.samples)

    def test_at_most_ten_cases_and_tampered_comparison_not_saved(self):
        changes = {sample.sample_id: {"category": "other" if sample.gold_category.value != "other" else "bugs"} for sample in self.samples if sample.split == "dev"}
        comparison = compare_development_reports([self.report("v1", changes=changes), self.report("v2")], self.samples)
        cases = comparison["error_analysis"]["representative_cases"]
        self.assertEqual(len(cases), 10)
        comparison["candidate_version"] = "v1"
        with self.assertRaises(ValueError):
            save_development_comparison(comparison, self.directory / "tampered.json")
        self.assertFalse((self.directory / "tampered.json").exists())

    def test_freeze_requires_independent_review_evidence_not_csv_flags(self):
        reports = [self.report("v1"), self.report("v2")]
        self.review.unlink()
        with self.assertRaises(ValueError):
            self.freeze(reports)
        self.assertFalse((self.directory / "experiment.json").exists())
        draft = write_draft_dataset(self.directory / "draft.csv")
        with self.assertRaises(ValueError):
            freeze_experiment_manifest(reports, dataset_path=draft, output_path=self.directory / "draft.json")
        self.assertFalse((self.directory / "draft.json").exists())

    def test_freeze_binds_hashes_settings_rule_and_v1_single_test_plan(self):
        reports = [self.report("v1"), self.report("v2")]
        frozen, path = self.freeze(reports)
        checked = validate_experiment_manifest(path, dataset_path=self.dataset, review_path=self.review)
        self.assertEqual(checked, frozen)
        self.assertEqual(frozen["prompts"]["baseline"]["hash"], prompt_hash("v1"))
        self.assertEqual(frozen["dataset"]["dataset_hash"], dataset_hash(self.samples))
        self.assertEqual(frozen["test_plan"]["versions"], ["v1"])
        self.assertEqual(frozen["test_plan"]["max_request_attempts"], 20)
        self.assertEqual(frozen["selection_rule"], SELECTION_RULE)
        self.assertEqual(frozen["runtime"]["max_workers"], 2)
        self.assertEqual(frozen["request_settings"]["max_retries"], 0)
        with self.assertRaises(ValueError):
            freeze_experiment_manifest(reports, dataset_path=self.dataset, review_path=self.review, output_path=path)

    def test_frozen_dataset_bytes_and_manifest_changes_are_detected(self):
        frozen, _ = self.freeze([self.report("v1"), self.report("v2")])
        changed = copy.deepcopy(frozen)
        changed["test_plan"]["versions"] = ["v1", "v2"]
        with self.assertRaises(ValueError):
            validate_experiment_manifest(changed, dataset_path=self.dataset, review_path=self.review)
        snapshot = Path(frozen["dataset"]["dataset_path"])
        snapshot.write_bytes(snapshot.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            validate_experiment_manifest(frozen, dataset_path=self.dataset, review_path=self.review)

    def test_test_report_must_start_after_freeze_and_match_frozen_settings(self):
        frozen, _ = self.freeze([self.report("v1"), self.report("v2")])
        test = self.report("v1", split="test")
        self.assertEqual(validate_test_report(test, frozen, dataset_path=self.dataset, review_path=self.review), test)
        too_early = copy.deepcopy(test)
        too_early["metadata"]["started_at"] = (datetime.fromisoformat(frozen["frozen_at_utc"]) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            validate_test_report(too_early, frozen, dataset_path=self.dataset, review_path=self.review)
        different = copy.deepcopy(test)
        different["metadata"]["request_settings"]["timeout_seconds"] += 1
        with self.assertRaises(ValueError):
            validate_test_report(different, frozen, dataset_path=self.dataset, review_path=self.review)
        outside = copy.deepcopy(test)
        outside["metadata"].update(prompt_version="v2", prompt_hash=prompt_hash("v2"))
        with self.assertRaises(ValueError):
            validate_test_report(outside, frozen, dataset_path=self.dataset, review_path=self.review)

    def test_final_summary_never_selects_on_test_and_rejects_duplicate_v1(self):
        frozen, _ = self.freeze([self.report("v1"), self.report("v2")])
        test = self.report("v1", split="test", changes={"test-001": {"category": "other"}})
        summary = summarize_test_reports([test], frozen, dataset_path=self.dataset, review_path=self.review)
        self.assertFalse(summary["selection_performed"])
        self.assertEqual(summary["frozen_candidate_version"], "v1")
        self.assertEqual(len(summary["sample_differences"]), 20)
        self.assertEqual(summary["change_counts"], {})
        paths = save_test_summary(summary, self.directory / "final_test.json")
        self.assertIn("不得依据结果继续调整", paths["markdown"].read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            summarize_test_reports([test, test], frozen, dataset_path=self.dataset, review_path=self.review)

    def test_v2_frozen_test_plan_requires_both_and_reports_degradation_honestly(self):
        frozen, _ = self.freeze([self.report("v1", changes={"dev-001": {"category": "other"}}), self.report("v2")])
        self.assertEqual(frozen["test_plan"]["versions"], ["v1", "v2"])
        self.assertEqual(frozen["test_plan"]["max_request_attempts"], 40)
        a = self.report("v1", split="test")
        b = self.report("v2", split="test", changes={"test-001": {"category": "other"}})
        with self.assertRaises(ValueError):
            summarize_test_reports([a], frozen, dataset_path=self.dataset, review_path=self.review)
        summary = summarize_test_reports([a, b], frozen, dataset_path=self.dataset, review_path=self.review)
        self.assertEqual(summary["frozen_candidate_version"], "v2")
        self.assertFalse(summary["selection_performed"])
        self.assertEqual(summary["change_counts"]["v2"]["category_degraded"], 1)

    def test_registration_extension_keeps_frozen_v1_v2_and_runs_through_evaluation(self):
        hashes = {version: prompt_hash(version) for version in ("v1", "v2")}
        version = "v99"
        try:
            register_prompt(version, "Unit-test feedback only: <feedback>{feedback_text}</feedback>")
            self.assertIn(version, available_prompt_versions())
            self.assertIn("feedback_text", get_prompt(version))
            report = self.report(version)
            self.assertEqual(report["metadata"]["prompt_version"], version)
            self.assertEqual(report["metadata"]["prompt_hash"], prompt_hash(version))
            for original, digest in hashes.items():
                self.assertEqual(prompt_hash(original), digest)
                with self.assertRaises(ValueError):
                    register_prompt(original, "replacement {feedback_text}")
            for template in ("missing field", "{feedback_text} {gold_category}", "{feedback_text} {feedback_text}"):
                with self.assertRaises(ValueError):
                    register_prompt("v98", template)
        finally:
            PROMPT_REGISTRY.pop(version, None)
            PROMPT_REGISTRY.pop("v98", None)


if __name__ == "__main__":
    unittest.main()
