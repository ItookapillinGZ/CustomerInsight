import io
from html import escape
from html.parser import HTMLParser
import re
import unittest
from xml.etree import ElementTree

import pandas as pd

from core.customer_report import build_customer_report, export_customer_csv, export_customer_html


class ReportMarkup(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def successful(row, sentiment="negative", category="delivery", priority="high", summary="配送慢"):
    return {
        "source_row": row, "processing_status": "success", "sentiment": sentiment,
        "category": category, "priority": priority, "summary": summary, "latency_ms": 12,
    }


class CustomerReportTests(unittest.TestCase):
    def setUp(self):
        self.source = pd.DataFrame({
            "商品": ["A", "A", "B", "A", "B", None],
            "客户评价": ["一直没发货", "包装一般", "很好用", " 一直没发货 ", "无法查询", None],
            "订单号": ["001", "002", "003", "004", "005", "006"],
        }, index=[8, 9, 20, 21, 50, 60])
        self.results = pd.DataFrame([
            {"source_row": 7, "processing_status": "skipped_empty", "error_message": "空白已跳过"},
            {"source_row": 6, "processing_status": "failed", "error_message": "服务未连接", "sentiment": "neutral"},
            {"source_row": 5, "processing_status": "skipped_duplicate", "duplicate_of": "row-2"},
            successful(4, "positive", "product_quality", "low", "质量好"),
            successful(3, "neutral", "other", "medium", "包装一般"),
            successful(2),
        ])

    def test_join_uses_source_rows_and_keeps_all_original_fields_and_order(self):
        report = build_customer_report(self.source, self.results, "客户评价", "商品")
        detail, names = report["detail"], report["detail_columns"]
        pd.testing.assert_frame_equal(detail[self.source.columns], self.source.reset_index(drop=True))
        self.assertEqual(detail[names["source_row"]].tolist(), list(range(2, 8)))
        self.assertEqual(detail[names["sentiment"]].tolist(), ["负面", "中性", "正面", "负面", "", ""])
        self.assertEqual(detail.loc[4, names["error_message"]], "服务未连接")
        self.assertEqual(detail.loc[3, names["duplicate_of"]], "row-2")

    def test_failure_and_skipped_rows_are_excluded_from_success_denominator(self):
        report = build_customer_report(self.source, self.results, "客户评价")
        metrics = report["metrics"]
        self.assertEqual(metrics["total_rows"], 6)
        self.assertEqual(metrics["success_rows"], 4)
        self.assertEqual(metrics["model_success_rows"], 3)
        self.assertEqual(metrics["reused_rows"], 1)
        self.assertEqual(metrics["unresolved_duplicate_rows"], 0)
        self.assertEqual(metrics["failed_rows"], 1)
        self.assertEqual(metrics["skipped_duplicate_rows"], 1)
        self.assertEqual(metrics["skipped_empty_rows"], 1)
        self.assertEqual(metrics["neutral_rows"], 1)
        self.assertEqual(metrics["negative_rows"], 2)
        self.assertEqual(metrics["high_priority_rows"], 2)
        self.assertAlmostEqual(metrics["negative_rate"], 0.5)
        self.assertAlmostEqual(metrics["success_rate"], 2 / 3)
        self.assertEqual(report["sentiment_counts"]["数量"].sum(), 4)
        self.assertAlmostEqual(report["sentiment_counts"]["占成功样本比例"].sum(), 1.0)
        self.assertEqual(report["problem_ranking"].iloc[0]["问题类别"], "配送物流")
        self.assertEqual(report["problem_ranking"].iloc[0]["数量"], 2)
        self.assertTrue(any("同文跨产品" in note for note in report["notes"]))

    def test_group_rates_use_successful_group_rows_and_missing_group_is_visible(self):
        report = build_customer_report(self.source, self.results, "客户评价", "商品")
        grouped = report["grouped"].set_index("分组")
        self.assertEqual(grouped.loc["A", "原始记录数"], 3)
        self.assertEqual(grouped.loc["A", "成功分析数"], 3)
        self.assertAlmostEqual(grouped.loc["A", "负面占比"], 2 / 3)
        self.assertEqual(grouped.loc["B", "成功分析数"], 1)
        self.assertEqual(grouped.loc["B", "负面占比"], 0)
        self.assertEqual(grouped.loc["（未填写）", "原始记录数"], 1)
        self.assertEqual(grouped.loc["（未填写）", "高优先级占比"], 0)
        evidence = report["negative_examples"]
        self.assertEqual(evidence["客户评价"].tolist(), ["一直没发货", " 一直没发货 "])
        self.assertEqual(evidence["订单号"].tolist(), ["001", "004"])

    def test_same_review_in_different_stores_reuses_labels_but_counts_each_original_record(self):
        source = pd.DataFrame({"门店": ["A", "B", "C"], "评价": [" 服务很差 ", "服务很差", "服务很差"]})
        results = pd.DataFrame([
            {**successful(2, category="customer_service"), "feedback_id": "row-2"},
            {"source_row": 3, "processing_status": "skipped_duplicate", "duplicate_of": "row-2", "latency_ms": 0},
            {"source_row": 4, "processing_status": "skipped_duplicate", "duplicate_of": "row-2", "latency_ms": 0},
        ])
        original = results.copy(deep=True)
        report = build_customer_report(source, results, "评价", "门店")
        self.assertEqual(report["metrics"]["success_rows"], 3)
        self.assertEqual(report["metrics"]["model_success_rows"], 1)
        self.assertEqual(report["metrics"]["reused_rows"], 2)
        self.assertEqual(report["metrics"]["negative_rows"], 3)
        grouped = report["grouped"].set_index("分组")
        for store in ["A", "B", "C"]:
            self.assertEqual(grouped.loc[store, "负面评价数"], 1)
            self.assertEqual(grouped.loc[store, "成功分析数"], 1)
        names = report["detail_columns"]
        detail = report["detail"]
        self.assertEqual(detail[names["processing_status"]].tolist(), ["分析成功", "重复评价（复用分析）", "重复评价（复用分析）"])
        self.assertEqual(detail[names["original_processing_status"]].tolist(), ["success", "skipped_duplicate", "skipped_duplicate"])
        self.assertIn("未新增模型请求", detail.loc[1, names["result_source"]])
        self.assertEqual(detail.loc[1, names["duplicate_of"]], "row-2")
        self.assertEqual(detail.loc[1, names["latency_ms"]], 0)
        pd.testing.assert_frame_equal(results, original)

    def test_duplicate_of_failed_first_record_remains_unresolved(self):
        source = pd.DataFrame({"门店": ["A", "B"], "评价": ["服务很差", "服务很差"]})
        results = pd.DataFrame([
            {"source_row": 2, "processing_status": "failed", "error_message": "服务未连接"},
            {"source_row": 3, "processing_status": "skipped_duplicate", "duplicate_of": "row-2"},
        ])
        report = build_customer_report(source, results, "评价", "门店")
        metrics = report["metrics"]
        self.assertEqual(metrics["success_rows"], 0)
        self.assertEqual(metrics["model_success_rows"], 0)
        self.assertEqual(metrics["reused_rows"], 0)
        self.assertEqual(metrics["unresolved_duplicate_rows"], 1)
        self.assertEqual(metrics["failed_rows"], 1)
        self.assertEqual(report["sentiment_counts"]["数量"].sum(), 0)
        self.assertEqual(report["grouped"]["成功分析数"].sum(), 0)
        self.assertTrue(report["negative_examples"].empty)

    def test_duplicate_reference_must_match_exact_first_text_and_cannot_follow_chains(self):
        source = pd.DataFrame({"评价": ["一样", "不同", "一样", "一样"]})
        for reference in ["row-3", "missing", "row-4"]:
            with self.subTest(reference=reference):
                results = pd.DataFrame([
                    successful(2), successful(3),
                    {"source_row": 4, "processing_status": "skipped_duplicate", "duplicate_of": "row-2"},
                    {"source_row": 5, "processing_status": "skipped_duplicate", "duplicate_of": reference},
                ])
                report = build_customer_report(source, results, "评价")
                self.assertEqual(report["metrics"]["reused_rows"], 1)
                self.assertEqual(report["metrics"]["unresolved_duplicate_rows"], 1)
                self.assertEqual(report["metrics"]["success_rows"], 3)

    def test_partial_results_remain_pending_and_all_failure_has_zero_rates(self):
        report = build_customer_report(self.source, self.results.iloc[0:2], "客户评价")
        self.assertEqual(report["metrics"]["success_rows"], 0)
        self.assertEqual(report["metrics"]["pending_rows"], 4)
        self.assertEqual(report["metrics"]["negative_rate"], 0)
        self.assertEqual(report["metrics"]["neutral_rate"], 0)
        self.assertEqual(report["sentiment_counts"]["数量"].sum(), 0)
        self.assertTrue(report["negative_examples"].empty)
        self.assertIn("暂无符合条件", export_customer_html(report).decode())
        empty = build_customer_report(self.source.iloc[:0], pd.DataFrame(), "客户评价")
        self.assertEqual(empty["metrics"]["total_rows"], 0)
        self.assertEqual(empty["metrics"]["success_rate"], 0)

    def test_duplicate_unmatched_invalid_results_are_rejected(self):
        for result in [
            pd.DataFrame([successful(2), successful(2)]),
            pd.DataFrame([successful(200)]),
            pd.DataFrame([{**successful(2), "processing_status": "unknown"}]),
            pd.DataFrame([{**successful(2), "sentiment": None}]),
            pd.DataFrame([{**successful(2), "summary": " "}]),
            pd.DataFrame([{"processing_status": "success"}]),
        ]:
            with self.subTest(result=result.to_dict()):
                with self.assertRaises(ValueError):
                    build_customer_report(self.source, result, "客户评价")

    def test_original_chinese_column_names_are_preserved_on_collision(self):
        source = self.source.copy()
        source["情感"] = "用户自己的字段"
        report = build_customer_report(source, self.results, "客户评价")
        self.assertEqual(report["detail"]["情感"].tolist(), ["用户自己的字段"] * 6)
        self.assertEqual(report["detail_columns"]["sentiment"], "情感（分析1）")
        self.assertEqual(report["detail"]["情感（分析1）"].iloc[0], "负面")

    def test_html_escapes_user_cells_headers_groups_and_model_text(self):
        malicious = '<img src=x onerror="alert(1)"><script>alert(1)</script>'
        source = pd.DataFrame({"<svg onload=alert(1)>": [malicious], "产品": ['<a href="https://evil.invalid">X</a>']})
        result = pd.DataFrame([successful(2, summary=malicious)])
        report = build_customer_report(source, result, "<svg onload=alert(1)>", "产品")
        html = export_customer_html(report).decode("utf-8")
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<img", html.lower())
        self.assertNotIn("<svg onload", html.lower())
        self.assertNotIn('<a href="https://evil.invalid">', html.lower())
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;svg", html)
        self.assertIn("Content-Security-Policy", html)
        markup = ReportMarkup(html)
        self.assertNotIn("script", [tag for tag, _ in markup.elements])
        for tag, attributes in markup.elements:
            self.assertFalse(any(name.lower().startswith("on") for name in attributes))
            self.assertNotIn("src", attributes)
            if "href" in attributes:
                self.assertTrue(attributes["href"].startswith("#"), (tag, attributes))

    def test_insights_use_original_frequency_and_reference_existing_rows(self):
        report = build_customer_report(self.source, self.results, "客户评价", "商品")
        insights = report["insights"]
        self.assertTrue(any("2 条负面评价" in item["title"] for item in insights))
        main_issue = next(item for item in insights if "差评主要涉及" in item["title"])
        self.assertIn("配送物流", main_issue["title"])
        self.assertIn("2 条负面评价", main_issue["body"])
        self.assertIn("不是自动主题聚类", main_issue["body"])
        self.assertEqual(main_issue["evidence_rows"], [2, 5])
        rows = set(report["detail"][report["detail_columns"]["source_row"]])
        self.assertTrue(all(set(item["evidence_rows"]).issubset(rows) for item in insights))

    def test_executive_summary_uses_covered_denominator_and_reused_original_records(self):
        report = build_customer_report(self.source, self.results, "客户评价")
        summary = report["executive_summary"]
        self.assertIn("有效覆盖 4/6 条评价", summary)
        self.assertIn("正面占 25.0%", summary)
        self.assertIn("负面占 50.0%", summary)
        self.assertIn("配送物流最多（2 条）", summary)
        self.assertIn("优先复核 2 条高优先级评价", summary)
        self.assertIn("仅反映已覆盖部分", summary)
        html = export_customer_html(report).decode()
        self.assertIn(escape(summary), html)
        self.assertLess(html.index(escape(summary)), html.index('id="distribution"'))

    def test_executive_summary_has_no_success_claim_when_empty_or_all_failed(self):
        empty = build_customer_report(self.source.iloc[:0], pd.DataFrame(), "客户评价")
        self.assertIn("没有评价记录", empty["executive_summary"])
        failed = build_customer_report(self.source, self.results.iloc[:2], "客户评价")
        self.assertIn("暂无有效分析结果", failed["executive_summary"])
        self.assertNotIn("0.0%", failed["executive_summary"])
        self.assertNotIn("暂无负面", failed["executive_summary"])

    def test_executive_summary_with_no_negatives_does_not_invent_problem_ranking(self):
        source = pd.DataFrame({"评价": ["很好", "一般"]})
        results = pd.DataFrame([
            successful(2, "positive", "product_quality", "low"),
            successful(3, "neutral", "other", "medium"),
        ])
        report = build_customer_report(source, results, "评价")
        summary = report["executive_summary"]
        self.assertIn("正面占 50.0%", summary)
        self.assertIn("有效评价中暂无负面结果", summary)
        self.assertIn("暂未识别出高优先级评价", summary)
        self.assertNotIn("差评中", summary)
        self.assertNotIn("仅反映已覆盖部分", summary)

    def test_executive_summary_handles_tied_negative_categories_without_single_winner(self):
        source = pd.DataFrame({"评价": ["很差", "不好", "太慢"]})
        results = pd.DataFrame([
            successful(2, category="product_quality"),
            successful(3, category="delivery"),
            successful(4, category="customer_service"),
        ])
        report = build_customer_report(source, results, "评价")
        summary = report["executive_summary"]
        self.assertIn("等 3 类并列最多（各 1 条）", summary)
        self.assertIn("负面占 100.0%", summary)
        self.assertIn("复核 3 条高优先级评价", summary)

    def test_html_recomputes_summary_instead_of_using_stale_or_untrusted_cached_text(self):
        report = build_customer_report(self.source, self.results, "客户评价")
        report["executive_summary"] = "过期结论<script>bad()</script>"
        html = export_customer_html(report).decode()
        self.assertNotIn("过期结论", html)
        self.assertIn("有效覆盖 4/6 条评价", html)

    def test_export_contains_accessible_static_svgs_and_complete_internal_links(self):
        report = build_customer_report(self.source, self.results, "客户评价", "商品")
        html = export_customer_html(report).decode()
        fragments = re.findall(r"<svg\b.*?</svg>", html, re.DOTALL)
        self.assertGreaterEqual(len(fragments), 5)
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        for fragment in fragments:
            svg = ElementTree.fromstring(fragment)
            self.assertEqual(svg.get("role"), "img")
            self.assertIsNotNone(svg.find("svg:title", namespace))
            self.assertIsNotNone(svg.find("svg:desc", namespace))
            self.assertNotIn("<script", fragment)
            self.assertNotIn("foreignObject", fragment)
        markup = ReportMarkup(html)
        identifiers = [attrs["id"] for _, attrs in markup.elements if "id" in attrs]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        targets = [attrs["href"][1:] for _, attrs in markup.elements if "href" in attrs]
        self.assertTrue(set(targets).issubset(set(identifiers)))
        self.assertIn("script-src 'none'", html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn("@media print", html)
        self.assertIn("@page appendix", html)
        self.assertIn("无外部资源、无脚本", html)
        self.assertIn("复用分析", html)

    def test_zero_coverage_and_all_failures_do_not_present_a_successful_distribution(self):
        source = pd.DataFrame({"评价": ["无法使用", "没收到货"]})
        failures = pd.DataFrame([
            {"source_row": 2, "processing_status": "failed", "error_message": "服务未连接"},
            {"source_row": 3, "processing_status": "validation_error", "error_message": "输出校验失败"},
        ])
        report = build_customer_report(source, failures, "评价")
        html = export_customer_html(report).decode()
        self.assertEqual(report["insights"][0]["title"], "暂无可统计的有效评价")
        self.assertIn("调用或输出失败 2 条", report["insights"][0]["body"])
        self.assertIn("暂无有效评价", html)
        donut = ElementTree.fromstring(re.findall(r"<svg\b.*?</svg>", html, re.DOTALL)[0])
        labels = [element.text for element in donut.iter() if element.tag.endswith("}text")]
        self.assertIn("—", labels)
        self.assertNotIn("0.0%", labels)
        for fragment in re.findall(r"<svg\b.*?</svg>", html, re.DOTALL):
            for element in ElementTree.fromstring(fragment).iter():
                for attribute in ("width", "height", "stroke-dashoffset"):
                    value = element.get(attribute)
                    if value:
                        self.assertNotIn("nan", value.lower())
                        self.assertNotIn("inf", value.lower())
        empty = build_customer_report(source.iloc[:0], pd.DataFrame(), "评价")
        self.assertIn("暂无可统计的有效评价", export_customer_html(empty).decode())

    def test_analysis_metadata_is_optional_allowlisted_and_does_not_change_metrics(self):
        metadata = {
            "planned_requests": 4, "actual_requests": 2, "batch_size": 8,
            "unique_pending_rows": 3, "elapsed_seconds": 1.25,
            "total_tokens": 120, "usage_reported_requests": 2,
            "prompt_version": '<script>alert("rule")</script>',
            "api_key": "must-never-be-displayed", "endpoint": "https://private.invalid",
        }
        report = build_customer_report(self.source, self.results, "客户评价", analysis_meta=metadata)
        self.assertEqual(report["metrics"]["success_rows"], 4)
        self.assertEqual(report["metrics"]["model_success_rows"], 3)
        html = export_customer_html(report).decode()
        self.assertIn("实际请求数", html)
        self.assertIn("2 次", html)
        self.assertIn("120 token", html)
        self.assertIn("不是费用", html)
        self.assertNotIn("must-never-be-displayed", html)
        self.assertNotIn("private.invalid", html)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;script&gt;", html)
        metadata["actual_requests"] = 999
        self.assertEqual(report["analysis_meta"]["actual_requests"], 2)
        report.pop("analysis_meta")
        report.pop("insights")
        legacy_html = export_customer_html(report).decode()
        self.assertIn("不根据有效评价数推算实际模型调用", legacy_html)
        self.assertTrue(all(note in legacy_html for note in report["notes"]))

    def test_long_original_text_and_wide_fields_are_preserved_for_printing(self):
        original = "原文开始" + "很长的顾客反馈。" * 900 + '<em>原文结束</em>'
        source = pd.DataFrame({"评价": [original], **{f"额外字段{number}": [f"原始值{number}"] for number in range(20)}})
        report = build_customer_report(source, pd.DataFrame([successful(2)]), "评价")
        original_detail = report["detail"].copy(deep=True)
        html = export_customer_html(report).decode()
        self.assertIn(escape(original), html)
        self.assertIn("原始值19", html)
        self.assertIn("原始第 2 行", html)
        self.assertIn("print-records", html)
        self.assertIn("overflow-wrap:anywhere", html)
        pd.testing.assert_frame_equal(report["detail"], original_detail)

    def test_store_chart_limits_visual_rows_but_keeps_all_groups_and_safe_labels(self):
        stores = ['<svg onload="bad()">门店' + str(number) + "长名称" * 30 for number in range(10)]
        source = pd.DataFrame({"门店": stores, "评价": ["评价" + str(number) for number in range(10)]})
        results = pd.DataFrame([successful(number + 2) for number in range(10)])
        report = build_customer_report(source, results, "评价", "门店")
        html = export_customer_html(report).decode()
        fragments = re.findall(r"<svg\b.*?</svg>", html, re.DOTALL)
        group_svg = next(fragment for fragment in fragments if 'id="groups-title"' in fragment)
        root = ElementTree.fromstring(group_svg)
        tracks = [node for node in root.iter() if node.tag.endswith("}rect") and node.get("fill") == "#E8F1F5"]
        self.assertEqual(len(tracks), 8)
        self.assertIn(escape(stores[-1]), html)
        self.assertNotIn('<svg onload="bad()">', html)
        self.assertEqual(len(report["grouped"]), 10)

    def test_csv_bom_and_formula_escaping_in_cells_and_headers_without_mutation(self):
        source = pd.DataFrame({"=危险列": ["=SUM(A1)", "+5"]})
        result = pd.DataFrame([successful(2), successful(3)])
        report = build_customer_report(source, result, "=危险列")
        exported = export_customer_csv(report)
        self.assertTrue(exported.startswith(b"\xef\xbb\xbf"))
        reread = pd.read_csv(io.BytesIO(exported), keep_default_na=False)
        self.assertEqual(reread.columns[0], "'=危险列")
        self.assertEqual(reread.iloc[:, 0].tolist(), ["'=SUM(A1)", "'+5"])
        self.assertEqual(report["detail"].iloc[:, 0].tolist(), ["=SUM(A1)", "+5"])

    def test_physical_excel_rows_can_be_noncontiguous_and_do_not_depend_on_index(self):
        source = pd.DataFrame({"评价": ["发货慢", "质量好"]}, index=[100, 900])
        source.attrs["source_rows"] = [5, 8]
        report = build_customer_report(source, pd.DataFrame([successful(8), successful(5)]), "评价")
        self.assertEqual(report["detail"][report["detail_columns"]["source_row"]].tolist(), [5, 8])
        source.attrs["source_rows"] = [5, 5]
        with self.assertRaises(ValueError):
            build_customer_report(source, pd.DataFrame(), "评价")


if __name__ == "__main__":
    unittest.main()
