"""Offline safe HTML, checked graph data and scoped source evidence exports."""

import copy
from html.parser import HTMLParser
import re
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pandas as pd

from core.agent_brief import render_agent_brief
from core.agent_charts import build_agent_charts
from core.analysis_agent import AgentPoint, AnalysisAgentResult, report_fingerprint
from core.customer_report import build_customer_report
from core.demo_workspace import load_demo_workspace
from core.history_workflow import attach_historical_context
from tests.test_analysis_agent import success


class _Tags(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.tags = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class AgentBriefTests(unittest.TestCase):
    def setUp(self):
        source = pd.DataFrame({"评价": ["送货太慢", "产品很好", "客服推诿", "还行"], "商品": ["A", "A", "B", "B"]})
        results = pd.DataFrame([success(2), success(3, sentiment="positive"), success(4), success(5, sentiment="neutral")])
        report = build_customer_report(source, results, "评价", "商品")
        report["period"] = {"start": "2026-10-01", "end": "2026-10-07", "valid_rows": 4}
        previous = build_customer_report(pd.DataFrame({"评价": ["上批配送准时", "上批体验一般"], "商品": ["A", "B"]}),
                                        pd.DataFrame([success(2, sentiment="positive"), success(3, sentiment="neutral")]), "评价", "商品")
        previous["period"] = {"start": "2026-09-21", "end": "2026-09-28", "valid_rows": 2}
        self.report = attach_historical_context(report, {"id": "selected", "filename": "上批评价.xlsx",
                                                       "created_at": "2026-10-01", "report": previous})

    def answer(self, **updates):
        value = dict(status="completed", question="配送问题怎么处理？", conclusion="建议先核对履约节点，再安排跟进。这是待验证的建议。",
                     conclusion_metrics=["负面评价：2 条"], findings=[], actions=[],
                     scope_note="仅限当前报告与用户明确选定上批。", tool_trace=["get_overview"],
                     model_calls=1, tool_calls=1, total_tokens=20, usage_reported_requests=1,
                     report_fingerprint=report_fingerprint(self.report), fallback_reason=None,
                     answer_origin="llm", evidence_rows=[2], historical_evidence=[])
        value.update(updates)
        return AnalysisAgentResult.model_validate(value)

    def test_selected_answers_current_and_previous_evidence_dates_and_scope(self):
        answer = self.answer(historical_evidence=[{"batch_id": "selected", "filename": "上批评价.xlsx",
                                                  "source_row": 2, "text": "配送准时"}])
        content = render_agent_brief(self.report, [answer]).decode("utf-8")
        for text in (answer.question, answer.conclusion, "送货太慢", "上批配送准时", "上批评价.xlsx",
                     "2026-10-01 至 2026-10-07", "2026-09-21 至 2026-09-28", "LLM 生成回答", "两个导入批次"):
            self.assertIn(text, content)
        self.assertNotIn("客服推诿", content)
        self.assertNotIn("上批体验一般", content)
        self.assertIn("本批和上批原始行号相互独立", content)

    def test_legacy_points_are_included_and_their_rows_are_checked(self):
        point = AgentPoint(title="客户跟进", narrative="先确认具体服务环节。", metrics=[], evidence_rows=[4])
        content = render_agent_brief(self.report, [self.answer(findings=[point])]).decode()
        self.assertIn("客户跟进", content)
        self.assertIn("客服推诿", content)
        point.evidence_rows = [999]
        with self.assertRaises(ValueError):
            render_agent_brief(self.report, [self.answer(findings=[point])])

    def test_full_html_escaping_and_no_executable_or_external_resources(self):
        payload = '<script>alert(1)</script><img src="https://example.test/pixel" onerror="alert(2)"> & "hi"'
        self.report["detail"].loc[0, "评价"] = payload
        answer = self.answer(question=payload, conclusion=payload, conclusion_metrics=[payload], scope_note=payload)
        content = render_agent_brief(self.report, [answer]).decode()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", content)
        parsed = _Tags(content)
        self.assertTrue(all(tag not in {"script", "img", "iframe", "object", "embed", "link"} for tag, _ in parsed.tags))
        for _, attrs in parsed.tags:
            self.assertFalse(any(name.startswith("on") for name in attrs))
            self.assertFalse(any(name in {"src", "href", "action"} for name in attrs))
        csp = next(attrs["content"] for tag, attrs in parsed.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        for directive in ("default-src 'none'", "script-src 'none'", "connect-src 'none'", "img-src 'none'", "form-action 'none'"):
            self.assertIn(directive, csp)

    def test_cached_chart_numbers_labels_titles_are_recomputed_from_report(self):
        graph = build_agent_charts(self.report, [{"source": "sentiment"}])[0].model_dump()
        graph["title"] = "FAKE CHART TITLE"
        for point in graph["data"]:
            point.update(label="FAKE LABEL", value=999, numerator=999, denominator=1000)
        content = render_agent_brief(self.report, [self.answer(charts=[graph])]).decode()
        self.assertNotIn("FAKE CHART TITLE", content)
        self.assertNotIn("FAKE LABEL", content)
        self.assertNotIn("999", content)
        self.assertIn("整批有效评价的情感分布", content)
        self.assertIn("2 条 · 50.0%", content)
        self.assertIn("不限于本轮关注的原文子集", content)
        graphs = re.findall(r"<svg\b.*?</svg>", content, re.S)
        self.assertEqual(len(graphs), 1)
        ET.fromstring(graphs[0])

    def test_percent_graphs_retain_individual_denominators(self):
        charts = build_agent_charts(self.report, [{"source": "group_negative_rate"}, {"source": "history_sentiment"}])
        content = render_agent_brief(self.report, [self.answer(charts=charts)]).decode()
        self.assertIn("50.0% · 1/2 条", content)
        self.assertIn("50.0% · 2/4 条", content)
        self.assertIn("0.0% · 0/2 条", content)
        self.assertIn("各组有效评价数分别作分母", content)
        self.assertIn("分别以各批有效评价为分母", content)

    def test_mismatched_fingerprint_and_unresolved_reference_are_rejected(self):
        with self.assertRaises(ValueError):
            render_agent_brief(self.report, [self.answer(report_fingerprint="other-report")])
        with self.assertRaises(ValueError):
            render_agent_brief(self.report, [self.answer(conclusion="[[row:unknown]]")])
        answer = self.answer()
        changed = copy.deepcopy(self.report)
        changed["detail"].loc[0, "评价"] = "a different source"
        with self.assertRaises(ValueError):
            render_agent_brief(changed, [answer])

    def test_current_and_previous_rows_cannot_be_forged_or_swapped(self):
        with self.assertRaises(ValueError):
            render_agent_brief(self.report, [self.answer(evidence_rows=[999])])
        base = {"batch_id": "selected", "filename": "上批评价.xlsx", "source_row": 2, "text": "配送准时"}
        for key, value in (("batch_id", "another-user-batch"), ("filename", "other.xlsx"),
                           ("source_row", 999), ("text", "伪造上批引文")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                render_agent_brief(self.report, [self.answer(historical_evidence=[{**base, key: value}])])

    def test_missing_selected_previous_batch_rejects_a_history_reference(self):
        self.report.pop("historical_context")
        value = {"batch_id": "selected", "filename": "上批评价.xlsx", "source_row": 2, "text": "配送准时"}
        with self.assertRaises(ValueError):
            render_agent_brief(self.report, [self.answer(historical_evidence=[value])])

    def test_limits_and_model_json_roundtrip(self):
        answer = self.answer()
        for answers in ([], [answer] * 11, (answer,)):
            with self.subTest(count=len(answers)), self.assertRaises(ValueError):
                render_agent_brief(self.report, answers)
        self.assertEqual(render_agent_brief(self.report, [answer]), render_agent_brief(self.report, [answer.model_dump(mode="json")]))

    def test_real_demo_replay_export_is_offline_predefined_and_never_llm(self):
        with patch("core.analysis_agent.get_openai_client", side_effect=AssertionError("no model client")) as client:
            workspace = load_demo_workspace()
            content = render_agent_brief(workspace["current_report"], workspace["replay_turns"]).decode()
        client.assert_not_called()
        self.assertEqual(content.count("离线预置示例 · 未调用模型"), 2)
        self.assertNotIn("LLM 生成回答", content)
        self.assertIn("数据与初始标签为离线合成示例", content)
        self.assertIn("本批原文依据", content)
        self.assertIn("选定上批原文依据", content)
        self.assertIn("不代表真实商户或模型准确率", content)

    def test_legacy_and_failed_answers_are_not_labelled_as_llm(self):
        content = render_agent_brief(self.report, [self.answer(answer_origin="legacy_template", model_calls=0),
                                                  self.answer(answer_origin="fallback", status="fallback", model_calls=1)]).decode()
        self.assertIn("规则模板 · 非 LLM 回答", content)
        self.assertIn("未完成的回答", content)
        self.assertNotIn("LLM 生成回答", content)

    def test_zero_call_failed_synthetic_answer_is_not_mislabelled_as_replay(self):
        self.report["data_origin"] = "synthetic"
        content = render_agent_brief(self.report, [self.answer(answer_origin="fallback", status="fallback", model_calls=0)]).decode()
        self.assertIn("未完成的回答", content)
        self.assertNotIn("离线预置示例 · 未调用模型", content)


if __name__ == "__main__":
    unittest.main()
