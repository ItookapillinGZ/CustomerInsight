"""Offline UI confirmation checks use temporary copies, never the shipped gold."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from evaluation.data import load_dataset
from evaluation.review import load_review_ledger
from evaluation.runner import run_evaluation
from tests.helpers import offline_temp_root, write_draft_dataset


class HumanReviewUITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=offline_temp_root(), prefix=".review-ui-tests-")
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "feedback_eval.csv"
        write_draft_dataset(self.source)
        self.target = Path(self.temp.name) / "review.json"
        self.original = self.source.read_bytes()
        self.script = Path(self.temp.name) / "review_app.py"
        self.script.write_text(
            "from evaluation.review_ui import render_review\n"
            f"render_review(dataset_path={str(self.source)!r}, review_path={str(self.target)!r})\n",
            encoding="utf-8",
        )
        warning = patch("streamlit.delta_generator._maybe_print_use_warning")
        warning.start()
        self.addCleanup(warning.stop)

    def app(self):
        return AppTest.from_file(str(self.script), default_timeout=20)

    def opened(self):
        app = self.app().run()
        app.button(key="review_open_button").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def identity(self, app):
        app.text_input(key="review_reviewer").input("UI human fixture").run()
        app.text_area(key="review_reason_dev-001").input("按标注规则确认主问题及影响").run()

    def test_default_and_opening_do_not_write_confirm_or_show_predictions(self):
        app = self.app().run()
        self.assertEqual(len(app.get("download_button")), 0)
        app.button(key="review_open_button").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("download_button")), 2)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertTrue(app.button(key="review_confirm_single").disabled)
        self.assertTrue(any("不展示模型预测" in caption.value for caption in app.caption))
        self.assertFalse(any("predicted_" in " ".join(table.value.columns) for table in app.dataframe))

    def test_saving_draft_requires_reason_and_does_not_review(self):
        app = self.opened()
        self.assertTrue(app.button(key="review_save_draft").disabled)
        self.identity(app)
        app.button(key="review_save_draft").click().run()
        self.assertEqual(len(app.exception), 0)
        ledger = load_review_ledger(self.source, self.target)
        self.assertEqual(ledger["records"][0]["review_status"], "draft")
        self.assertFalse(ledger["records"][0]["history"][-1]["explicit_confirmation"])
        app.run()
        self.assertEqual(len(load_review_ledger(self.source, self.target)["records"][0]["history"]), 1)

    def test_single_confirmation_requires_separate_checkbox_and_explicit_button(self):
        app = self.opened()
        self.identity(app)
        self.assertTrue(app.button(key="review_confirm_single").disabled)
        checkbox = next(box for box in app.checkbox if "dev-001" in box.label)
        checkbox.check().run()
        self.assertFalse(app.button(key="review_confirm_single").disabled)
        self.assertFalse(self.target.exists())
        app.button(key="review_confirm_single").click().run()
        self.assertEqual(len(app.exception), 0)
        records = load_review_ledger(self.source, self.target)["records"]
        self.assertEqual([record["sample_id"] for record in records if record["review_status"] == "reviewed"], ["dev-001"])
        self.assertEqual(records[0]["history"][-1]["reviewer"], "UI human fixture")

    def test_bulk_confirmation_shows_exact_selected_ids_and_requires_checkbox(self):
        app = self.opened()
        app.text_input(key="review_reviewer").input("UI human fixture").run()
        ids = ["dev-002", "test-003"]
        app.multiselect(key="review_bulk_ids").set_value(ids).run()
        app.text_area(key="review_bulk_reason").input("逐条依照指南复核，无修改").run()
        self.assertTrue(app.button(key="review_confirm_bulk").disabled)
        selected_table = next(table.value for table in app.dataframe if "sample_id" in table.value.columns)
        self.assertEqual(selected_table["sample_id"].tolist(), ids)
        self.assertTrue(any(all(sample_id in element.value for sample_id in ids) for element in app.markdown))
        checkbox = next(box for box in app.checkbox if "上表列出的所有" in box.label)
        checkbox.check().run()
        self.assertFalse(self.target.exists())
        app.button(key="review_confirm_bulk").click().run()
        self.assertEqual(len(app.exception), 0)
        records = load_review_ledger(self.source, self.target)["records"]
        self.assertEqual([record["sample_id"] for record in records if record["review_status"] == "reviewed"], ids)

    def test_new_session_can_resume_saved_draft(self):
        app = self.opened()
        self.identity(app)
        priority = next(box for box in app.selectbox if box.label == "参考优先级")
        choice = "high" if priority.value != "high" else "medium"
        priority.select(choice).run()
        app.button(key="review_save_draft").click().run()
        resumed = self.opened()
        self.assertEqual(next(box for box in resumed.selectbox if box.label == "参考优先级").value, choice)
        self.assertEqual(load_dataset(self.source)[0].review_status, "draft")

    def test_review_mode_hides_evaluation_controls_and_retained_model_results(self):
        main = Path(__file__).resolve().parents[1] / "main.py"
        report = run_evaluation(load_dataset(self.source), mode="demo")
        with patch("evaluation.ui.DEFAULT_DATASET", self.source), patch("evaluation.ui.DEFAULT_REPORT_DIR", Path(self.temp.name)):
            app = AppTest.from_file(str(main), default_timeout=20).run()
            app.session_state["evaluation_reports"] = [report]
            app.run()
            self.assertTrue(any(button.key == "eval_run" for button in app.button))
            app.button(key="review_open_button").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertFalse(any(button.key == "eval_run" for button in app.button))
            self.assertFalse(any("predicted_" in " ".join(table.value.columns) for table in app.dataframe))
            self.assertEqual(len(app.get("download_button")), 2)
            app.button(key="review_close_button").click().run()
            self.assertTrue(any(button.key == "eval_run" for button in app.button))


if __name__ == "__main__":
    unittest.main()
