"""Offline checks for original-table access used by the reasoning Agent."""

import copy
import json
import unittest

import pandas as pd

from core.agent_table_tools import MAX_PAYLOAD_BYTES, TableToolbox
from core.customer_report import build_customer_report


def _success(number, sentiment="positive", category="product_quality", summary="包装很好"):
    return {"source_row": number, "processing_status": "success", "sentiment": sentiment,
            "category": category, "priority": "low", "summary": summary}


class TableToolboxTests(unittest.TestCase):
    def setUp(self):
        source = pd.DataFrame({
            "评价": ["GOOD [包装]，值得购买", "一般，但客服耐心", "包装[.*]很好", None, "GOOD [包装]，值得购买"],
            "门店": ["东店", "西店", "东店", None, "西店"],
            "商品": ["杯子", "杯子", "餐具", None, "杯子"],
            "金额": [10, 10, 20, None, 10],
        })
        source.attrs["source_rows"] = [2, 7, 11, 18, 27]
        self.report = build_customer_report(source, pd.DataFrame([
            _success(2), _success(7, "neutral", "customer_service", "服务耐心，体验一般"), _success(11),
            {"source_row": 18, "processing_status": "skipped_empty"},
            {"source_row": 27, "processing_status": "skipped_duplicate"},
        ]), "评价", "门店")
        self.seen = []

        def evidence(row):
            names = self.report["detail_columns"]
            number = int(row[names["source_row"]])
            self.seen.append(number)
            return {"evidence_id": f"evidence-{number}", "source_row": number,
                    "untrusted_original_excerpt": str(row["评价"] or "")[:320],
                    "sentiment": row[names["sentiment"]], "category": row[names["category"]],
                    "priority": row[names["priority"]], "summary": row[names["summary"]]}

        self.tools = TableToolbox(self.report, evidence)

    def test_catalogue_is_strict_and_has_only_four_read_only_tools(self):
        catalogue = self.tools.catalogue()
        self.assertEqual({item["name"] for item in catalogue},
                         {"describe_table", "search_reviews", "read_table_rows", "summarize_column"})
        for item in catalogue:
            self.assertFalse(item["parameters"]["additionalProperties"])

    def test_description_lists_original_and_generated_columns_without_cells(self):
        result = self.tools.execute("describe_table", {})
        self.assertEqual(result["row_count"], 5)
        self.assertEqual(result["text_column"], "评价")
        columns = {item["name"]: item["origin"] for item in result["columns"]}
        self.assertEqual(columns["商品"], "original")
        self.assertEqual(columns[self.report["detail_columns"]["source_row"]], "analysis")
        self.assertNotIn("GOOD", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.seen, [])

    def test_search_positive_and_neutral_not_limited_to_negative(self):
        positive = self.tools.execute("search_reviews", {"sentiment": "positive"})
        neutral = self.tools.execute("search_reviews", {"sentiment": "中性"})
        negative = self.tools.execute("search_reviews", {"sentiment": "negative"})
        self.assertEqual([row["source_row"] for row in positive["evidence"]], [2, 11])
        self.assertEqual([row["source_row"] for row in neutral["evidence"]], [7])
        self.assertEqual(negative["matched_count"], 0)

    def test_search_uses_case_insensitive_literal_substring_not_regex(self):
        self.assertEqual(self.tools.execute("search_reviews", {"query": "good"})["matched_count"], 2)
        literal = self.tools.execute("search_reviews", {"query": "[.*]"})
        self.assertEqual([row["source_row"] for row in literal["evidence"]], [11])
        self.assertEqual(self.tools.execute("search_reviews", {"query": "服务耐心"})["matched_count"], 1)

    def test_search_pages_original_rows_and_allows_repeated_calls(self):
        first = self.tools.execute("search_reviews", {"limit": 2})
        second = self.tools.execute("search_reviews", {"limit": 2, "offset": 2})
        last = self.tools.execute("search_reviews", {"limit": 2, "offset": 4})
        self.assertEqual(first["matched_count"], 5)
        self.assertEqual([item["source_row"] for item in first["evidence"]], [2, 7])
        self.assertEqual([item["source_row"] for item in second["evidence"]], [11, 18])
        self.assertEqual([item["source_row"] for item in last["evidence"]], [27])
        self.assertTrue(first["has_more"])
        self.assertFalse(last["has_more"])

    def test_search_filters_group_category_and_priority(self):
        result = self.tools.execute("search_reviews", {
            "sentiment": "positive", "category": "产品质量", "priority": "low", "group_value": "东店",
        })
        self.assertEqual(result["matched_count"], 2)
        missing = self.tools.execute("search_reviews", {"group_value": "（未填写）"})
        self.assertEqual([row["source_row"] for row in missing["evidence"]], [18])

    def test_read_rows_traces_noncontiguous_source_ids_and_requested_original_columns(self):
        result = self.tools.execute("read_table_rows", {"source_rows": [11, 2], "columns": ["商品", "金额"]})
        self.assertEqual([item["source_row"] for item in result["rows"]], [11, 2])
        self.assertEqual(result["rows"][0]["untrusted_fields"], {"商品": "餐具", "金额": 20.0})
        self.assertNotIn("评价", result["rows"][0]["untrusted_fields"])
        self.assertEqual(result["rows"][0]["sentiment"], "正面")
        default = self.tools.execute("read_table_rows", {"source_rows": [2]})
        self.assertEqual(set(default["rows"][0]["untrusted_fields"]), {"评价", "门店"})

    def test_long_review_can_be_read_in_successive_bounded_windows(self):
        long_text = "甲" * 1000 + "乙" * 1000 + "丙" * 25
        self.report["detail"].loc[0, "评价"] = long_text
        tools = TableToolbox(self.report, self.tools._evidence_callback)
        first = tools.execute("read_table_rows", {"source_rows": [2], "columns": ["评价"]})
        second = tools.execute("read_table_rows", {"source_rows": [2], "columns": ["评价"], "character_offset": 1000})
        final = tools.execute("read_table_rows", {"source_rows": [2], "columns": ["评价"], "character_offset": 2000})
        self.assertEqual(first["rows"][0]["untrusted_fields"]["评价"], "甲" * 1000)
        self.assertEqual(second["rows"][0]["untrusted_fields"]["评价"], "乙" * 1000)
        self.assertEqual(final["rows"][0]["untrusted_fields"]["评价"], "丙" * 25)
        self.assertTrue(first["rows"][0]["cells_truncated"])
        self.assertTrue(final["rows"][0]["cells_truncated"])
        self.assertEqual(second["character_offset"], 1000)

    def test_reader_respects_total_utf8_payload_budget(self):
        report = copy.deepcopy(self.report)
        for index in range(20):
            report["detail"][f"字段{index}"] = ["长" * 3000] * 5
        tools = TableToolbox(report, self.tools._evidence_callback)
        result = tools.execute("read_table_rows", {"source_rows": [2, 7, 11, 18, 27],
                                                  "columns": [f"字段{index}" for index in range(20)]})
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), MAX_PAYLOAD_BYTES)
        self.assertTrue(result["payload_truncated"])
        self.assertTrue(any(item["fields_omitted"] for item in result["rows"]))
        self.assertTrue(all(len(value) <= 1000 for item in result["rows"] for value in item["untrusted_fields"].values()))

    def test_column_summary_counts_all_rows_including_skipped_and_null(self):
        result = self.tools.execute("summarize_column", {"column": "商品", "limit": 1})
        self.assertEqual(result["total_rows"], 5)
        self.assertEqual(result["non_null_count"], 4)
        self.assertEqual(result["null_count"], 1)
        self.assertEqual(result["distinct_count"], 2)
        self.assertEqual(result["top_values"][0]["untrusted_value"], "杯子")
        self.assertEqual(result["top_values"][0]["count"], 3)
        self.assertEqual(result["other_non_null_count"], 1)

    def test_summarize_counts_complete_values_before_truncating(self):
        report = copy.deepcopy(self.report)
        report["detail"]["长值"] = ["甲" * 1000 + suffix for suffix in ("a", "b", "a", "c", "a")]
        result = TableToolbox(report, self.tools._evidence_callback).execute("summarize_column", {"column": "长值"})
        self.assertEqual(result["distinct_count"], 3)
        self.assertEqual([item["count"] for item in result["top_values"]], [3, 1, 1])
        self.assertTrue(all(item["value_truncated"] for item in result["top_values"]))

    def test_original_dates_are_returned_as_dates_and_blank_groups_match_report(self):
        report = copy.deepcopy(self.report)
        report["detail"]["日期"] = [pd.Timestamp("2026-10-20")] * 5
        report["detail"].loc[0, "门店"] = "   "
        tools = TableToolbox(report, self.tools._evidence_callback)
        rows = tools.execute("read_table_rows", {"source_rows": [2], "columns": ["日期"]})
        self.assertEqual(rows["rows"][0]["untrusted_fields"]["日期"], "2026-10-20 00:00:00")
        missing = tools.execute("search_reviews", {"group_value": "（未填写）"})
        self.assertEqual(missing["matched_count"], 2)

    def test_unknown_rows_columns_filters_and_executable_interfaces_rejected_safely(self):
        cases = [
            ("read_table_rows", {"source_rows": [3]}),
            ("read_table_rows", {"source_rows": [2], "columns": ["密钥\nSECRET"]}),
            ("read_table_rows", {"source_rows": [2], "columns": [self.report["detail_columns"]["sentiment"]]}),
            ("summarize_column", {"column": "不存在SECRET"}),
            ("search_reviews", {"sentiment": "SECRET"}),
            ("search_reviews", {"category": "SECRET"}),
            ("search_reviews", {"group_value": "SECRET"}),
            ("read_file", {"path": "SECRET"}),
            ("search_reviews", {"sql": "SELECT SECRET"}),
            ("describe_table", {"python": "SECRET"}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError) as error:
                self.tools.execute(name, args)
            self.assertNotIn("SECRET", str(error.exception))

    def test_strict_numeric_arguments_and_limits(self):
        cases = [("search_reviews", {"limit": value}) for value in (True, 1.0, "1", 0, 11)]
        cases += [("search_reviews", {"offset": value}) for value in (False, -1, 1.0)]
        cases += [("read_table_rows", {"source_rows": value}) for value in ([True], [2.0], [2, 2], [], list(range(2, 13)))]
        cases += [("read_table_rows", {"source_rows": [2], "character_offset": value}) for value in (True, -1)]
        cases += [("summarize_column", {"column": "商品", "limit": value}) for value in (True, 0, 21)]
        for name, args in cases:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                self.tools.execute(name, args)

    def test_tools_never_modify_input_report_and_snapshot_survives_later_changes(self):
        before = copy.deepcopy(self.report)
        self.tools.execute("search_reviews", {"query": "包装"})
        self.tools.execute("read_table_rows", {"source_rows": [2], "columns": ["商品"]})
        self.tools.execute("summarize_column", {"column": "商品"})
        pd.testing.assert_frame_equal(before["detail"], self.report["detail"])
        self.assertEqual(before["detail_columns"], self.report["detail_columns"])
        self.report["detail"].loc[0, "商品"] = "后来修改"
        result = self.tools.execute("read_table_rows", {"source_rows": [2], "columns": ["商品"]})
        self.assertEqual(result["rows"][0]["untrusted_fields"]["商品"], "杯子")


if __name__ == "__main__":
    unittest.main()
