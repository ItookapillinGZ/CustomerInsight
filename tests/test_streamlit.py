"""Exercise real Streamlit controls with deterministic, offline SDK responses."""

from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from core.config import Config
from tests.helpers import fake_client, fake_response


APP_PATH = Path(__file__).resolve().parents[1] / "main.py"


class StreamlitSmokeTests(unittest.TestCase):
    def setUp(self):
        # Streamlit's first-render launch warning imports CLI styling and walks
        # inspect.stack() to detect a REPL. Windows sandbox path resolution can
        # make that environment-only check slow. AppTest needs no launch hint;
        # the widgets and application code below still execute normally.
        launch_warning = patch("streamlit.delta_generator._maybe_print_use_warning")
        launch_warning.start()
        self.addCleanup(launch_warning.stop)
        api_mode = patch.object(Config, "API_MODE", "responses", create=True)
        api_mode.start()
        self.addCleanup(api_mode.stop)

    def new_app(self):
        return AppTest.from_file(str(APP_PATH), default_timeout=10)

    def test_application_starts_without_api_key(self):
        with patch.object(Config, "OPENAI_API_KEY", None), patch("ai.analyzer.get_openai_client") as factory:
            app = self.new_app().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "CustomerInsight")
        self.assertEqual([tab.label for tab in app.tabs], ["单条反馈分析", "CSV 批量分析", "自动化评测"])
        self.assertEqual(len(app.get("file_uploader")), 1)
        factory.assert_not_called()

    def test_blank_single_feedback_shows_warning_without_api_call(self):
        with patch("ai.analyzer.get_openai_client") as factory:
            app = self.new_app().run()
            app.button(key="analyze_single").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.warning), 1)
        factory.assert_not_called()

    def test_single_feedback_mock_analysis_and_clear(self):
        client = fake_client()
        with patch("ai.analyzer.get_openai_client", return_value=client):
            app = self.new_app().run()
            app.text_area(key="single_text").input("Checkout fails").run()
            app.button(key="analyze_single").click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state["single_result"]["sentiment"], "negative")
            self.assertEqual(len(app.json), 1)
            next(button for button in app.button if button.label == "清空单条反馈").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.text_area(key="single_text").value, "")
        self.assertNotIn("single_result", app.session_state)
        self.assertEqual(len(app.json), 0)
        self.assertEqual(client.responses.parse.call_count, 1)
        client.close.assert_called_once_with()

    def test_upload_select_column_analyze_partial_failure_and_offer_download(self):
        # AppTest does not offer a file-uploader setter. Only that widget's
        # returned file is mocked; selectbox/button/table/chart code runs normally.
        upload = BytesIO(
            b"customer,comments\nA,Checkout fails\nB,\nC,Checkout fails\nD,SERVICE_FAILURE\nE,Works well\n"
        )
        upload.name = "feedback.csv"
        client = fake_client()

        def reply(**kwargs):
            if "SERVICE_FAILURE" in kwargs["input"][-1]["content"]:
                raise RuntimeError("Service unavailable")
            return fake_response()

        client.responses.parse.side_effect = reply
        with patch("streamlit.file_uploader", return_value=upload), patch("ai.batch.get_openai_client", return_value=client):
            app = self.new_app().run()
            next(box for box in app.selectbox if box.label == "选择反馈文本列").select("comments").run()
            app.button(key="analyze_batch").click().run()
        self.assertEqual(len(app.exception), 0)
        frame = app.session_state["batch_result"]
        self.assertEqual(frame["original_text"].tolist(), ["Checkout fails", "", "Checkout fails", "SERVICE_FAILURE", "Works well"])
        self.assertEqual(frame["processing_status"].tolist(), ["success", "skipped_empty", "skipped_duplicate", "failed", "success"])
        metrics = {metric.label: metric.value for metric in app.metric}
        self.assertEqual(metrics, {"原始记录": "5", "分析成功": "2", "分析失败": "1", "高优先级反馈": "2"})
        self.assertGreaterEqual(len(app.dataframe), 2)
        charts = app.get("arrow_vega_lite_chart") + app.get("vega_lite_chart")
        self.assertEqual(len(charts), 2)
        self.assertEqual(len(app.get("download_button")), 1)
        self.assertEqual(client.responses.parse.call_count, 3)
        client.close.assert_called_once_with()

    def test_invalid_uploaded_csv_shows_error_and_does_not_create_client(self):
        upload = BytesIO(b'comments\n"unterminated')
        upload.name = "broken.csv"
        with patch("streamlit.file_uploader", return_value=upload), patch("ai.batch.get_openai_client") as factory:
            app = self.new_app().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 1)
        self.assertNotIn("batch_result", app.session_state)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
