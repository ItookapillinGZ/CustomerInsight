import copy
from pathlib import Path
import tempfile
import unittest

from core.history_store import HistoryStore, _pack, _unpack
from core.review_topics import compare_report_batches, refresh_report_topics
from core.topic_taxonomy import TAXONOMY_VERSION, normalize_topic
from tests.test_review_topics import aspect, report_for


class TopicTaxonomyTests(unittest.TestCase):
    def test_exact_chinese_and_english_aliases_share_a_canonical_name(self):
        for raw in ("配送慢", "物流延迟", "到货慢", "配送速度", "配送时效", "slow delivery",
                    " Late   Delivery ", "Ｄｅｌｉｖｅｒｙ　Ｓｐｅｅｄ"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_topic(raw), "配送时效")

    def test_unknown_terms_and_distinct_aspects_are_not_fuzzy_clustered(self):
        for raw in ("配送员丢件", "配送慢导致退款", "灯光闪烁", "battery cycle count"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_topic(raw), raw)
        self.assertNotEqual(normalize_topic("客服回复慢"), normalize_topic("客服态度差"))
        self.assertNotEqual(normalize_topic("密封性"), normalize_topic("包装完好"))

    def test_synonyms_merge_across_reviews_and_count_each_record_once(self):
        report = report_for(["配送慢，而且物流延迟", "Late delivery", "希望配送及时"], [
            [aspect("配送慢", "issue", "negative", "配送慢"),
             aspect("物流延迟", "issue", "negative", "物流延迟")],
            [aspect("late delivery", "issue", "negative", "Late delivery")],
            [aspect("配送及时", "request", "neutral", "希望配送及时")],
        ])
        topics = {item["key"]: item for item in report["topics"]}
        issue = topics["issue:配送时效"]
        self.assertEqual(issue["review_count"], 2)
        self.assertEqual(issue["source_rows"], [2, 3])
        self.assertEqual(issue["share"], 2 / 3)
        self.assertEqual(issue["raw_topics"], ["late delivery", "物流延迟", "配送慢"])
        self.assertEqual(issue["examples"][0]["raw_topics"], ["物流延迟", "配送慢"])
        self.assertEqual(topics["request:配送时效"]["review_count"], 1)
        self.assertEqual(report["taxonomy_version"], TAXONOMY_VERSION)
        self.assertEqual(issue["taxonomy_version"], TAXONOMY_VERSION)
        self.assertEqual(report["topic_coverage"]["with_aspects_rows"], 3)

    def test_praise_issue_request_and_unknown_spellings_remain_separate(self):
        report = report_for(["送货及时", "配送慢", "希望快些", "独特需求"], [
            [aspect("送货及时", "praise", "positive", "送货及时")],
            [aspect("配送慢", "issue", "negative", "配送慢")],
            [aspect("配送时效", "request", "neutral", "希望快些")],
            [aspect("独特需求", "request", "neutral", "独特需求")],
        ])
        self.assertEqual({item["key"] for item in report["topics"]}, {
            "praise:配送时效", "issue:配送时效", "request:配送时效", "request:独特需求"})

    def test_original_terms_and_exact_evidence_survive_report_and_refresh(self):
        originals = [aspect("减少塑料包装", "request", "neutral", "少用塑料"),
                     aspect("物流延迟", "issue", "negative", "延迟三天")]
        report = report_for(["希望少用塑料，延迟三天"], [originals])
        self.assertEqual(report["aspects_by_row"][0]["aspects"], originals)
        packaging = next(item for item in report["topics"] if item["topic"] == "包装减塑")
        self.assertEqual(packaging["raw_topics"], ["减少塑料包装"])
        before = copy.deepcopy(report["topics"])
        refresh_report_topics(report)
        self.assertEqual(report["topics"], before)
        self.assertEqual(report["aspects_by_row"][0]["aspects"], originals)
        report.pop("aspects_by_row")
        refresh_report_topics(report)
        self.assertEqual(report["topics"], before)
        self.assertEqual(report["aspects_by_row"][0]["aspects"], originals)

    def test_complete_new_reports_compare_synonyms_by_canonical_topic(self):
        previous = report_for(["配送慢"], [[aspect("配送慢", "issue", "negative", "配送慢")]])
        current = report_for(["物流延迟", "不错"], [
            [aspect("物流延迟", "issue", "negative", "物流延迟")],
            [aspect("耐用性", "praise", "positive", "不错")]])
        comparison = compare_report_batches(current, previous)
        self.assertTrue(comparison["topics_comparable"])
        delivery = next(item for item in comparison["topics"] if item["key"] == "issue:配送时效")
        self.assertEqual((delivery["current_count"], delivery["previous_count"]), (1, 1))
        self.assertEqual(delivery["change_pp"], -50)

    def test_legacy_new_or_unknown_taxonomy_blocks_topics_but_keeps_basic_comparison(self):
        current = report_for(["配送慢"], [[aspect("配送慢", "issue", "negative", "配送慢")]])
        for version in (None, "unknown-taxonomy"):
            previous = copy.deepcopy(current)
            if version is None:
                previous.pop("taxonomy_version")
            else:
                previous["taxonomy_version"] = version
            before = _pack(previous)
            with self.subTest(version=version):
                comparison = compare_report_batches(current, previous)
                self.assertFalse(comparison["topics_comparable"])
                self.assertTrue(comparison["basic_comparable"])
                self.assertEqual(comparison["topics"], [])
                self.assertTrue(any("归一版本" in reason for reason in comparison["topic_reasons"]))
                self.assertEqual(_pack(previous), before)
        previous = copy.deepcopy(current)
        previous["taxonomy_version"] = current["taxonomy_version"] = "unknown-taxonomy"
        self.assertFalse(compare_report_batches(current, previous)["topics_comparable"])

    def test_explicit_legacy_refresh_preserves_legacy_buckets_without_migration(self):
        report = report_for(["配送慢", "物流延迟"], [
            [aspect("配送慢", "issue", "negative", "配送慢")],
            [aspect("物流延迟", "issue", "negative", "物流延迟")]])
        report.pop("taxonomy_version")
        refresh_report_topics(report)
        self.assertNotIn("taxonomy_version", report)
        self.assertEqual({item["topic"] for item in report["topics"]}, {"配送慢", "物流延迟"})
        self.assertTrue(all("raw_topics" not in item for item in report["topics"]))
        self.assertTrue(compare_report_batches(report, copy.deepcopy(report))["topics_comparable"])

    def test_unknown_taxonomy_cannot_be_recomputed_or_overwritten(self):
        report = report_for(["物流延迟"], [[aspect("物流延迟", "issue", "negative", "物流延迟")]])
        report["taxonomy_version"] = "future-version"
        before = _pack(report)
        with self.assertRaisesRegex(ValueError, "归一版本"):
            refresh_report_topics(report)
        self.assertEqual(_pack(report), before)

    def test_new_and_legacy_history_roundtrip_neither_normalizes_nor_drops_fields(self):
        report = report_for(["物流延迟"], [[aspect("物流延迟", "issue", "negative", "物流延迟")]])
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            store = HistoryStore(directory)
            identifier = store.save_batch(report, filename="new.csv", file_bytes="评价\n物流延迟".encode(),
                                          mapping={"text_column": "评价"})
            restored = store.load_batch(identifier)["report"]
            self.assertEqual(_pack(restored), _pack(report))
            self.assertEqual(restored["taxonomy_version"], TAXONOMY_VERSION)
            self.assertEqual(_unpack(_pack(report))["topics"][0]["raw_topics"], ["物流延迟"])
            legacy = copy.deepcopy(report)
            legacy.pop("taxonomy_version")
            legacy["topics"] = [{"key": "issue:物流延迟", "topic": "物流延迟", "kind": "issue"}]
            old_id = store.save_batch(legacy, filename="legacy.csv", file_bytes="评价\n旧物流延迟".encode(),
                                     mapping={"text_column": "评价"})
            old = store.load_batch(old_id)["report"]
            self.assertEqual(_pack(old), _pack(legacy))
            self.assertNotIn("taxonomy_version", old)
            self.assertEqual(old["topics"][0]["topic"], "物流延迟")


if __name__ == "__main__":
    unittest.main()
