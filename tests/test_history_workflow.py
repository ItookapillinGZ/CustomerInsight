import unittest
import pandas as pd
from core.history_workflow import date_scope, suggest_columns, attach_historical_context
from core.customer_report import build_customer_report


class HistoryWorkflowTests(unittest.TestCase):
    def test_suggestions_reuse_visible_mapping_without_guessing_missing_columns(self):
        columns = ["产品", "评价", "日期", "额外文字"]
        self.assertEqual(suggest_columns(columns), {"text_column": "评价", "group_column": "产品", "date_column": "日期"})
        self.assertEqual(suggest_columns(columns, {"text_column": "额外文字"})["text_column"], "额外文字")
        self.assertEqual(suggest_columns(columns, {"group_column": "不存在"})["group_column"], "产品")

    def test_dates_are_explicit_and_keep_missing_invalid_counts(self):
        frame = pd.DataFrame({"日期": ["2026-10-18", "2026/10/19", "2026年10月20日", 45000, "明天", None]})
        scope = date_scope(frame, "日期")
        self.assertEqual((scope["start"], scope["end"]), ("2026-10-18", "2026-10-20"))
        self.assertEqual((scope["valid_rows"], scope["invalid_rows"]), (3, 3))
        self.assertIsNone(date_scope(frame, None)["start"])

    def test_explicit_previous_snapshot_is_bounded_and_does_not_mutate(self):
        source = pd.DataFrame({"评价": ["包装不错"]})
        results = pd.DataFrame([{"source_row": 2, "processing_status": "success", "sentiment": "positive", "category": "product_quality", "priority": "low", "summary": "包装不错"}])
        current = build_customer_report(source, results, "评价")
        old = {"id": "old", "filename": "上一批.xlsx", "created_at": "2026-10-19", "report": current}
        prepared = attach_historical_context(current, old)
        self.assertNotIn("historical_context", current)
        self.assertEqual(prepared["historical_context"]["evidence"], [{"source_row": 2, "text": "包装不错", "group": ""}])
        self.assertTrue(prepared["historical_context"]["comparison"]["warnings"])
        self.assertNotIn("historical_context", attach_historical_context(prepared, None))


if __name__ == "__main__":
    unittest.main()
