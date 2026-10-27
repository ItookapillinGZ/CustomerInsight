"""Offline UI checks prove explicit triggers, retained reports, and safe downloads."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from core.config import Config
from evaluation.data import load_dataset
from evaluation.reports import save_report
from evaluation.runner import run_evaluation
from evaluation.ui import load_saved_report, request_scale, saved_report_files
from models.models import CustomerFeedbackAnalysis
from tests.helpers import fake_client, valid_analysis, write_draft_dataset


APP_PATH = Path(__file__).resolve().parents[1] / "main.py"


class EvaluationDashboardTests(unittest.TestCase):
    def setUp(self):
        launch_warning = patch("streamlit.delta_generator._maybe_print_use_warning")
        launch_warning.start()
        self.addCleanup(launch_warning.stop)
        # Keep temporary test reports in this checkout, including Windows
        # restricted hosts whose system Temp is not writable by child Python.
        self.temp = TemporaryDirectory(dir=APP_PATH.parent, prefix=".ui-tests-")
        self.addCleanup(self.temp.cleanup)
        directory = patch("evaluation.ui.DEFAULT_REPORT_DIR", Path(self.temp.name))
        directory.start()
        self.addCleanup(directory.stop)
        self.dataset = write_draft_dataset(Path(self.temp.name) / "feedback_eval.csv")
        source = patch("evaluation.ui.DEFAULT_DATASET", self.dataset)
        source.start()
        self.addCleanup(source.stop)
        self.samples = load_dataset(self.dataset)

    def app(self):
        return AppTest.from_file(str(APP_PATH), default_timeout=20)

    def test_default_and_rerender_never_trigger_evaluation(self):
        with patch.object(Config, "OPENAI_API_KEY", None), patch("evaluation.ui.run_evaluation") as run:
            app = self.app().run()
            app.selectbox(key="eval_split").select("test").run()
            app.selectbox(key="eval_prompt_version").select("v2").run()
            app.slider(key="eval_workers").set_value(2).run()
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual([tab.label for tab in app.tabs], ["单条反馈分析", "CSV 批量分析", "自动化评测"])
        self.assertEqual(len(app.get("file_uploader")), 1)
        run.assert_not_called()

    def test_explicit_demo_comparison_and_downloads_survive_rerenders(self):
        with patch.object(Config, "OPENAI_API_KEY", None), patch("evaluation.ui.run_evaluation", wraps=run_evaluation) as run, patch("evaluation.ui.save_report") as save:
            app = self.app().run()
            app.selectbox(key="eval_prompt_version").select("v1 与 v2 对比").run()
            run.assert_not_called()
            app.button(key="eval_run").click().run()
            self.assertEqual(len(app.exception), 0)
            reports = app.session_state["evaluation_reports"]
            self.assertEqual([report["metadata"]["prompt_version"] for report in reports], ["v1", "v2"])
            self.assertTrue(all(report["metadata"]["mode"] == "demo" for report in reports))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(save.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["split"], "dev")
                self.assertEqual(call.kwargs["mode"], "demo")
                self.assertEqual(len(call.args[0]), 60)
            self.assertEqual(len(app.get("download_button")), 3)
            self.assertTrue(any("DEMO" in message.value for message in app.info))
            self.assertTrue(any("临时结果" in message.value for message in app.warning))
            app.selectbox(key="eval_report_detail").select(1).run()
            app.selectbox(key="eval_split").select("test").run()
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(app.get("download_button")), 3)
        tables = [table.value for table in app.dataframe]
        comparison = next(frame for frame in tables if "category_macro_f1" in frame.columns)
        self.assertEqual(comparison["prompt_version"].tolist(), ["v1", "v2"])
        confusion = next(frame for frame in tables if "__failure__" in frame.columns)
        self.assertGreaterEqual(confusion["__failure__"].sum(), 1)

    def test_live_requires_key_and_explicit_click(self):
        client = fake_client()
        analysis = CustomerFeedbackAnalysis.model_validate(valid_analysis())
        with patch.object(Config, "OPENAI_API_KEY", None), patch("evaluation.ui.run_evaluation") as run:
            app = self.app().run()
            app.radio(key="eval_mode").set_value("LIVE 真实接口").run()
            self.assertTrue(app.button(key="eval_run").disabled)
            run.assert_not_called()
        with patch.object(Config, "OPENAI_API_KEY", "offline-test-key"), patch("evaluation.runner.analyze_feedback", return_value=analysis) as analyzer, patch("evaluation.ui.run_evaluation", side_effect=lambda samples, **kwargs: run_evaluation(samples, client=client, **kwargs)) as run, patch("evaluation.ui.save_report"):
            app.run()
            app.selectbox(key="eval_prompt_version").select("v2").run()
            app.selectbox(key="eval_split").select("test").run()
            self.assertTrue(app.button(key="eval_run").disabled)
            run.assert_not_called()
            app.selectbox(key="eval_split").select("dev").run()
            app.selectbox(key="eval_selection_mode").select("限制样本数").run()
            run.assert_not_called()
            app.button(key="eval_run").click().run()
            self.assertEqual(len(app.exception), 0)
            report = app.session_state["evaluation_reports"][0]
            self.assertEqual(report["metadata"]["mode"], "live")
            self.assertEqual(report["metadata"]["sample_count"], 5)
            self.assertEqual(run.call_args.kwargs["prompt_version"], "v2")
            self.assertEqual(run.call_args.kwargs["mode"], "live")
            self.assertEqual(analyzer.call_count, 5)
            app.run()
            run.assert_called_once()
            self.assertEqual(analyzer.call_count, 5)

    def test_local_reports_load_without_key_and_do_not_run_model(self):
        report = run_evaluation(self.samples, mode="demo")
        save_report(report, output_dir=Path(self.temp.name))
        names = [path.name for path in saved_report_files()]
        with patch.object(Config, "OPENAI_API_KEY", None), patch("evaluation.ui.run_evaluation") as run:
            app = self.app().run()
            app.multiselect(key="eval_saved_reports").set_value(names).run()
            self.assertNotIn("evaluation_reports", app.session_state)
            app.button(key="eval_load_reports").click().run()
            app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["evaluation_reports"][0]["metadata"]["run_id"], report["metadata"]["run_id"])
        self.assertEqual(len(app.get("download_button")), 3)
        run.assert_not_called()

    def test_invalid_local_report_is_rejected_before_rendering(self):
        (Path(self.temp.name) / "invalid.report.json").write_text('{"schema_version":1}', encoding="utf-8")
        with patch("evaluation.ui.run_evaluation") as run:
            app = self.app().run()
            app.multiselect(key="eval_saved_reports").set_value(["invalid.report.json"]).run()
            app.button(key="eval_load_reports").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("报告读取失败" in error.value for error in app.error))
        self.assertNotIn("evaluation_reports", app.session_state)
        run.assert_not_called()

    def test_saved_report_viewer_works_when_current_dataset_is_invalid(self):
        report = run_evaluation(self.samples)
        save_report(report, output_dir=Path(self.temp.name))
        names = [path.name for path in saved_report_files()]
        with patch.object(Config, "OPENAI_API_KEY", None), patch("evaluation.ui.load_dataset", side_effect=ValueError("数据集无效")), patch("evaluation.ui.run_evaluation") as run:
            app = self.app().run()
            app.multiselect(key="eval_saved_reports").set_value(names).run()
            app.button(key="eval_load_reports").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("数据校验失败" in error.value for error in app.error))
        self.assertEqual(app.session_state["evaluation_reports"][0]["metadata"]["run_id"], report["metadata"]["run_id"])
        self.assertEqual(len(app.get("download_button")), 3)
        run.assert_not_called()

    def test_incompatible_reports_show_reason_without_comparison_metrics(self):
        for split, version in [("dev", "v1"), ("test", "v2")]:
            save_report(run_evaluation(self.samples, split=split, prompt_version=version), output_dir=Path(self.temp.name))
        names = [path.name for path in saved_report_files()]
        with patch("evaluation.ui.run_evaluation") as run:
            app = self.app().run()
            app.multiselect(key="eval_saved_reports").set_value(names).run()
            app.button(key="eval_load_reports").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("不能比较" in error.value for error in app.error))
        self.assertFalse(any("category_macro_f1" in table.value.columns for table in app.dataframe))
        self.assertEqual(len(app.get("download_button")), 3)
        run.assert_not_called()

    def test_saved_report_selection_rejects_arbitrary_paths(self):
        report = run_evaluation(self.samples)
        save_report(report, output_dir=Path(self.temp.name))
        with self.assertRaises(ValueError):
            load_saved_report("../outside.report.json")
        with self.assertRaises(ValueError):
            load_saved_report(str(Path(self.temp.name) / saved_report_files()[0].name))

    def test_request_scale_includes_all_versions_and_retries(self):
        with patch.object(Config, "MAX_RETRIES", 2):
            self.assertEqual(request_scale(20, 2, mode="live"), 40)
            self.assertEqual(request_scale(20, 2, mode="live", max_retries=2), 120)
            self.assertEqual(request_scale(40, 2, mode="demo"), 0)

    def test_single_and_csv_prompt_selection_is_forwarded_once(self):
        analysis = CustomerFeedbackAnalysis.model_validate(valid_analysis())
        # AppTest executes main.py under its own module; patch the imported
        # dependency so the test observes the actual click path.
        with patch("ai.analyzer.analyze_feedback", return_value=analysis) as analyzer:
            app = self.app().run()
            app.selectbox(key="single_prompt_version").select("v2").run()
            app.text_area(key="single_text").input("结账失败").run()
            app.button(key="analyze_single").click().run()
            app.run()
        analyzer.assert_called_once_with("结账失败", prompt_version="v2")
        upload = BytesIO(b"feedback\nCheckout fails\n")
        upload.name = "feedback.csv"
        with patch("streamlit.file_uploader", return_value=upload), patch("ai.batch.analyze_batch", side_effect=lambda rows, **kwargs: rows) as batch:
            app = self.app().run()
            app.selectbox(key="batch_prompt_version").select("v2").run()
            app.button(key="analyze_batch").click().run()
            app.run()
        self.assertEqual(len(app.exception), 0)
        batch.assert_called_once()
        self.assertEqual(batch.call_args.kwargs["prompt_version"], "v2")


if __name__ == "__main__":
    unittest.main()
