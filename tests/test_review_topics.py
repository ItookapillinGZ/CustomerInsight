import copy
from html import escape
from html.parser import HTMLParser
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from pydantic import ValidationError

from ai.grouped_batch import GroupedFeedbackResponse, analyze_grouped_batch
from core.config import Config
from core.csv_io import prepare_feedback_rows, results_to_dataframe
from core.customer_report import build_customer_report, export_customer_html
from core.review_topics import (
    TOPICS_VERSION, ReviewAspect, compare_report_batches, refresh_report_topics,
    validate_aspects,
)
from models.models import CustomerFeedbackAnalysis, FeedbackResult


def aspect(topic="保温效果", kind="praise", sentiment="positive", evidence="保温很好"):
    return {"topic": topic, "kind": kind, "sentiment": sentiment, "evidence": evidence}


def report_for(texts, aspects, *, statuses=None, groups=None, prompt="v3_grouped_aspects_v1"):
    source = pd.DataFrame({"评价": texts})
    if groups is not None:
        source["门店"] = groups
    results = []
    for index, (text, item) in enumerate(zip(texts, aspects), start=2):
        status = statuses[index - 2] if statuses else "success"
        record = {"source_row": index, "processing_status": status, "aspects": item}
        if status == "success":
            record.update(sentiment="positive", category="product_quality", priority="low", summary="产品反馈")
        results.append(record)
    return build_customer_report(source, pd.DataFrame(results), "评价", "门店" if groups is not None else None,
                                 analysis_meta={"prompt_version": prompt})


class AspectValidationTests(unittest.TestCase):
    def test_specific_topics_preserve_original_terms_without_using_main_category(self):
        value = aspect("减少塑料包装", "request", "neutral", "希望减少塑料包装")
        parsed = validate_aspects([value], "质量好，希望减少塑料包装")
        self.assertEqual(parsed[0].topic, "减少塑料包装")
        for value in ["产品质量", "其他", " ", "x\ny"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReviewAspect.model_validate(aspect(value))

    def test_evidence_must_be_continuous_exact_and_belong_to_same_review(self):
        for evidence in ["", "   ", "保温…很好", "密封不漏水", "保温" * 121]:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate_aspects([aspect(evidence=evidence)], "保温很好，客服不错")
        with self.assertRaises(ValueError):
            validate_aspects([aspect()] * 6, "保温很好")
        with self.assertRaises(ValueError):
            validate_aspects({"topic": "保温效果"}, "保温很好")

    def test_unknown_fields_invalid_kind_and_types_are_rejected(self):
        for item in [{**aspect(), "action": "do"}, {**aspect(), "kind": "mixed"},
                     {**aspect(), "evidence": 12}, {**aspect(), "sentiment": "mixed"}]:
            with self.subTest(item=item), self.assertRaises(ValidationError):
                ReviewAspect.model_validate(item)

    def test_feedback_result_validates_quotes_and_disallows_failed_aspects(self):
        fields = dict(feedback_id="row-2", source_row=2, original_text="保温很好", sentiment="positive",
                      category="product_quality", priority="low", summary="好", processing_status="success")
        self.assertEqual(FeedbackResult(**fields, aspects=[aspect()]).aspects[0].topic, "保温效果")
        with self.assertRaises(ValueError):
            FeedbackResult(**fields, aspects=[aspect(evidence="并无原文")])
        with self.assertRaises(ValueError):
            FeedbackResult(feedback_id="row-2", source_row=2, original_text="保温很好", processing_status="failed", aspects=[aspect()])
        self.assertNotIn("aspects", CustomerFeedbackAnalysis.model_fields)
        self.assertEqual(FeedbackResult(**fields).aspects, [])

    def test_strict_provider_schema_requires_aspects_without_breaking_old_local_results(self):
        schema = GroupedFeedbackResponse.model_json_schema()
        compact = schema["$defs"]["CompactFeedbackPrediction"]
        self.assertIn("aspects", compact["required"])
        self.assertFalse(compact["additionalProperties"])
        self.assertEqual(schema["$defs"]["ReviewAspect"]["required"], ["topic", "kind", "sentiment", "evidence"])

    def test_grouped_response_isolates_cross_row_quote_and_preserves_valid_other_row(self):
        client = MagicMock()
        source = prepare_feedback_rows(pd.DataFrame({"评价": ["保温很好", "希望减少塑料包装"]}), "评价")
        response = [
            {"feedback_id": "row-2", "sentiment": "positive", "category": "product_quality", "priority": "low", "summary": "好", "aspects": [aspect(evidence="希望减少塑料包装")]},
            {"feedback_id": "row-3", "sentiment": "neutral", "category": "suggestions", "priority": "low", "summary": "减塑需求", "aspects": [aspect("包装减塑", "request", "neutral", "希望减少塑料包装")]},
        ]
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps({"results": response}), refusal=None))])
        with patch.object(Config, "API_MODE", "chat_completions"), patch.object(Config, "CHAT_OUTPUT_MODE", "json_object"):
            rows = analyze_grouped_batch(source, client=client)
        self.assertEqual([row.processing_status for row in rows], ["validation_error", "success"])
        self.assertIsNone(rows[0].sentiment)
        self.assertEqual(rows[0].aspects, [])
        self.assertEqual(rows[1].aspects[0].topic, "包装减塑")
        self.assertEqual(client.chat.completions.create.call_count, 1)


class TopicReportTests(unittest.TestCase):
    def test_multi_aspects_count_original_rows_once_per_kind_topic(self):
        report = report_for(["保温很好，希望减少塑料包装"], [[
            aspect(), aspect(), aspect("包装减塑", "request", "neutral", "希望减少塑料包装")]])
        self.assertEqual(report["topic_coverage"]["status"], "complete")
        self.assertEqual(report["topic_coverage"]["with_aspects_rows"], 1)
        self.assertEqual(report["topics"][0]["kind"], "request")
        self.assertEqual([item["review_count"] for item in report["topics"]], [1, 1])
        self.assertEqual(sum(item["share"] for item in report["topics"]), 2.0)
        self.assertEqual(report["topics"][0]["examples"], [{"source_row": 2, "text": "希望减少塑料包装", "group": None,
                                                        "raw_topics": ["包装减塑"]}])

    def test_same_topic_praise_and_issue_have_separate_keys(self):
        report = report_for(["包装完好", "包装破损"], [
            [aspect("包装完整", "praise", "positive", "包装完好")],
            [aspect("包装完整", "issue", "negative", "包装破损")]])
        self.assertEqual({item["key"] for item in report["topics"]}, {"praise:包装完整", "issue:包装完整"})
        self.assertEqual(report["topics"][0]["kind"], "issue")

    def test_failed_rows_are_excluded_but_empty_aspects_are_visible_as_partial_coverage(self):
        report = report_for(["保温很好", "普通", "失败"], [[aspect()], [], []], statuses=["success", "success", "failed"])
        self.assertEqual(report["topics"][0]["share"], 0.5)
        self.assertEqual(report["topic_coverage"], {"success_rows": 2, "with_aspects_rows": 1,
                                                  "without_aspects_rows": 1, "rate": 0.5, "status": "partial"})
        self.assertEqual(report["aspects_by_row"][-1]["aspects"], [])

    def test_old_report_and_empty_report_never_invent_topics(self):
        old = report_for(["保温很好"], [[]])
        self.assertEqual(old["topics"], [])
        self.assertEqual(old["topic_coverage"]["status"], "unavailable")
        empty = build_customer_report(pd.DataFrame({"评价": []}), pd.DataFrame(), "评价")
        self.assertEqual(empty["topics"], [])
        self.assertEqual(empty["topic_coverage"]["status"], "no_valid_reviews")

    def test_duplicate_reuse_has_each_original_row_weight_and_its_own_store(self):
        source = pd.DataFrame({"评价": [" 保温很好 ", "保温很好"], "门店": ["A", "B"]})
        rows = prepare_feedback_rows(source, "评价")
        rows[0] = FeedbackResult.model_validate({**rows[0].model_dump(), "processing_status": "success",
            "sentiment": "positive", "category": "product_quality", "priority": "low", "summary": "好", "aspects": [aspect()]})
        report = build_customer_report(source, results_to_dataframe(rows), "评价", "门店")
        topic = report["topics"][0]
        self.assertEqual(topic["review_count"], 2)
        self.assertEqual(topic["source_rows"], [2, 3])
        self.assertEqual({item["group"] for item in topic["examples"]}, {"A", "B"})
        self.assertEqual(report["aspects_by_row"][1]["aspects"], report["aspects_by_row"][0]["aspects"])
        self.assertEqual(report["topic_coverage"]["rate"], 1)

    def test_noncontiguous_excel_source_rows_are_retained(self):
        source = pd.DataFrame({"评价": ["保温很好"]})
        source.attrs["source_rows"] = [9]
        result = pd.DataFrame([{"source_row": 9, "processing_status": "success", "sentiment": "positive",
                                "category": "product_quality", "priority": "low", "summary": "好", "aspects": [aspect()]}])
        report = build_customer_report(source, result, "评价")
        self.assertEqual(report["topics"][0]["source_rows"], [9])

    def test_refresh_round_trip_from_json_detail_preserves_topics(self):
        report = report_for(["保温很好"], [[aspect()]])
        expected = copy.deepcopy(report["topics"])
        report.pop("aspects_by_row")
        report["topics"] = []
        refresh_report_topics(report)
        self.assertEqual(report["topics"], expected)
        self.assertIsInstance(report["detail"].iloc[0][report["detail_columns"]["aspects"]], str)

    def test_tampered_unknown_or_duplicate_row_and_cross_row_quotes_are_rejected(self):
        report = report_for(["保温很好"], [[aspect()]])
        cases = [[{"source_row": 8, "aspects": [aspect()]}], report["aspects_by_row"] * 2,
                 [{"source_row": 2, "aspects": [aspect(evidence="别条原文")]}]]
        for records in cases:
            changed = copy.deepcopy(report)
            changed["aspects_by_row"] = records
            with self.subTest(records=records), self.assertRaises(ValueError):
                refresh_report_topics(changed)

    def test_customer_report_rejects_untraceable_aspect_even_if_labels_are_valid(self):
        with self.assertRaises(ValueError):
            report_for(["保温很好"], [[aspect(evidence="其他顾客的内容")]])


class BatchComparisonTests(unittest.TestCase):
    def test_comparison_uses_denominator_percentage_points_and_separates_kinds(self):
        previous = report_for(["包装破损", "保温很好"], [
            [aspect("包装完整", "issue", "negative", "包装破损")], [aspect()]])
        current = report_for(["包装破损", "包装破损", "保温很好", "希望减少塑料包装"], [
            [aspect("包装完整", "issue", "negative", "包装破损")],
            [aspect("包装完整", "issue", "negative", "包装破损")], [aspect()],
            [aspect("包装减塑", "request", "neutral", "希望减少塑料包装")]])
        comparison = compare_report_batches(current, previous)
        self.assertTrue(comparison["comparable"])
        topics = {topic["key"]: topic for topic in comparison["topics"]}
        packaging = topics["issue:包装完整"]
        self.assertEqual((packaging["current_count"], packaging["previous_count"]), (2, 1))
        self.assertEqual(packaging["change_pp"], 0)
        self.assertEqual(packaging["change"], "unchanged")
        self.assertEqual(topics["praise:保温效果"]["change_pp"], -25)
        self.assertEqual(topics["request:包装减塑"]["change"], "new")
        self.assertEqual(comparison["comparison_type"], "batch")
        self.assertTrue(any("不足 5" in warning for warning in comparison["warnings"]))

    def test_unseen_topic_is_not_claimed_resolved_or_causal(self):
        previous = report_for(["包装破损"], [[aspect("包装完整", "issue", "negative", "包装破损")]])
        current = report_for(["保温很好"], [[aspect()]])
        comparison = compare_report_batches(current, previous)
        self.assertEqual(next(item for item in comparison["topics"] if item["kind"] == "issue")["change"], "not_seen")
        self.assertTrue(any("不能证明" in warning for warning in comparison["warnings"]))

    def test_unknown_or_mismatched_definitions_and_partial_topics_are_not_comparable(self):
        good = report_for(["保温很好"], [[aspect()]])
        for kind in ["text", "group", "prompt", "no_prompt", "topics", "legacy", "partial", "empty"]:
            changed = copy.deepcopy(good)
            if kind == "text": changed["text_column"] = "其他列"
            if kind == "group": changed["group_column"] = "门店"
            if kind == "prompt": changed["analysis_meta"]["prompt_version"] = "old"
            if kind == "no_prompt": changed["analysis_meta"] = {}
            if kind == "topics": changed["topics_version"] = "other"
            if kind == "legacy": changed = report_for(["保温很好"], [[]])
            if kind == "partial": changed = report_for(["保温很好", "普通"], [[aspect()], []])
            if kind == "empty": changed = build_customer_report(pd.DataFrame({"评价": []}), pd.DataFrame(), "评价")
            with self.subTest(kind=kind):
                comparison = compare_report_batches(good, changed)
                self.assertFalse(comparison["comparable"])
                self.assertTrue(comparison["reasons"])
                self.assertEqual(comparison["topics"], [])
                self.assertFalse(comparison["topics_comparable"])
                self.assertEqual(comparison["topic_reasons"], comparison["reasons"])
                self.assertEqual(comparison["basic_comparable"], kind not in {"text", "group", "empty"})

    def test_one_missing_aspect_does_not_block_100_row_classification_comparison(self):
        def batch(success, aspect_rows):
            source = pd.DataFrame({"评价": [f"包装评价{i}" for i in range(100)]})
            rows = []
            for index, text in enumerate(source["评价"]):
                row = {"source_row": index + 2, "processing_status": "success" if index < success else "failed"}
                if index < success:
                    row.update(sentiment="negative" if index < 46 else "positive",
                               category="delivery" if index < 46 else "product_quality",
                               priority="high" if index < 10 else "low", summary="包装评价",
                               aspects=[aspect("包装完整", "issue", "negative", text)] if index < aspect_rows else [])
                rows.append(row)
            return build_customer_report(source, pd.DataFrame(rows), "评价", analysis_meta={"prompt_version": "v3_grouped_aspects_v2"})

        current, previous = batch(97, 97), batch(93, 92)
        previous_topics = copy.deepcopy(previous["topics"])
        comparison = compare_report_batches(current, previous)
        self.assertTrue(comparison["basic_comparable"])
        self.assertFalse(comparison["topics_comparable"])
        self.assertFalse(comparison["comparable"])
        self.assertEqual(comparison["topics"], [])
        negative = comparison["metrics"]["negative_rate"]
        self.assertEqual((negative["current_count"], negative["previous_count"]), (46, 46))
        self.assertEqual((negative["current_denominator"], negative["previous_denominator"]), (97, 93))
        self.assertEqual(negative["change_pp"], -2.039685)
        coverage = comparison["metrics"]["success_rate"]
        self.assertEqual((coverage["current"], coverage["previous"], coverage["change_pp"]), (.97, .93, 4))
        self.assertEqual((coverage["current_denominator"], coverage["previous_denominator"]), (100, 100))
        for records in comparison["distributions"].values():
            self.assertEqual(sum(item["current_count"] for item in records), 97)
            self.assertEqual(sum(item["previous_count"] for item in records), 93)
        self.assertEqual(previous["topics"], previous_topics)
        self.assertEqual(previous["topic_coverage"]["with_aspects_rows"], 92)

    def test_fixed_classification_recounts_do_not_depend_on_optional_saved_rates(self):
        current = report_for(["保温很好"], [[aspect()]])
        previous = report_for(["普通"], [[]], prompt="old_without_aspects")
        current["metrics"].pop("negative_rate")
        previous["metrics"]["negative_rate"] = 1
        comparison = compare_report_batches(current, previous)
        self.assertTrue(comparison["basic_comparable"])
        self.assertEqual(comparison["metrics"]["negative_rate"]["previous"], 0)
        self.assertEqual(comparison["distributions"]["sentiment"][0]["current_count"], 1)
        self.assertTrue(any("基础比较仅反映" in warning for warning in comparison["warnings"]))

    def test_missing_labels_and_conflicting_counts_do_not_become_zeroes(self):
        good = report_for(["保温很好"], [[aspect()]])
        for mutate in (
            lambda report: report["detail"].__setitem__(report["detail_columns"]["sentiment"], ""),
            lambda report: report["detail"].__setitem__(report["detail_columns"]["category"], "未支持标签"),
            lambda report: report["metrics"].update(success_rows=2),
            lambda report: report["detail"].drop(columns=[report["detail_columns"]["priority"]], inplace=True),
        ):
            changed = copy.deepcopy(good)
            mutate(changed)
            comparison = compare_report_batches(good, changed)
            self.assertFalse(comparison["basic_comparable"])
            self.assertTrue(comparison["basic_reasons"])
            self.assertEqual(comparison["metrics"], {})
            self.assertTrue(all(not records for records in comparison["distributions"].values()))

    def test_explicitly_reused_classification_keeps_original_record_weight(self):
        source = pd.DataFrame({"评价": ["保温很好", "保温很好"]})
        results = pd.DataFrame([
            {"source_row": 2, "feedback_id": "row-2", "processing_status": "success", "sentiment": "positive",
             "category": "product_quality", "priority": "low", "summary": "好", "aspects": [aspect()]},
            {"source_row": 3, "processing_status": "skipped_duplicate", "duplicate_of": "row-2"},
        ])
        current = build_customer_report(source, results, "评价")
        previous = report_for(["普通"], [[]])
        comparison = compare_report_batches(current, previous)
        self.assertTrue(comparison["basic_comparable"])
        self.assertEqual(comparison["metrics"]["positive_rate"]["current_count"], 2)
        self.assertEqual(comparison["metrics"]["success_rate"]["current"], 1)


class _Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class TopicHtmlTests(unittest.TestCase):
    def test_topic_cards_show_three_aspects_evidence_weight_and_existing_row_links(self):
        report = report_for(["保温很好，包装破损，希望减少塑料包装"], [[
            aspect(), aspect("包装完整", "issue", "negative", "包装破损"),
            aspect("包装减塑", "request", "neutral", "希望减少塑料包装")]])
        html = export_customer_html(report).decode()
        section = re.search(r'<section id="review-topics">.*?</section>', html, re.DOTALL).group()
        self.assertEqual(section.count('class="evidence-item"'), 3)
        self.assertIn("顾客认可", section)
        self.assertIn("具体问题", section)
        self.assertIn("改进需求", section)
        self.assertEqual(section.count("1 条原记录 · 占有效评价 100.0%（分母 1 条）"), 3)
        self.assertIn("各主题占比之和可以超过 100%", section)
        self.assertEqual(section.count('href="#review-row-2"'), 3)
        self.assertIn("希望减少塑料包装", section)
        self.assertLess(section.index("具体问题"), section.index("顾客认可"))

    def test_topic_and_history_text_are_escaped_and_export_has_no_external_resources(self):
        malicious = '<script>bad()</script><img src=x onerror="bad()">'
        report = report_for([malicious], [[aspect(malicious[:35], "request", "neutral", malicious)]], groups=[malicious])
        previous = report_for(["保温很好"], [[aspect()]], groups=["A"])
        comparison = compare_report_batches(report, previous)
        comparison["warnings"].append(malicious)
        report["historical_context"] = {"batch": {"filename": malicious}, "comparison": comparison,
            "evidence": [{"source_row": 2, "text": malicious, "group": malicious}]}
        html = export_customer_html(report).decode()
        self.assertIn(escape(malicious), html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<img", html.lower())
        self.assertNotIn('<svg onload', html.lower())
        elements = _Elements(html).elements
        self.assertNotIn("script", [tag for tag, _ in elements])
        for tag, attrs in elements:
            self.assertFalse(any(key.lower().startswith("on") for key in attrs), (tag, attrs))
            self.assertNotIn("src", attrs)
            if "href" in attrs:
                self.assertTrue(attrs["href"].startswith("#"))
        self.assertIn("connect-src 'none'", html)
        self.assertIn("script-src 'none'", html)

    def test_history_comparison_names_file_and_displays_bases_and_percentage_points(self):
        previous = report_for(["包装破损", "保温很好"], [
            [aspect("包装完整", "issue", "negative", "包装破损")], [aspect()]])
        current = report_for(["包装破损"], [[aspect("包装完整", "issue", "negative", "包装破损")]])
        current["historical_context"] = {"batch": {"filename": "上次评价.xlsx"},
            "comparison": compare_report_batches(current, previous),
            "evidence": [{"source_row": 2, "text": "包装破损", "group": "上批门店"}]}
        html = export_customer_html(current).decode()
        section = re.search(r'<section id="batch-comparison">.*?</section>', html, re.DOTALL).group()
        self.assertIn("上次评价.xlsx", section)
        self.assertIn("本批：原始记录 1 条，有效评价 1 条，主题覆盖 1 条", section)
        self.assertIn("上批：原始记录 2 条，有效评价 2 条，主题覆盖 2 条", section)
        self.assertIn("占比变化（百分点）", section)
        self.assertIn("+50.0", section)
        self.assertIn("不足 5 条", section)
        self.assertIn("不能证明", section)
        self.assertIn("上批《上次评价.xlsx》原始第 2 行", section)
        self.assertNotIn('href="#review-row-2"', section)
        self.assertIn("明确选定的上批分析结果", html)

    def test_incomparable_history_does_not_display_supplied_false_changes(self):
        current = report_for(["保温很好"], [[aspect()]])
        previous = report_for(["普通"], [[]])
        comparison = compare_report_batches(current, previous)
        comparison["topics"] = [{"topic": "不应出现的假上涨", "kind": "issue", "current_count": 100, "previous_count": 0}]
        comparison["metrics"] = {"negative_rate": {"current": 1, "previous": 0, "change_pp": 100}}
        current["historical_context"] = {"batch": {"filename": "旧版报告.xlsx"}, "comparison": comparison, "evidence": []}
        html = export_customer_html(current).decode()
        section = re.search(r'<section id="batch-comparison">.*?</section>', html, re.DOTALL).group()
        self.assertIn("旧版报告.xlsx", section)
        self.assertIn("主题覆盖不完整", section)
        self.assertIn("主题", section)
        self.assertNotIn("不应出现的假上涨", section)
        self.assertNotIn("+100.0", section)

    def test_legacy_export_does_not_add_empty_topic_or_history_sections(self):
        report = report_for(["普通"], [[]])
        html = export_customer_html(report).decode()
        self.assertNotIn('id="review-topics"', html)
        self.assertNotIn('id="batch-comparison"', html)
        self.assertNotIn('href="#review-topics"', html)
        self.assertNotIn('href="#batch-comparison"', html)
        self.assertIn("本报告仅使用本次表格的分析结果", html)


if __name__ == "__main__":
    unittest.main()
