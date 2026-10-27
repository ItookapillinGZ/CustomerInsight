"""Drill-down selects actual rows and recommendations respect available fields."""
import unittest

import pandas as pd

from core.customer_report import build_customer_report
from core.report_interactions import chart_focus, focus_label, focus_question, focus_rows, recommended_questions, update_chart_focus


def report_fixture():
    source = pd.DataFrame({"产品": ["杯子", "杯子", "水壶"], "评价": ["配送慢", "包装很好", "杯口破损"]})
    results = pd.DataFrame([
        {"source_row": 2, "processing_status": "success", "sentiment": "negative", "category": "delivery", "priority": "medium", "summary": "配送慢"},
        {"source_row": 3, "processing_status": "success", "sentiment": "positive", "category": "product_quality", "priority": "low", "summary": "包装很好"},
        {"source_row": 4, "processing_status": "failed", "error_message": "未分析"},
    ])
    return build_customer_report(source, results, "评价", "产品")


class ReportInteractionTests(unittest.TestCase):
    def test_drilldown_matches_only_real_rows_and_preserves_whole_batch(self):
        report = report_fixture()
        shown = focus_rows(report, {"field": "group", "value": "杯子", "negative_only": True})
        self.assertEqual(shown[report["detail_columns"]["source_row"]].tolist(), [2])
        self.assertEqual(report["metrics"]["total_rows"], 3)
        shown.iloc[0, 0] = "changed"
        self.assertEqual(report["detail"].iloc[0, 0], "杯子")

    def test_unknown_focus_fails_closed_and_empty_question_is_not_submitted(self):
        report = report_fixture()
        for focus in ({"field": "group", "value": "missing"}, {"field": "sql", "value": "select"}, {"field": "topic", "value": "unknown"}):
            self.assertTrue(focus_rows(report, focus).empty)
            self.assertEqual(focus_question(report, focus), "")

    def test_topic_selection_uses_source_rows_not_text_substring(self):
        report = report_fixture()
        report["topics"] = [{"key": "issue:配送延迟", "topic": "配送延迟", "source_rows": [2]}]
        focus = {"field": "topic", "value": "issue:配送延迟"}
        self.assertEqual(len(focus_rows(report, focus)), 1)
        self.assertIn("配送延迟", focus_label(report, focus))
        self.assertIn("原表第 2 行", focus_question(report, focus))
        self.assertIn("整批统计", focus_question(report, focus))

    def test_suggestions_only_offer_group_and_history_when_available(self):
        report = report_fixture()
        questions = recommended_questions(report)
        self.assertEqual(len(questions), 3)
        self.assertTrue(any("各产品或门店" in question for question in questions))
        self.assertFalse(any("上一批" in question for question in questions))
        report["historical_context"] = {"batch": {"id": "demo"}}
        self.assertTrue(any("上一批" in question for question in recommended_questions(report)))
        report["grouped"] = pd.DataFrame()
        report["group_column"] = None
        self.assertFalse(any("各产品或门店" in question for question in recommended_questions(report)))
        self.assertFalse(any("日期" in question for question in recommended_questions(report)))

    def test_praise_topic_is_not_presented_as_a_defect(self):
        report = report_fixture()
        report["top_topics"] = [{"topic": "包装完整", "kind": "praise"}]
        first = recommended_questions(report)[0]
        self.assertIn("包装完整", first)
        self.assertNotIn("说明问题", first)
        self.assertIn("反馈", first)

    def test_chart_events_accept_only_known_selection_shape(self):
        self.assertEqual(chart_focus({"selection": {"ci_pick": [{"情感": "负面"}]}}, "sentiment", "情感"),
                         {"field": "sentiment", "value": "负面", "negative_only": False})
        for event in (None, {}, {"selection": {"ci_pick": {"情感": "负面"}}}, {"selection": {"ci_pick": [{"情感": 2}]}}):
            self.assertIsNone(chart_focus(event, "sentiment", "情感"))

    def test_chart_clear_then_reselect_and_topic_focus_do_not_get_stuck(self):
        choice = {"field": "sentiment", "value": "负面"}
        topic = {"field": "topic", "value": "issue:配送延迟"}
        seen = {}
        self.assertEqual(update_chart_focus("sentiment", choice, None, seen), choice)
        self.assertEqual(update_chart_focus("sentiment", choice, topic, seen), topic)
        self.assertIsNone(update_chart_focus("sentiment", None, choice, seen))
        self.assertEqual(update_chart_focus("sentiment", choice, topic, seen), choice)
        self.assertEqual(update_chart_focus("sentiment", None, topic, seen), topic)


if __name__ == "__main__":
    unittest.main()
