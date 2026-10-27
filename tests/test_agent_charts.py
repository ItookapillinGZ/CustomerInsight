"""Offline chart materialization, transport compatibility and real progress."""

import copy
import json
import unittest
from unittest.mock import patch

import pandas as pd
from pydantic import ValidationError

from core.agent_charts import AgentChart, AgentChartRequest, build_agent_charts
from core.analysis_agent import AnalysisAgentResult, ReportAgentContext, report_fingerprint, run_analysis_agent
from core.customer_report import build_customer_report
from core.history_workflow import attach_historical_context
from tests.test_analysis_agent import DynamicAgentClient, FakeControl, query, query_decision, success


class AgentChartTests(unittest.TestCase):
    def setUp(self):
        source = pd.DataFrame({"评价": ["送货太慢", "品质很好", "客服推诿", "还行", "失败评价"],
                               "产品": ["A", "A", "B", "B", "B"]})
        results = pd.DataFrame([success(2), success(3, sentiment="positive"),
                                success(4), success(5, sentiment="neutral"),
                                {"source_row": 6, "processing_status": "failed", "error_message": "offline"}])
        self.report = build_customer_report(source, results, "评价", "产品")
        config = patch.multiple("core.analysis_agent.Config", API_MODE="chat_completions", CHAT_OUTPUT_MODE="json_object",
            CHAT_TOKEN_PARAMETER="max_completion_tokens", MAX_OUTPUT_TOKENS=1000, OPENAI_REASONING_EFFORT=None,
            REQUEST_TIMEOUT_SECONDS=5)
        config.start()
        self.addCleanup(config.stop)

    def chart(self, source):
        return build_agent_charts(self.report, [{"source": source}])[0]

    def run_agent(self, client, question="这些评价主要讨论什么？", **kwargs):
        return run_analysis_agent(self.report, question, client=client, control=FakeControl(), **kwargs)

    def test_old_model_json_without_chart_request_keeps_plain_answer(self):
        result = self.run_agent(DynamicAgentClient([DynamicAgentClient.answer]))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.charts, [])
        old = result.model_dump(mode="json")
        del old["charts"]
        self.assertEqual(AnalysisAgentResult.model_validate(old).charts, [])

    def test_selected_chart_uses_current_labels_and_survives_json_roundtrip(self):
        def answer(parameters):
            value = DynamicAgentClient.answer(parameters)
            value["response"]["chart_requests"] = [{"source": "sentiment", "limit": 5}]
            return value
        result = self.run_agent(DynamicAgentClient([answer]))
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.charts), 1)
        restored = AnalysisAgentResult.model_validate_json(result.model_dump_json())
        self.assertEqual(restored.charts, result.charts)
        chart = result.charts[0]
        self.assertEqual({point.label: point.value for point in chart.data}, {"正面": 1, "负面": 2, "中性": 1})
        self.assertTrue(all(point.denominator == 4 for point in chart.data))
        self.assertIn("整批", chart.title)
        self.assertIn("不限于", chart.denominator_note)

    def test_forged_model_values_code_columns_and_batch_ids_are_rejected(self):
        for key, value in (("data", [999]), ("value", 999), ("code", "import os"),
                           ("column", "客户姓名"), ("batch_id", "another-user")):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                build_agent_charts(self.report, [{"source": "sentiment", key: value}])
        with self.assertRaises(ValidationError):
            AgentChartRequest(source="arbitrary_sql")
        with self.assertRaises(ValidationError):
            AgentChartRequest(source="sentiment", limit=True)
        with self.assertRaises(ValueError):
            build_agent_charts(self.report, [{"source": "sentiment"}] * 3)

    def test_chart_validation_rejects_inconsistent_and_nonfinite_saved_values(self):
        value = self.chart("sentiment").model_dump(mode="json")
        value["data"][0]["value"] = 999
        with self.assertRaises(ValidationError):
            AgentChart.model_validate(value)
        value["data"][0]["value"] = float("nan")
        with self.assertRaises(ValidationError):
            AgentChart.model_validate(value)

    def test_forged_report_aggregates_cannot_replace_actual_label_counts(self):
        self.report["metrics"]["negative_rows"] = 999
        self.report["sentiment_counts"].loc[:, "数量"] = 999
        self.report["grouped"].loc[:, "负面占比"] = 999
        self.assertEqual({point.label: point.value for point in self.chart("sentiment").data}["负面"], 2)
        self.assertEqual([point.value for point in self.chart("group_negative_rate").data], [50, 50])

    def test_group_percentages_show_numerators_and_individual_denominators(self):
        chart = self.chart("group_negative_rate")
        self.assertEqual(chart.unit, "%")
        self.assertEqual([(point.label, point.numerator, point.denominator, point.value) for point in chart.data],
                         [("A", 1, 2, 50), ("B", 1, 2, 50)])

    def test_missing_group_column_and_one_small_group_produce_no_misleading_chart(self):
        self.report["group_column"] = None
        self.assertEqual(build_agent_charts(self.report, [{"source": "group_negative_rate"}]), [])
        self.report["group_column"] = "产品"
        self.report["detail"].loc[0, "产品"] = "C"
        self.assertEqual(build_agent_charts(self.report, [{"source": "group_negative_rate"}]), [])

    def test_zero_denominator_missing_column_and_failed_stale_labels_are_excluded(self):
        names = self.report["detail_columns"]
        self.report["detail"].loc[:, names["processing_status"]] = "调用失败"
        self.assertEqual(build_agent_charts(self.report, [{"source": "sentiment"}]), [])
        self.report["detail"].drop(columns=[names["sentiment"]], inplace=True)
        self.assertEqual(build_agent_charts(self.report, [{"source": "sentiment"}]), [])

    def test_topic_values_use_distinct_current_rows_not_claimed_counts_or_other_rows(self):
        self.report["topics"] = [{"topic": "配送延迟", "kind": "issue", "review_count": 999,
                                  "source_rows": [2, 2, 3, 6, 999]}]
        point = self.chart("topics").data[0]
        self.assertEqual((point.numerator, point.denominator), (2, 4))
        self.assertEqual(build_agent_charts({**self.report, "topics": []}, [{"source": "topics"}]), [])

    def test_inference_is_only_for_clear_supported_chart_question(self):
        self.assertEqual(build_agent_charts(self.report, question="分析一下"), [])
        self.assertEqual(build_agent_charts(self.report, question="画出按日期的销售趋势图"), [])
        self.assertEqual(build_agent_charts(self.report, question="各产品的差评占比画个图")[0].source,
                         "group_negative_rate")
        result = self.run_agent(DynamicAgentClient([DynamicAgentClient.answer]), "画出情感分布图")
        self.assertEqual((result.model_calls, len(result.charts)), (1, 1))

    def test_only_caller_selected_comparable_previous_batch_is_charted(self):
        self.assertEqual(build_agent_charts(self.report, [{"source": "history_sentiment"}]), [])
        previous = copy.deepcopy(self.report)
        names = previous["detail_columns"]
        previous["detail"].loc[:, names["sentiment"]] = previous["detail"][names["sentiment"]].replace("负面", "正面")
        # Use a real locally recomputed prior report, not an invented comparison.
        source = pd.DataFrame({"评价": ["很好", "还行"], "产品": ["A", "B"]})
        previous = build_customer_report(source, pd.DataFrame([success(2, sentiment="positive"),
                                                              success(3, sentiment="neutral")]), "评价", "产品")
        report = attach_historical_context(self.report, {"id": "selected-batch", "filename": "previous.xlsx",
                                                        "created_at": "2026-10-01", "report": previous})
        charts = build_agent_charts(report, [{"source": "history_sentiment"}])
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0].scope, "selected_history")
        points = {(point.series, point.label): point for point in charts[0].data}
        self.assertEqual((points["本批", "负面"].numerator, points["本批", "负面"].denominator), (2, 4))
        self.assertEqual((points["上批", "负面"].numerator, points["上批", "负面"].denominator), (0, 2))
        comparison = report["historical_context"]["comparison"]
        comparison["basic_comparable"] = False
        self.assertEqual(build_agent_charts(report, [{"source": "history_sentiment"}]), [])
        comparison["basic_comparable"] = True
        comparison["distributions"]["sentiment"][0]["previous_count"] = 999
        self.assertEqual(build_agent_charts(report, [{"source": "history_sentiment"}]), [])

    def test_insufficient_answer_does_not_auto_draw_an_unrelated_full_report_chart(self):
        answer = {"action": "answer", "queries": [], "response": {"answer": "表内没有销售额，无法分析。",
                  "answer_type": "insufficient", "metric_refs": [], "evidence_refs": [],
                  "chart_requests": [{"source": "sentiment"}]}}
        result = self.run_agent(DynamicAgentClient([answer]), "画销售额变化图")
        self.assertEqual((result.status, result.charts), ("completed", []))

    def test_progress_tracks_actual_tools_calls_and_completion_without_table_text(self):
        events = []
        client = DynamicAgentClient([query_decision(query("search_reviews", query="送货", limit=2)),
                                     DynamicAgentClient.answer])
        result = self.run_agent(client, progress_callback=events.append)
        self.assertEqual(result.status, "completed")
        self.assertEqual([item["model_call"] for item in events if item["stage"] == "model"], [1, 2])
        self.assertEqual([item["tool"] for item in events if item["stage"] == "tool"], result.tool_trace)
        self.assertEqual(events[-1]["stage"], "completed")
        self.assertNotIn("送货太慢", json.dumps(events, ensure_ascii=False))

    def test_failed_request_emits_failure_and_broken_observer_cannot_add_requests(self):
        events = []
        result = self.run_agent(DynamicAgentClient(raised=TimeoutError()), progress_callback=events.append)
        self.assertEqual((result.status, events[-1]["stage"]), ("fallback", "failed"))
        def broken(event):
            raise RuntimeError("observer broke")
        result = self.run_agent(DynamicAgentClient([DynamicAgentClient.answer]), progress_callback=broken)
        self.assertEqual((result.status, result.model_calls), ("completed", 1))

    def test_taxonomy_version_is_fingerprinted_only_when_present(self):
        self.report.pop("taxonomy_version", None)
        legacy = report_fingerprint(self.report)
        self.assertEqual(legacy, report_fingerprint(copy.deepcopy(self.report)))
        self.report["taxonomy_version"] = "v-next"
        self.assertNotEqual(legacy, report_fingerprint(self.report))
        self.assertEqual(ReportAgentContext(self.report).query("get_review_topics", {})["taxonomy_version"], "v-next")


if __name__ == "__main__":
    unittest.main()
