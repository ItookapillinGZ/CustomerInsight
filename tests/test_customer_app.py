"""Offline customer workflow tests; no credentials or real requests are used."""

from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from core.product_service import ProductLimitError
from core.analysis_agent import AnalysisAgentResult, report_fingerprint
from models.enums import ProcessingStatus
from models.models import FeedbackResult


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
NATURAL_AGENT_ANSWER = (
    "当前应优先核对产品质量问题。第 2 行提到**杯口有裂纹**，应先确认这件商品的破损情况。\n\n"
    "建议先查看同批次的包装与退换货记录，再判断是否需要扩大抽检。"
)
UNFINISHED_AGENT_ANSWER = "本轮未能生成可靠回答，请重新发送问题；已有分析报告仍可查看。"


def uploaded_csv(body="产品,评价\n杯子,杯口有裂纹\n杯子,包装很好\n"):
    stream = BytesIO(body.encode("utf-8-sig"))
    stream.name = "评价.csv"
    return stream


def successful_rows(rows, progress_callback=None, result_callback=None, **kwargs):
    results = []
    for index, row in enumerate(rows):
        if row.processing_status == ProcessingStatus.PENDING:
            if row.original_text == "服务失败":
                row = FeedbackResult.model_validate({
                    **row.model_dump(), "processing_status": "failed",
                    "error_message": "分析服务暂时不可用。",
                })
            else:
                row = FeedbackResult.model_validate({
                    **row.model_dump(), "processing_status": "success",
                    "sentiment": "negative" if "裂纹" in row.original_text else "positive",
                    "category": "product_quality", "priority": "high" if "裂纹" in row.original_text else "low",
                    "summary": "杯口有裂纹" if "裂纹" in row.original_text else "包装评价良好",
                })
        results.append(row)
        if result_callback:
            result_callback(row)
        if progress_callback:
            progress_callback(index + 1, len(rows))
    return results


def successful_agent(report, question, **kwargs):
    return AnalysisAgentResult(
        status="completed", question=question,
        conclusion=NATURAL_AGENT_ANSWER,
        conclusion_metrics=["负面占有效评价：50.0%"],
        findings=[], actions=[], answer_origin="llm", evidence_rows=[2],
        scope_note="结论只依据本次表格的有效评价。",
        tool_trace=["get_overview", "find_negative_evidence"],
        model_calls=2, tool_calls=2, total_tokens=120,
        usage_reported_requests=2, report_fingerprint=report_fingerprint(report),
        fallback_reason=None,
    )


class CustomerAppTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict("os.environ", {"APP_DATA_DIR": directory}))
        self.stack.enter_context(patch("streamlit.delta_generator._maybe_print_use_warning"))
        self.requires_password = self.stack.enter_context(patch("core.product_service.requires_access_password", return_value=False))
        self.password = self.stack.enter_context(patch("core.product_service.service_access_password", return_value=None))
        self.status = self.stack.enter_context(patch("core.product_service.get_service_status", return_value={
            "configured": True, "remaining_today": 50, "daily_limit": 50,
            "request_batch_size": 10, "max_batch_characters": 12000,
            "agent_requests_per_run": 3,
        }))
        self.estimator = self.stack.enter_context(patch(
            "core.product_service.estimate_customer_requests",
            side_effect=lambda rows: (sum(row.processing_status == ProcessingStatus.PENDING for row in rows) + 9) // 10,
        ))
        self.analyzer = self.stack.enter_context(patch("core.product_service.analyze_customer_rows", side_effect=successful_rows))
        self.agent = self.stack.enter_context(patch("core.product_service.run_customer_report_agent", side_effect=successful_agent))

    def new_app(self):
        return AppTest.from_file(str(APP_PATH), default_timeout=15)

    def with_upload(self, stream=None):
        upload = stream or uploaded_csv()
        self.stack.enter_context(patch("streamlit.file_uploader", return_value=upload))
        return self.new_app().run()

    def analyze(self, app):
        app.checkbox(key="customer_consent").check().run()
        app.button(key="customer_analyze").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def ask_agent(self, app, question):
        app.text_area(key="customer_agent_question").input(question).run()
        app.checkbox(key="customer_agent_consent").check().run()
        app.button(key="customer_agent_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_starts_with_upload_only_and_no_request(self):
        app = self.new_app().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "顾客评价分析")
        self.assertEqual(len(app.get("file_uploader")), 1)
        self.assertEqual(len(app.tabs), 0)
        self.analyzer.assert_not_called()

    def test_upload_defaults_to_review_column_and_requires_consent(self):
        app = self.with_upload()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(next(box for box in app.selectbox if box.label == "哪一列是顾客评价？").value, "评价")
        self.assertTrue(app.button(key="customer_analyze").disabled)
        self.assertNotIn("customer_report", app.session_state)
        self.analyzer.assert_not_called()

    def test_full_flow_grouping_statistics_downloads_and_no_repeat(self):
        app = self.with_upload()
        next(box for box in app.selectbox if box.label == "按产品或门店汇总（可选）").select("产品").run()
        self.analyze(app)
        report = app.session_state["customer_report"]
        self.assertEqual(report["metrics"]["success_rows"], 2)
        self.assertEqual(report["metrics"]["negative_rate"], 0.5)
        self.assertIn("正面占 50.0%、负面占 50.0%", report["executive_summary"])
        displayed = [element.value for element in app.markdown]
        self.assertIn("**本次评价总结**", displayed)
        self.assertIn(report["executive_summary"], displayed)
        self.assertEqual(report["grouped"]["分组"].tolist(), ["杯子"])
        self.assertEqual(len(report["negative_examples"]), 1)
        self.assertGreaterEqual(len(app.get("arrow_vega_lite_chart") + app.get("vega_lite_chart")), 4)
        self.assertEqual([tab.label for tab in app.tabs], ["分析结果", "问 Agent", "明细与下载"])
        downloads = {button.label for button in app.get("download_button")}
        self.assertIn("下载逐条分析明细 CSV", downloads)
        self.assertIn("下载完整分析报告", downloads)
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.analyzer.assert_called_once()
        self.agent.assert_not_called()
        passed_rows = self.analyzer.call_args.args[0]
        self.assertEqual([row.original_text for row in passed_rows], ["杯口有裂纹", "包装很好"])
        self.assertNotIn("杯子", [row.original_text for row in passed_rows])

    def test_partial_failure_and_skips_are_not_counted_as_normal_reviews(self):
        app = self.with_upload(uploaded_csv("产品,评价\n杯子,杯口有裂纹\n杯子,\n杯子,杯口有裂纹\n杯子,服务失败\n"))
        self.analyze(app)
        metrics = app.session_state["customer_report"]["metrics"]
        self.assertEqual(metrics["total_rows"], 4)
        self.assertEqual(metrics["success_rows"], 2)
        self.assertEqual(metrics["model_success_rows"], 1)
        self.assertEqual(metrics["reused_rows"], 1)
        self.assertEqual(metrics["failed_rows"], 1)
        self.assertEqual(metrics["skipped_empty_rows"], 1)
        self.assertEqual(metrics["skipped_duplicate_rows"], 1)
        self.assertEqual(metrics["negative_rate"], 1.0)
        self.assertTrue(any("部分评价" in warning.value for warning in app.warning))
        caption = " ".join(element.value for element in app.caption)
        self.assertIn("实际分析成功 1 条", caption)
        self.assertIn("相同文本复用 1 条", caption)
        self.assertIn("跳过 1 条", caption)

    def test_reused_review_counts_in_its_own_store_without_another_request(self):
        app = self.with_upload(uploaded_csv("门店,评价\n东店,杯口有裂纹\n西店,杯口有裂纹\n西店,包装很好\n"))
        next(box for box in app.selectbox if box.label == "按产品或门店汇总（可选）").select("门店").run()
        self.analyze(app)
        report = app.session_state["customer_report"]
        self.assertEqual(report["metrics"]["success_rows"], 3)
        self.assertEqual(report["metrics"]["model_success_rows"], 2)
        self.assertEqual(report["metrics"]["reused_rows"], 1)
        grouped = report["grouped"].set_index("分组")
        self.assertEqual(grouped.loc["东店", "负面评价数"], 1)
        self.assertEqual(grouped.loc["西店", "负面评价数"], 1)
        self.assertEqual(grouped.loc["西店", "成功分析数"], 2)
        self.assertEqual(next(metric for metric in app.metric if metric.label == "有效评价").value, "3")
        caption = " ".join(element.value for element in app.caption)
        self.assertIn("跳过 0 条", caption)
        self.analyzer.assert_called_once()

    def test_column_change_invalidates_results_and_consent(self):
        app = self.analyze(self.with_upload())
        next(box for box in app.selectbox if box.label == "哪一列是顾客评价？").select("产品").run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_report", app.session_state)
        self.assertFalse(app.checkbox(key="customer_consent").value)
        self.assertTrue(app.button(key="customer_analyze").disabled)
        self.analyzer.assert_called_once()

    def test_new_upload_invalidates_previous_report(self):
        upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=uploaded_csv()))
        app = self.analyze(self.new_app().run())
        upload.return_value = uploaded_csv("产品,评价\n盘子,好用\n")
        app.run()
        self.assertNotIn("customer_report", app.session_state)
        self.assertFalse(app.checkbox(key="customer_consent").value)
        self.analyzer.assert_called_once()

    def test_xlsx_sheet_change_clears_results(self):
        upload = BytesIO(b"mock-xlsx-contents")
        upload.name = "评价.xlsx"
        self.stack.enter_context(patch("core.excel_io.inspect_workbook", return_value=["华东", "华南"]))
        self.stack.enter_context(patch("core.excel_io.read_feedback_xlsx", return_value=pd.DataFrame({"评价": ["杯口有裂纹"]})))
        app = self.analyze(self.with_upload(upload))
        next(box for box in app.selectbox if box.label == "选择工作表").select("华南").run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_report", app.session_state)
        self.assertFalse(app.checkbox(key="customer_consent").value)
        self.analyzer.assert_called_once()

    def test_clear_removes_session_data_replaces_uploader_and_keeps_other_session(self):
        upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=uploaded_csv()))
        first = self.analyze(self.new_app().run())
        second = self.analyze(self.new_app().run())
        upload.return_value = None
        first.button(key="customer_clear").click().run()
        self.assertEqual(len(first.exception), 0)
        self.assertEqual(first.session_state["customer_upload_nonce"], 1)
        self.assertNotIn("customer_results", first.session_state)
        self.assertNotIn("customer_report", first.session_state)
        self.assertNotIn("customer_file_digest", first.session_state)
        self.assertIn("customer_report", second.session_state)
        self.assertIn("customer_results", second.session_state)

    def test_old_xls_explains_conversion_without_calling_service(self):
        upload = BytesIO(b"old-xls")
        upload.name = "旧表.xls"
        app = self.with_upload(upload)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("另存为 .xlsx" in error.value for error in app.error))
        self.analyzer.assert_not_called()

    def test_missing_configuration_and_exhausted_quota_disable_run(self):
        self.status.return_value = {"configured": False, "remaining_today": 50, "daily_limit": 50}
        app = self.with_upload()
        app.checkbox(key="customer_consent").check().run()
        self.assertTrue(app.button(key="customer_analyze").disabled)
        self.assertTrue(any("普通业务用户请联系管理员" in warning.value for warning in app.warning))
        self.assertTrue(any(".env" in element.value and "业务用户无需填写 Key" in element.value for element in app.markdown))
        self.status.return_value = {"configured": True, "remaining_today": 0, "daily_limit": 50}
        app.run()
        self.assertTrue(app.button(key="customer_analyze").disabled)
        self.assertTrue(any("剩余额度" in warning.value for warning in app.warning))
        self.analyzer.assert_not_called()

    def test_quota_uses_estimated_requests_instead_of_review_count(self):
        self.status.return_value = {"configured": True, "remaining_today": 1, "daily_limit": 50, "request_batch_size": 10}
        app = self.with_upload()
        app.checkbox(key="customer_consent").check().run()
        self.assertFalse(app.button(key="customer_analyze").disabled)
        self.assertEqual(app.session_state["customer_expected_requests"], 1)
        self.assertTrue(any("有效待分析 2 条，预计合并 1 次请求" in element.value for element in app.caption))
        self.estimator.side_effect = None
        self.estimator.return_value = 2
        app.run()
        self.assertTrue(app.button(key="customer_analyze").disabled)
        self.assertTrue(any("本次预计需要 2 次" in element.value for element in app.warning))
        self.analyzer.assert_not_called()

    def test_one_hundred_short_reviews_show_ten_estimated_requests(self):
        body = "产品,评价\n" + "".join(f"杯子,第{index}条独立评价\n" for index in range(100))
        app = self.with_upload(uploaded_csv(body))
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["customer_expected_requests"], 10)
        self.assertEqual(next(metric for metric in app.metric if metric.label == "有效待分析").value, "100")
        self.assertEqual(next(metric for metric in app.metric if metric.label == "预计合并请求").value, "10")
        self.analyzer.assert_not_called()

    def test_search_filters_and_group_chart_controls_do_not_repeat_analysis(self):
        app = self.with_upload(uploaded_csv("门店,评价\n东店,杯口有裂纹\n西店,包装很好\n西店,送货很快\n"))
        next(box for box in app.selectbox if box.label == "按产品或门店汇总（可选）").select("门店").run()
        self.analyze(app)
        app.text_input(key="customer_filter_search").input("裂纹").run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.session_state["customer_visible_detail"]), 1)
        self.assertEqual(app.session_state["customer_report"]["metrics"]["success_rows"], 3)
        app.text_input(key="customer_filter_search").input("").run()
        app.multiselect(key="customer_filter_sentiment").select("正面").run()
        self.assertEqual(len(app.session_state["customer_visible_detail"]), 2)
        app.selectbox(key="customer_group_chart_metric").select("负面占比").run()
        self.assertEqual(len(app.exception), 0)
        self.analyzer.assert_called_once()

    def test_actual_request_metadata_is_preserved_and_displayed(self):
        class Results(list):
            analysis_meta = {"planned_requests": 1, "actual_requests": 1, "elapsed_seconds": 2.5}

        def reply(rows, progress_callback=None, **kwargs):
            return Results(successful_rows(rows, progress_callback, **kwargs))

        self.analyzer.side_effect = reply
        app = self.analyze(self.with_upload())
        self.assertEqual(app.session_state["customer_report"]["analysis_meta"]["actual_requests"], 1)
        self.assertTrue(any("本次实际使用 1 次请求（预计 1 次），用时 2.5 秒" in element.value for element in app.caption))

    def test_saved_public_example_is_separate_and_never_calls_service(self):
        app = self.new_app().run()
        app.button(key="customer_view_demo").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_report", app.session_state)
        self.assertEqual(app.session_state["customer_demo_report"]["metrics"]["total_rows"], 20)
        self.assertFalse(next(expander for expander in app.expander if expander.label == "上传评价表").proto.expanded)
        self.assertTrue(any("历史公开样例" in element.value and "不消耗今日额度" in element.value for element in app.info))
        app.text_input(key="customer_filter_search").input("product").run()
        self.assertIn("customer_demo_report", app.session_state)
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()
        self.assertFalse(any(button.key == "customer_agent_generate" for button in app.button))
        app.button(key="customer_clear_export").click().run()
        self.assertNotIn("customer_demo_report", app.session_state)
        self.assertNotIn("customer_visible_detail", app.session_state)

    def test_own_upload_exits_public_example(self):
        upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=None))
        app = self.new_app().run()
        app.button(key="customer_view_demo").click().run()
        self.assertIn("customer_demo_report", app.session_state)
        upload.return_value = uploaded_csv()
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_demo_report", app.session_state)
        self.assertNotIn("customer_report", app.session_state)
        self.analyzer.assert_not_called()

    def test_invited_access_wrong_then_correct_password_is_not_retained(self):
        self.requires_password.return_value = True
        self.password.return_value = "测试访问码"
        app = self.new_app().run()
        self.assertEqual(len(app.get("file_uploader")), 0)
        app.text_input(key="customer_password").input("不正确")
        next(button for button in app.button if button.label == "进入分析").click().run()
        self.assertTrue(any("访问码不正确" in error.value for error in app.error))
        app.text_input(key="customer_password").input("测试访问码")
        next(button for button in app.button if button.label == "进入分析").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("file_uploader")), 1)
        self.assertNotIn("测试访问码", str(app.session_state.filtered_state))
        self.analyzer.assert_not_called()

    def test_required_access_without_configured_password_is_closed(self):
        self.requires_password.return_value = True
        app = self.new_app().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("file_uploader")), 0)
        self.assertTrue(any("暂未开放" in error.value for error in app.error))
        self.analyzer.assert_not_called()

    def test_invalid_public_access_configuration_is_closed_without_traceback(self):
        self.requires_password.side_effect = ProductLimitError("APP_PUBLIC_MODE contains private diagnostic details")
        app = self.new_app().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("file_uploader")), 0)
        self.assertTrue(any("访问配置无效" in error.value for error in app.error))
        self.assertFalse(any("private diagnostic" in error.value for error in app.error))
        self.analyzer.assert_not_called()

    def test_unexpected_error_does_not_echo_credentials_or_feedback(self):
        self.analyzer.side_effect = RuntimeError("secret-provider-token customer phone 13800138000")
        app = self.analyze(self.with_upload())
        self.assertNotIn("customer_report", app.session_state)
        error_text = " ".join(error.value for error in app.error)
        self.assertNotIn("secret-provider-token", error_text)
        self.assertNotIn("13800138000", error_text)
        self.analyzer.assert_called_once()

    def test_interrupted_analysis_keeps_partial_report_pending_rows_and_downloads(self):
        def interrupted(rows, progress_callback=None, result_callback=None, **kwargs):
            result_callback(successful_rows(rows[:1])[0])
            raise RuntimeError("private provider diagnostic")

        self.analyzer.side_effect = interrupted
        app = self.analyze(self.with_upload())
        self.assertEqual(app.session_state["customer_results"]["processing_status"].tolist(), ["success", "pending"])
        report = app.session_state["customer_report"]
        self.assertEqual(report["metrics"]["success_rows"], 1)
        self.assertEqual(report["metrics"]["pending_rows"], 1)
        self.assertEqual(report["metrics"]["failed_rows"], 0)
        self.assertTrue(report["analysis_meta"]["run_incomplete"])
        self.assertNotIn("actual_requests", report["analysis_meta"])
        self.assertIn("下载逐条分析明细 CSV", [button.label for button in app.get("download_button")])
        self.assertIn("下载完整分析报告", [button.label for button in app.get("download_button")])
        self.assertTrue(any("实际请求用量未完整返回" in item.value for item in app.caption))
        # A rerender in this same session can rebuild a report if the previous
        # save was interrupted after callbacks updated the mutable snapshot.
        del app.session_state["customer_report"]
        del app.session_state["customer_results"]
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["customer_report"]["metrics"]["pending_rows"], 1)
        self.analyzer.assert_called_once()

    def test_changing_file_discards_partial_snapshot(self):
        def interrupted(rows, progress_callback=None, result_callback=None, **kwargs):
            result_callback(successful_rows(rows[:1])[0])
            raise RuntimeError("interrupted")

        self.analyzer.side_effect = interrupted
        upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=uploaded_csv()))
        app = self.analyze(self.new_app().run())
        self.assertIn("customer_run_snapshot", app.session_state)
        upload.return_value = uploaded_csv("产品,评价\n盘子,新文件里的评价\n")
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_run_snapshot", app.session_state)
        self.assertNotIn("customer_report", app.session_state)
        self.assertNotIn("customer_results", app.session_state)
        self.analyzer.assert_called_once()

    def test_no_valid_results_show_unknown_negative_rate(self):
        app = self.analyze(self.with_upload(uploaded_csv("产品,评价\n杯子,服务失败\n")))
        self.assertEqual(app.session_state["customer_report"]["metrics"]["success_rows"], 0)
        self.assertEqual(next(metric for metric in app.metric if metric.label == "差评占比").value, "—")
        self.assertTrue(any("没有有效分析结果" in item.value for item in app.info))

    def test_agent_requires_separate_consent_and_only_explicit_button_calls_once(self):
        app = self.analyze(self.with_upload())
        self.assertTrue(app.button(key="customer_agent_generate").disabled)
        self.agent.assert_not_called()
        app.text_area(key="customer_agent_question").input("最应该先解决哪个问题？").run()
        self.agent.assert_not_called()
        app.checkbox(key="customer_agent_consent").check().run()
        self.agent.assert_not_called()
        app.button(key="customer_agent_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        self.agent.assert_called_once()
        called_report, called_question = self.agent.call_args.args
        self.assertIs(called_report, app.session_state["customer_report"])
        self.assertEqual(called_question, "最应该先解决哪个问题？")
        self.assertEqual(app.session_state["customer_agent_result"]["model_calls"], 2)
        self.assertIn(NATURAL_AGENT_ANSWER, [item.value for item in app.markdown])
        self.assertTrue(any("参考原表第 2 行" in item.value for item in app.caption))
        self.assertIn("下载本次经营建议", [button.label for button in app.get("download_button")])
        app.run()
        self.assertEqual(self.agent.call_count, 1)

    def test_agent_is_disabled_when_quota_is_below_three_or_results_are_empty(self):
        self.status.return_value = {
            "configured": True, "remaining_today": 2, "daily_limit": 50,
            "request_batch_size": 10, "agent_requests_per_run": 3,
        }
        app = self.analyze(self.with_upload())
        app.checkbox(key="customer_agent_consent").check().run()
        self.assertTrue(app.button(key="customer_agent_generate").disabled)
        self.assertTrue(any("需要预留 3 次" in item.value for item in app.warning))
        self.agent.assert_not_called()

        self.status.return_value = {
            "configured": True, "remaining_today": 50, "daily_limit": 50,
            "request_batch_size": 10, "agent_requests_per_run": 3,
        }
        app = self.analyze(self.with_upload(uploaded_csv("产品,评价\n杯子,服务失败\n")))
        app.checkbox(key="customer_agent_consent").check().run()
        self.assertTrue(app.button(key="customer_agent_generate").disabled)
        self.agent.assert_not_called()

    def test_agent_fallback_is_explained_without_internal_reason(self):
        def fallback(report, question, **kwargs):
            result = successful_agent(report, question)
            return result.model_copy(update={
                "status": "fallback", "fallback_reason": "SecretProviderDiagnostic",
                "conclusion": UNFINISHED_AGENT_ANSWER, "model_calls": 1,
                "answer_origin": "fallback", "evidence_rows": [], "conclusion_metrics": [],
            })

        self.agent.side_effect = fallback
        app = self.analyze(self.with_upload())
        app.checkbox(key="customer_agent_consent").check().run()
        app.button(key="customer_agent_generate").click().run()
        self.assertTrue(any("回答未完成" in item.value for item in app.markdown))
        self.assertTrue(any("模型回答未完成" in item.value for item in app.warning))
        rendered = " ".join(
            item.value for collection in (app.markdown, app.caption, app.warning, app.info)
            for item in collection
        )
        self.assertNotIn("SecretProviderDiagnostic", rendered)
        self.assertIn(UNFINISHED_AGENT_ANSWER, rendered)
        self.assertNotIn("安全总结", rendered)
        self.assertNotIn("依据已核对", rendered)

    def test_agent_natural_answer_markdown_is_preserved_without_template_headings(self):
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "具体应该怎么改善？")
        displayed = [item.value for item in app.markdown]
        self.assertIn(NATURAL_AGENT_ANSWER, displayed)
        self.assertTrue(any("LLM 回答" in text for text in displayed))
        self.assertFalse(any("依据已核对" in text or "安全总结" in text for text in displayed))
        self.assertNotIn("主要发现", [item.value for item in app.subheader])
        self.assertNotIn("建议下一步", [item.value for item in app.subheader])
        self.assertFalse(any(item.value == NATURAL_AGENT_ANSWER for item in app.info))
        self.assertEqual(app.session_state["customer_agent_result"]["answer_origin"], "llm")

    def test_agent_original_rows_and_statistics_are_available_in_reference_expanders(self):
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "哪条评价能说明问题？")
        report = app.session_state["customer_report"]
        names = report["detail_columns"]
        reference = next(item for item in app.expander if item.label == "核对引用的评价原文")
        table = reference.dataframe[0].value
        self.assertEqual(table[names["source_row"]].tolist(), [2])
        self.assertEqual(table["评价"].tolist(), ["杯口有裂纹"])
        basis = next(item for item in app.expander if item.label == "查看回答依据与运行信息")
        self.assertTrue(any("负面占有效评价：50.0%" in item.value for item in basis.caption))
        self.assertTrue(any("实际模型请求 2 次" in item.value for item in basis.caption))
        self.agent.assert_called_once()

    def test_agent_legacy_template_is_labelled_distinctly_when_history_is_reopened(self):
        app = self.analyze(self.with_upload())
        report = app.session_state["customer_report"]
        legacy = successful_agent(report, "之前的模板回答").model_copy(update={
            "answer_origin": "legacy_template", "conclusion": "历史模板的统计摘要。",
        })
        app.session_state["customer_agent_result"] = legacy.model_dump(mode="json")
        app.session_state["customer_agent_report_key"] = report_fingerprint(report)
        app.run()
        displayed = [item.value for item in app.markdown]
        self.assertTrue(any("历史模板回答" in text for text in displayed))
        self.assertFalse(any("LLM 回答" in text or "依据已核对" in text for text in displayed))
        self.assertIn("历史模板的统计摘要。", displayed)
        self.agent.assert_not_called()

    def test_agent_failure_is_safe_and_keeps_report_downloads(self):
        self.agent.side_effect = RuntimeError("private-token 13800138000 raw review")
        app = self.analyze(self.with_upload())
        app.checkbox(key="customer_agent_consent").check().run()
        app.button(key="customer_agent_generate").click().run()
        error_text = " ".join(item.value for item in app.error)
        self.assertIn("回答暂时未生成", error_text)
        self.assertNotIn("private-token", error_text)
        self.assertNotIn("13800138000", error_text)
        downloads = [button.label for button in app.get("download_button")]
        self.assertIn("下载逐条分析明细 CSV", downloads)
        self.assertIn("下载完整分析报告", downloads)

    def test_agent_state_is_cleared_when_upload_changes(self):
        upload = self.stack.enter_context(patch("streamlit.file_uploader", return_value=uploaded_csv()))
        app = self.analyze(self.new_app().run())
        app.checkbox(key="customer_agent_consent").check().run()
        app.button(key="customer_agent_generate").click().run()
        self.assertIn("customer_agent_result", app.session_state)
        self.assertEqual(len(app.session_state["customer_agent_history"]), 1)
        upload.return_value = uploaded_csv("产品,评价\n盘子,完全不同的新评价\n")
        app.run()
        self.assertNotIn("customer_agent_result", app.session_state)
        self.assertNotIn("customer_agent_consent", app.session_state)
        self.assertNotIn("customer_agent_history", app.session_state)

    def test_agent_two_turns_forward_prior_answer_and_keep_input_available(self):
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "哪一个问题应该先处理？")
        first_result = AnalysisAgentResult.model_validate(app.session_state["customer_agent_result"])
        self.assertEqual(self.agent.call_args.kwargs["history"], [])

        self.ask_agent(app, "针对刚才的问题，应该怎样安排负责人？")
        self.assertEqual(self.agent.call_count, 2)
        prior = self.agent.call_args.kwargs["history"]
        self.assertEqual(len(prior), 1)
        self.assertIsInstance(prior[0], AnalysisAgentResult)
        self.assertEqual(prior[0].model_dump(), first_result.model_dump())
        turns = app.session_state["customer_agent_history"]
        self.assertEqual([turn["question"] for turn in turns], [
            "哪一个问题应该先处理？", "针对刚才的问题，应该怎样安排负责人？",
        ])
        self.assertEqual(turns[0]["result"]["question"], turns[0]["question"])
        self.assertEqual(turns[1]["result"]["question"], turns[1]["question"])
        self.assertEqual(len(app.get("chat_message")), 4)

        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.text_area(key="customer_agent_question").value, "")
        self.assertTrue(app.button(key="customer_agent_generate").disabled)
        app.text_area(key="customer_agent_question").input("还有什么需要持续跟踪？").run()
        self.assertFalse(app.button(key="customer_agent_generate").disabled)
        self.assertEqual(self.agent.call_count, 2)
        self.assertEqual(len(app.session_state["customer_agent_history"]), 2)

    def test_agent_clear_conversation_keeps_upload_report_and_does_not_call_services(self):
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "先处理什么？")
        report = app.session_state["customer_report"]
        digest = app.session_state["customer_file_digest"]
        app.button(key="customer_agent_clear_chat").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["customer_file_digest"], digest)
        self.assertIs(app.session_state["customer_report"], report)
        self.assertIn("customer_results", app.session_state)
        self.assertFalse(app.session_state.filtered_state.get("customer_agent_history", []))
        self.assertNotIn("customer_agent_result", app.session_state)
        self.assertEqual(len(app.get("chat_message")), 0)
        self.analyzer.assert_called_once()
        self.agent.assert_called_once()

        self.ask_agent(app, "重新从头分析一下？")
        self.assertEqual(self.agent.call_args.kwargs["history"], [])
        self.assertEqual(len(app.session_state["customer_agent_history"]), 1)

    def test_agent_column_change_discards_conversation(self):
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "最需要关注什么？")
        next(box for box in app.selectbox if box.label == "哪一列是顾客评价？").select("产品").run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("customer_report", app.session_state)
        self.assertNotIn("customer_agent_history", app.session_state)
        self.assertNotIn("customer_agent_result", app.session_state)
        self.assertNotIn("customer_agent_consent", app.session_state)
        self.agent.assert_called_once()

    def test_agent_conversations_are_isolated_between_sessions(self):
        self.stack.enter_context(patch("streamlit.file_uploader", return_value=uploaded_csv()))
        first = self.analyze(self.new_app().run())
        second = self.analyze(self.new_app().run())
        self.ask_agent(first, "第一位用户的问题")
        self.ask_agent(second, "第二位用户的问题")
        self.assertEqual(self.agent.call_args.kwargs["history"], [])
        self.assertEqual([turn["question"] for turn in first.session_state["customer_agent_history"]], ["第一位用户的问题"])
        self.assertEqual([turn["question"] for turn in second.session_state["customer_agent_history"]], ["第二位用户的问题"])

        self.ask_agent(first, "继续第一位用户的分析")
        self.assertEqual([turn.question for turn in self.agent.call_args.kwargs["history"]], ["第一位用户的问题"])
        self.assertEqual(len(second.session_state["customer_agent_history"]), 1)
        self.assertEqual(len(first.session_state["customer_agent_history"]), 2)

    def test_agent_fallback_retains_previous_turn_and_is_passed_to_follow_up(self):
        def reply(report, question, **kwargs):
            result = successful_agent(report, question)
            if question == "补充一条保守建议":
                return result.model_copy(update={
                    "status": "fallback", "fallback_reason": "SecretProviderDiagnostic",
                    "conclusion": UNFINISHED_AGENT_ANSWER, "model_calls": 1,
                    "answer_origin": "fallback", "evidence_rows": [], "conclusion_metrics": [],
                })
            return result

        self.agent.side_effect = reply
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "优先处理什么？")
        self.ask_agent(app, "补充一条保守建议")
        turns = app.session_state["customer_agent_history"]
        self.assertEqual([turn["result"]["status"] for turn in turns], ["completed", "fallback"])
        self.assertEqual(len(app.get("chat_message")), 4)
        self.assertTrue(any("回答未完成" in item.value for item in app.markdown))
        self.assertFalse(any("安全总结" in item.value for item in app.markdown))
        self.assertFalse(any("SecretProviderDiagnostic" in item.value for item in app.markdown))

        self.ask_agent(app, "基于前面继续分析")
        self.assertEqual([turn.status for turn in self.agent.call_args.kwargs["history"]], ["completed", "fallback"])
        self.assertEqual(len(app.session_state["customer_agent_history"]), 3)

    def test_agent_failed_turn_keeps_previous_answer_and_can_be_explicitly_retried(self):
        app = self.analyze(self.with_upload())
        self.ask_agent(app, "先处理什么？")
        self.agent.side_effect = RuntimeError("private-token 13800138000 raw review")
        self.ask_agent(app, "具体怎么跟进？")
        turns = app.session_state["customer_agent_history"]
        self.assertEqual(len(turns), 2)
        self.assertIsNotNone(turns[0]["result"])
        self.assertIsNone(turns[1]["result"])
        self.assertTrue(turns[1]["error"])
        self.assertEqual(len(app.get("chat_message")), 4)
        error_text = " ".join(item.value for item in app.error)
        self.assertIn("回答暂时未生成", error_text)
        self.assertNotIn("private-token", error_text)
        self.assertNotIn("13800138000", error_text)
        app.run()
        self.assertEqual(self.agent.call_count, 2)
        self.assertEqual(len(app.session_state["customer_agent_history"]), 2)

        self.agent.side_effect = successful_agent
        self.ask_agent(app, "具体怎么跟进？")
        self.assertEqual(self.agent.call_count, 3)
        self.assertEqual([turn.question for turn in self.agent.call_args.kwargs["history"]], ["先处理什么？"])
        self.assertEqual(len(app.session_state["customer_agent_history"]), 3)
        self.assertIn("customer_report", app.session_state)


if __name__ == "__main__":
    unittest.main()
