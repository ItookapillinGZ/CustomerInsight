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
from evaluation.reports import load_report, metrics_json, predictions_csv, report_markdown, save_report, validate_report
from evaluation.runner import run_evaluation
from tests.test_evaluation_runner import samples


class EvaluationReportTests(unittest.TestCase):
    def test_report_roundtrip_downloads_and_traceability_without_key(self):
        with patch.object(Config, "OPENAI_API_KEY", None), patch("evaluation.runner.get_openai_client") as factory:
            report = run_evaluation(samples())
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
                paths = save_report(report, output_dir=directory)
                loaded = load_report(paths["report"])
                self.assertEqual(loaded, report)
                self.assertEqual(set(paths), {"report", "metrics", "predictions", "markdown"})
                for path in paths.values():
                    self.assertTrue(path.is_file())
                exported = json.loads(metrics_json(loaded))
                self.assertEqual(exported["metadata"]["prompt_hash"], report["metadata"]["prompt_hash"])
                self.assertEqual(exported["metrics"], report["metrics"])
                self.assertNotIn("predictions", exported)
        factory.assert_not_called()
        rows = list(csv.DictReader(io.StringIO(predictions_csv(report).decode("utf-8-sig"))))
        self.assertEqual([row["sample_id"] for row in rows], report["metadata"]["sample_ids"])
        self.assertTrue(all(row["mode"] == "demo" for row in rows))
        markdown = report_markdown(report)
        self.assertIn("DEMO", markdown)
        self.assertIn("待", markdown)
        self.assertIn("不能代表真实接口性能", markdown)
        self.assertIn("__failure__", markdown)
        self.assertIn("support", markdown)
        self.assertNotIn("api_key", markdown.lower())

    def test_csv_protects_formulas_without_changing_local_original(self):
        data = samples()
        data[0] = data[0].model_copy(update={"text": "=HYPERLINK(\"unsafe\")"})
        report = run_evaluation(data)
        rows = list(csv.DictReader(io.StringIO(predictions_csv(report).decode("utf-8-sig"))))
        self.assertEqual(rows[0]["text"], "'=HYPERLINK(\"unsafe\")")
        self.assertEqual(report["predictions"][0]["text"], data[0].text)

    def test_load_rejects_mixed_predictions_metrics_tampering_or_secret_settings(self):
        original = run_evaluation(samples())
        changes = [
            ("source", lambda report: report["predictions"][0].update(prediction_source="live")),
            ("denominator", lambda report: report["metrics"].update(sample_count=1)),
            ("metric", lambda report: report["metrics"]["sentiment"].update(accuracy=1.0)),
            ("key", lambda report: report["metadata"]["request_settings"].update(api_key="private")),
            ("gold", lambda report: report["predictions"][0].update(gold_sentiment="neutral")),
            ("top_level_secret", lambda report: report.update(api_key="private")),
            ("metadata_secret", lambda report: report["metadata"].update(api_key="private")),
            ("prediction_secret", lambda report: report["predictions"][0].update(api_key="private")),
            ("nested_setting", lambda report: report["metadata"]["request_settings"].update(model={"api_key": "private"})),
            ("raw_error", lambda report: report["predictions"][0].update(error_message="private raw exception body")),
        ]
        for name, change in changes:
            report = copy.deepcopy(original)
            change(report)
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_report(report)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "broken.report.json"
            path.write_text("not JSON", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_report(path)

    def test_report_rejects_invalid_setting_types_dates_and_inconsistent_request_provenance(self):
        report = run_evaluation(samples())
        changes = [
            ("boolean_tokens", lambda item: item["metadata"]["request_settings"].update(max_output_tokens=True)),
            ("nan_timeout", lambda item: item["metadata"]["request_settings"].update(timeout_seconds=float("nan"))),
            ("bad_api", lambda item: item["metadata"]["request_settings"].update(api_mode="unknown")),
            ("bad_hash", lambda item: item["metadata"]["request_settings"].update(endpoint_sha256="bad")),
            ("missing_setting", lambda item: item["metadata"]["request_settings"].pop("store")),
            ("sdk_retries", lambda item: item["metadata"]["request_settings"].update(sdk_max_retries=2)),
            ("request_bound", lambda item: item["metadata"].update(request_attempts_upper_bound=100)),
            ("configured_model", lambda item: item["metadata"].update(configured_model="another-model")),
            ("negative_duration", lambda item: item["metadata"].update(wall_duration_ms=-1)),
            ("backward_time", lambda item: item["metadata"].update(completed_at="2000-01-01T00:00:00+00:00")),
        ]
        for name, change in changes:
            altered = copy.deepcopy(report)
            change(altered)
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_report(altered)

    def test_cli_default_run_and_compare_are_offline_and_can_compare_saved_reports(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory, patch("evaluation.runner.get_openai_client") as factory, patch("evaluation.runner.analyze_feedback") as analyzer, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["validate"]), 0)
            self.assertEqual(main(["run", "--output-dir", directory, "--workers", "1"]), 0)
            self.assertEqual(main(["compare", "--output-dir", directory, "--workers", "1"]), 0)
            paths = list(Path(directory).glob("*.report.json"))
            self.assertEqual(len(paths), 3)
            selected = {}
            for path in paths:
                selected[load_report(path)["metadata"]["prompt_version"]] = path
            self.assertEqual(main(["compare", "--reports", str(selected["v1"]), str(selected["v2"])]), 0)
            self.assertIn("DEMO", output.getvalue())
        factory.assert_not_called()
        analyzer.assert_not_called()

    def test_cli_live_flag_is_explicit_and_report_comparison_cannot_start_requests(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory, patch("evaluation.cli.run_evaluation", wraps=run_evaluation) as evaluate, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["demo", "--output-dir", directory]), 0)
            self.assertEqual(evaluate.call_args.kwargs["mode"], "demo")
            evaluate.reset_mock()
            self.assertEqual(main(["compare", "--reports", "unused", "--live"]), 2)
            evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
