import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from ai.analyzer import StructuredOutputError
from core.config import Config
from evaluation.data import EvaluationSample, load_dataset
from evaluation.runner import compare_reports, run_evaluation
from models.models import CustomerFeedbackAnalysis
from tests.helpers import valid_analysis


def samples():
    return [
        EvaluationSample(sample_id="preserve-ID-A", text="结账页面无法提交。", gold_sentiment="negative", gold_category="bugs", gold_priority="high", split="dev", review_status="draft"),
        EvaluationSample(sample_id="preserve-ID-B", text="加入按年份筛选功能。", gold_sentiment="neutral", gold_category="features", gold_priority="medium", split="dev", review_status="draft"),
        EvaluationSample(sample_id="preserve-ID-C", text="按钮文字很难理解。", gold_sentiment="negative", gold_category="user_experience", gold_priority="low", split="dev", review_status="draft"),
        EvaluationSample(sample_id="test-only-ID", text="客服回复解决了疑问。", gold_sentiment="positive", gold_category="customer_service", gold_priority="low", split="test", review_status="draft"),
    ]


class EvaluationRunnerTests(unittest.TestCase):
    def test_live_boundary_preserves_ids_and_never_passes_gold(self):
        selected = samples()
        client = MagicMock()
        analyzer = MagicMock(return_value=CustomerFeedbackAnalysis.model_validate(valid_analysis()))
        with patch("evaluation.runner.analyze_feedback", analyzer):
            report = run_evaluation(selected, mode="live", client=client, prompt_version="v2", max_workers=1)
        self.assertEqual([row["sample_id"] for row in report["predictions"]], [item.sample_id for item in selected[:3]])
        self.assertEqual(analyzer.call_count, 3)
        for call, sample in zip(analyzer.call_args_list, selected[:3]):
            self.assertEqual(call.args, (sample.text,))
            self.assertEqual(call.kwargs["client"], client)
            self.assertEqual(call.kwargs["prompt_version"], "v2")
            self.assertEqual(call.kwargs["max_retries"], 0)
            self.assertEqual(call.kwargs["request_control"].sample_id, sample.sample_id)
            self.assertNotIn("gold", json.dumps({"text": call.args[0]}))
        client.close.assert_not_called()
        self.assertEqual(report["metadata"]["prompt_version"], "v2")
        self.assertEqual(report["metadata"]["review_status_counts"], {"draft": 3, "reviewed": 0})
        self.assertEqual(report["metadata"]["result_status"], "provisional")

    def test_failures_and_invalid_complete_result_are_isolated_and_safe(self):
        client = MagicMock()
        responses = [
            RuntimeError("private-api-key-and-request-body"),
            StructuredOutputError("private-api-key-and-request-body"),
            CustomerFeedbackAnalysis.model_construct(**valid_analysis(sentiment="invented")),
        ]
        with patch("evaluation.runner.analyze_feedback", side_effect=responses):
            report = run_evaluation(samples(), mode="live", client=client, max_workers=1)
        self.assertEqual([row["status"] for row in report["predictions"]], ["failed", "validation_error", "validation_error"])
        self.assertEqual(report["metrics"]["failed_count"], 3)
        self.assertEqual(report["metrics"]["sentiment"]["accuracy"], 0)
        self.assertNotIn("private-api-key", json.dumps(report))
        for row in report["predictions"]:
            self.assertIsNone(row["predicted_sentiment"])
            self.assertIsNone(row["predicted_category"])
            self.assertIsNone(row["predicted_priority"])
            self.assertIsNone(row["summary"])

    def test_demo_is_deterministic_does_not_read_gold_and_never_opens_client(self):
        base = samples()
        changed = [item.model_copy(update={"gold_sentiment": "positive", "gold_category": "other", "gold_priority": "medium"}) for item in base]
        with patch("evaluation.runner.get_openai_client") as factory, patch("evaluation.runner.analyze_feedback") as analyzer:
            report_a = run_evaluation(base)
            report_b = run_evaluation(changed)
        factory.assert_not_called()
        analyzer.assert_not_called()
        predicted_keys = ["sample_id", "status", "predicted_sentiment", "predicted_category", "predicted_priority", "latency_ms"]
        self.assertEqual(
            [{key: row[key] for key in predicted_keys} for row in report_a["predictions"]],
            [{key: row[key] for key in predicted_keys} for row in report_b["predictions"]],
        )
        self.assertEqual(report_a["metadata"]["mode"], "demo")
        self.assertTrue(report_a["metrics"]["latency"]["is_mock"])
        self.assertGreater(report_a["metrics"]["failures_by_status"]["failed"], 0)
        self.assertGreater(report_a["metrics"]["failures_by_status"]["validation_error"], 0)
        self.assertNotEqual(report_a["metadata"]["dataset_hash"], report_b["metadata"]["dataset_hash"])

    def test_demo_dataset_exercises_wrong_labels_and_both_failure_types(self):
        report = run_evaluation(load_dataset())
        observed = {field for row in report["errors"] for field in row["error_fields"]}
        self.assertTrue({"情感错误", "类别错误", "优先级错误", "调用失败", "结构校验失败"}.issubset(observed))

    def test_invalid_inputs_are_rejected_before_client_or_request(self):
        with patch("evaluation.runner.get_openai_client") as factory, patch("evaluation.runner.analyze_feedback") as analyzer:
            for kwargs in ({"prompt_version": "missing"}, {"mode": "unknown"}, {"max_workers": True}, {"max_workers": 0}, {"split": "unknown"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    run_evaluation(samples(), **{**{"mode": "live"}, **kwargs})
            with self.assertRaises(ValueError):
                run_evaluation([samples()[0], samples()[0]], mode="live")
        factory.assert_not_called()
        analyzer.assert_not_called()

    def test_owned_live_client_closes_and_secrets_not_in_metadata(self):
        client = MagicMock()
        with patch.object(Config, "OPENAI_API_KEY", "sk-DO-NOT-EXPORT"), patch("evaluation.runner.get_openai_client", return_value=client) as factory, patch("evaluation.runner.analyze_feedback", return_value=CustomerFeedbackAnalysis.model_validate(valid_analysis())):
            report = run_evaluation(samples(), mode="live", max_workers=1)
        factory.assert_called_once_with()
        client.close.assert_called_once_with()
        self.assertNotIn("sk-DO-NOT-EXPORT", json.dumps(report))
        self.assertEqual(report["metadata"]["request_attempts_upper_bound"], 3)
        self.assertEqual(report["metadata"]["application_max_retries"], 0)

    def test_comparison_requires_same_dataset_model_settings_mode_and_samples(self):
        a = run_evaluation(samples(), prompt_version="v1")
        b = run_evaluation(samples(), prompt_version="v2")
        comparison = compare_reports([a, b])
        self.assertTrue(comparison["compatible"])
        self.assertEqual([row["prompt_version"] for row in comparison["rows"]], ["v1", "v2"])
        self.assertNotEqual(a["metadata"]["prompt_hash"], b["metadata"]["prompt_hash"])
        for key, replacement in (
            ("dataset_hash", "a" * 64), ("configured_model", "different-model"),
            ("request_settings", {**b["metadata"]["request_settings"], "timeout_seconds": 999}),
            ("max_workers", 1), ("sample_ids", ["different"]), ("mode", "live"),
        ):
            changed = copy.deepcopy(b)
            changed["metadata"][key] = replacement
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare_reports([a, changed])
        with self.assertRaises(ValueError):
            compare_reports([a, a])


if __name__ == "__main__":
    unittest.main()
