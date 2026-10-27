"""Offline UI integration for local history, comparisons and grounded topics."""

from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from streamlit.testing.v1 import AppTest

from core.analysis_agent import AnalysisAgentResult, report_fingerprint
from core.history_store import HistoryStore
from core.product_service import CustomerAnalysisResults
from models.enums import ProcessingStatus
from models.models import FeedbackResult
from tests import test_customer_app as customer_fixtures


def upload(body, filename):
    result = BytesIO(body.encode("utf-8-sig"))
    result.name = filename
    return result


def rows_with_aspects(rows, progress_callback=None, result_callback=None, **kwargs):
    results = []
    for index, row in enumerate(rows):
        if row.processing_status == ProcessingStatus.PENDING:
            text = row.original_text
            if "减" in text or "希望" in text:
                topic, kind, sentiment = "包装减塑", "request", "neutral"
            elif "破损" in text or "裂纹" in text:
                topic, kind, sentiment = "包装完整", "issue", "negative"
            else:
                topic, kind, sentiment = "包装完整", "praise", "positive"
            row = FeedbackResult.model_validate({**row.model_dump(), "processing_status": "success",
                "sentiment": sentiment, "category": "product_quality", "priority": "medium" if kind == "issue" else "low",
                "summary": text, "aspects": [{"topic": topic, "kind": kind, "sentiment": sentiment, "evidence": text[:240]}]})
        results.append(row)
        if result_callback:
            result_callback(row)
        if progress_callback:
            progress_callback(index + 1, len(rows))
    return CustomerAnalysisResults(results, {"mode": "grouped", "prompt_version": "v3_grouped_aspects_v1",
        "planned_requests": 1, "actual_requests": 1, "run_status": "finished"})


def history_agent_answer(report, question, **kwargs):
    previous = report.get("historical_context")
    history_evidence = []
    if previous and previous.get("evidence"):
        row = previous["evidence"][0]
        history_evidence = [{"batch_id": previous["batch"]["id"], "filename": previous["batch"]["filename"],
                             "source_row": row["source_row"], "text": row["text"]}]
    return AnalysisAgentResult(status="completed", question=question,
        conclusion="本批提出包装减塑需求，上批原文谈到包装完整。建议分别核对原文并小规模试行。",
        conclusion_metrics=[], findings=[], actions=[], answer_origin="llm", evidence_rows=[],
        historical_evidence=history_evidence, scope_note="当前与上批行号分别标记。",
        tool_trace=["get_review_topics", "compare_history", "read_previous_reviews"] if previous else ["get_review_topics"],
        model_calls=2, tool_calls=3 if previous else 1, total_tokens=120, usage_reported_requests=2,
        report_fingerprint=report_fingerprint(report), fallback_reason=None)


class HistoryAppTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict("os.environ", {"APP_DATA_DIR": self.directory}))
        self.stack.enter_context(patch("streamlit.delta_generator._maybe_print_use_warning"))
        self.stack.enter_context(patch("core.product_service.requires_access_password", return_value=False))
        self.stack.enter_context(patch("core.product_service.service_access_password", return_value=None))
        self.stack.enter_context(patch("core.product_service.get_service_status", return_value={
            "configured": True, "remaining_today": 50, "daily_limit": 50,
            "request_batch_size": 10, "max_batch_characters": 12000, "agent_requests_per_run": 3}))
        self.stack.enter_context(patch("core.product_service.estimate_customer_requests", return_value=1))
        self.analyzer = self.stack.enter_context(patch("core.product_service.analyze_customer_rows", side_effect=rows_with_aspects))
        self.agent = self.stack.enter_context(patch("core.product_service.run_customer_report_agent", side_effect=history_agent_answer))
        self.upload = None
        self.stack.enter_context(patch("streamlit.file_uploader", side_effect=lambda *args, **kwargs: self.upload))

    def new_app(self):
        app = AppTest.from_file(str(customer_fixtures.APP_PATH), default_timeout=25).run()
        self.assertEqual(len(app.exception), 0)
        return app

    def analyze(self, app):
        app.checkbox(key="customer_consent").check().run()
        app.button(key="customer_analyze").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("customer_active_batch_id", app.session_state)
        return app

    def create_report(self, filename="本批.csv", text="希望减少塑料包装", date="2026-10-10"):
        self.upload = upload(f"产品,评价,日期\n杯子,{text},{date}\n", filename)
        return self.analyze(self.new_app())

    def open_history(self, batch_id):
        self.upload = None
        app = self.new_app()
        app.selectbox(key="customer_history_selection").select(batch_id).run()
        app.button(key="customer_open_history").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def ask(self, app, question="请解释这个主题"):
        app.text_area(key="customer_agent_question").input(question).run()
        app.checkbox(key="customer_agent_consent").check().run()
        app.button(key="customer_agent_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_analysis_archives_original_report_topics_mapping_and_dates_locally(self):
        app = self.create_report()
        batch_id = app.session_state["customer_active_batch_id"]
        store = HistoryStore()
        record = store.load_batch(batch_id)
        self.assertEqual(len(store.list_batches()), 1)
        self.assertEqual(record["filename"], "本批.csv")
        self.assertEqual(store.read_original(batch_id), self.upload.getvalue())
        self.assertEqual(record["report"]["topics"][0]["kind"], "request")
        self.assertEqual(record["report"]["period"]["start"], "2026-10-10")
        self.assertEqual(store.get_mapping(["产品", "评价", "日期"])["date_column"], "日期")
        self.assertTrue((Path(self.directory) / "customerinsight.sqlite3").is_file())
        self.analyzer.assert_called_once()
        self.agent.assert_not_called()

    def test_new_session_opens_archived_report_without_upload_or_any_analysis_request(self):
        first = self.create_report()
        batch_id = first.session_state["customer_active_batch_id"]
        self.analyzer.reset_mock()
        app = self.open_history(batch_id)
        self.assertTrue(app.session_state["customer_history_view"])
        self.assertEqual(app.session_state["customer_report"]["metrics"]["success_rows"], 1)
        self.assertEqual([tab.label for tab in app.tabs], ["分析结果", "问 Agent", "明细与下载"])
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_identical_file_and_mapping_restore_without_duplicate_analysis(self):
        first = self.create_report()
        batch_id = first.session_state["customer_active_batch_id"]
        self.analyzer.reset_mock()
        second = self.new_app()
        self.assertEqual(second.session_state["customer_active_batch_id"], batch_id)
        self.assertEqual(len(HistoryStore().list_batches()), 1)
        self.assertTrue(any("已恢复相同文件" in message.value for message in second.info))
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_clear_current_upload_keeps_archived_report_and_original(self):
        app = self.create_report()
        batch_id = app.session_state["customer_active_batch_id"]
        self.upload = None
        app.button(key="customer_clear").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_report", app.session_state)
        self.assertEqual(HistoryStore().load_batch(batch_id)["filename"], "本批.csv")
        self.assertTrue(HistoryStore().read_original(batch_id))
        self.analyzer.assert_called_once()

    def test_selected_history_is_injected_for_agent_and_references_are_explicit(self):
        first = self.create_report("上批.csv", "收到时包装破损", "2026-10-01")
        previous_id = first.session_state["customer_active_batch_id"]
        app = self.create_report("本批.csv", "希望减少塑料包装", "2026-10-10")
        current_id = app.session_state["customer_active_batch_id"]
        app.selectbox(key=f"customer_compare_{current_id}").select(previous_id).run()
        self.ask(app, "与上批相比具体变了什么？")
        sent = self.agent.call_args.args[0]
        self.assertEqual(sent["historical_context"]["batch"]["id"], previous_id)
        self.assertTrue(sent["historical_context"]["comparison"]["comparable"])
        self.assertEqual(sent["historical_context"]["evidence"][0]["text"], "收到时包装破损")
        self.assertNotIn("historical_context", app.session_state["customer_report"])
        self.assertNotIn("historical_context", HistoryStore().load_batch(current_id)["report"])
        self.assertIn("核对上批引用的原文", [item.label for item in app.expander])
        self.assertTrue(any("上批《上批.csv》第 2 行" in item.value for item in app.caption))
        self.assertEqual(app.session_state["customer_agent_result"]["evidence_rows"], [])
        saved = HistoryStore().load_chat(current_id)
        self.assertEqual(saved[0]["comparison_batch_id"], previous_id)
        self.assertEqual(saved[0]["result"]["historical_evidence"][0]["batch_id"], previous_id)
        self.assertEqual(self.analyzer.call_count, 2)
        self.agent.assert_called_once()

    def test_chat_reopens_from_history_and_clear_chat_is_persisted(self):
        app = self.create_report()
        batch_id = app.session_state["customer_active_batch_id"]
        self.ask(app, "减塑需求有什么建议？")
        self.assertEqual(len(HistoryStore().load_chat(batch_id)), 1)
        reopened = self.open_history(batch_id)
        self.assertEqual(len(reopened.session_state["customer_agent_history"]), 1)
        self.assertIn("减塑需求有什么建议？", [item.value for item in reopened.markdown])
        reopened.button(key="customer_agent_clear_chat").click().run()
        self.assertEqual(len(reopened.exception), 0)
        self.assertEqual(HistoryStore().load_chat(batch_id), [])
        self.assertIn("customer_report", reopened.session_state)
        self.assertFalse(reopened.session_state.filtered_state.get("customer_agent_history", []))
        self.agent.assert_called_once()
        self.analyzer.assert_called_once()

    def test_history_chat_restores_selected_comparison_and_keeps_batch_namespace(self):
        previous = self.create_report("上批.csv", "收到时包装破损", "2026-10-01")
        previous_id = previous.session_state["customer_active_batch_id"]
        current = self.create_report()
        batch_id = current.session_state["customer_active_batch_id"]
        current.selectbox(key=f"customer_compare_{batch_id}").select(previous_id).run()
        self.ask(current, "上批原文支持什么？")
        reopened = self.open_history(batch_id)
        self.assertEqual(reopened.session_state["customer_previous_batch_id"], previous_id)
        self.assertEqual(reopened.session_state["customer_agent_history"][0]["result"]["historical_evidence"][0]["batch_id"], previous_id)
        self.assertIn("核对上批引用的原文", [item.label for item in reopened.expander])
        self.agent.assert_called_once()

    def test_backup_download_and_confirmed_deletion_only_affect_selected_batch(self):
        first = self.create_report("保留.csv", "包装完好", "2026-10-01")
        retained_id = first.session_state["customer_active_batch_id"]
        second = self.create_report("删除.csv", "希望减少塑料包装", "2026-10-10")
        deleted_id = second.session_state["customer_active_batch_id"]
        self.upload = None
        app = self.new_app()
        app.selectbox(key="customer_history_selection").select(deleted_id).run()
        self.assertTrue(app.button(key="customer_delete_history").disabled)
        app.button(key="customer_prepare_backup").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("下载全部历史备份", [item.label for item in app.get("download_button")])
        with zipfile.ZipFile(BytesIO(app.session_state["customer_backup_blob"])) as archive:
            self.assertTrue(any(name.endswith(".sqlite3") for name in archive.namelist()))
            self.assertGreaterEqual(len(archive.namelist()), 3)
        app.checkbox(key=f"customer_delete_confirm_{deleted_id}").check().run()
        self.assertFalse(app.button(key="customer_delete_history").disabled)
        app.button(key="customer_delete_history").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual([item["id"] for item in HistoryStore().list_batches()], [retained_id])
        self.assertEqual(HistoryStore().load_batch(retained_id)["filename"], "保留.csv")
        self.agent.assert_not_called()

    def test_column_mapping_is_reused_for_different_file_with_same_headers(self):
        self.upload = upload("门店,内容甲,内容乙,日期\n东店,内部备注,包装完好,2026-10-01\n", "甲.csv")
        first = self.new_app()
        next(item for item in first.selectbox if item.label == "哪一列是顾客评价？").select("内容乙").run()
        self.analyze(first)
        self.upload = upload("门店,内容甲,内容乙,日期\n西店,另一备注,希望减少塑料包装,2026-10-10\n", "乙.csv")
        second = self.new_app()
        self.assertEqual(next(item for item in second.selectbox if item.label == "哪一列是顾客评价？").value, "内容乙")
        self.assertEqual(next(item for item in second.selectbox if item.label == "按产品或门店汇总（可选）").value, "门店")
        self.assertEqual(next(item for item in second.selectbox if item.label == "哪一列是评价日期？").value, "日期")
        self.assertNotIn("customer_report", second.session_state)
        self.analyzer.assert_called_once()
        self.agent.assert_not_called()

    def test_topic_button_prefills_question_without_request_or_consent(self):
        app = self.create_report()
        self.assertNotIn("包装减塑", app.text_area(key="customer_agent_question").value)
        app.button(key="customer_topic_ask_0").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("包装减塑", app.text_area(key="customer_agent_question").value)
        self.assertTrue(app.button(key="customer_agent_generate").disabled)
        self.assertFalse(app.checkbox(key="customer_agent_consent").value)
        self.agent.assert_not_called()
        self.analyzer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
