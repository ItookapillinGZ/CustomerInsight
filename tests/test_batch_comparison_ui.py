"""Batch comparison keeps basic statistics visible when topic coverage is partial."""

import copy
import re
import unittest
from unittest.mock import MagicMock, call, patch

import pandas as pd

import app
from core.customer_report import build_customer_report, export_customer_html
from core.history_workflow import attach_historical_context


def comparison_fixture(sentiments, *, partial_topics=False, name="评价"):
    texts = [f"{name}{index}包装完好" for index in range(len(sentiments) + 1)]
    source = pd.DataFrame({"评价": texts})
    results = []
    for index, sentiment in enumerate(sentiments):
        aspects = [] if partial_topics and index == len(sentiments) - 1 else [
            {"topic": "包装完整", "kind": "praise", "sentiment": "positive", "evidence": "包装完好"}]
        results.append({"source_row": index + 2, "processing_status": "success", "sentiment": sentiment,
                        "category": "customer_service" if index == 0 else "product_quality",
                        "priority": "high" if index == 0 else "low", "summary": "顾客反馈", "aspects": aspects})
    results.append({"source_row": len(sentiments) + 2, "processing_status": "failed"})
    return build_customer_report(source, pd.DataFrame(results), "评价",
                                 analysis_meta={"prompt_version": "v3_grouped_aspects_v1"})


def previous_record(report):
    return {"id": "b" * 32, "filename": "顾客评价500条.xlsx", "created_at": "2026-10-23T10:00:00Z",
            "mapping": {"sheet_name": "评价1-100"}, "report": report}


class BatchComparisonUiTests(unittest.TestCase):
    def setUp(self):
        self.current = comparison_fixture(["positive", "neutral", "negative", "negative"], name="本批")
        self.previous = comparison_fixture(["positive", "positive", "positive", "neutral", "negative"],
                                           partial_topics=True, name="上批")

    def render(self, current=None, previous=None, prepared=None):
        current, previous = current or self.current, previous or self.previous
        record = previous_record(previous)
        streamlit = MagicMock()
        streamlit.session_state = {"customer_active_batch_id": "a" * 32,
                                   "customer_previous_batch_id": record["id"]}
        streamlit.selectbox.return_value = record["id"]
        store = MagicMock()
        store.list_batches.return_value = [record]
        store.load_batch.return_value = record
        with patch.object(app, "st", streamlit), patch.object(app, "_history_store", return_value=store), \
                patch.object(app, "api_ui_enabled", return_value=False):
            if prepared is None:
                result = app._render_history_comparison(current)
            else:
                with patch.object(app, "attach_historical_context", return_value=prepared):
                    result = app._render_history_comparison(current)
        return streamlit, result

    def test_partial_topics_still_show_samples_and_three_basic_distributions(self):
        streamlit, result = self.render()
        comparison = result["historical_context"]["comparison"]
        self.assertTrue(comparison["basic_comparable"])
        self.assertFalse(comparison["topics_comparable"])
        tables = [call.args[0] for call in streamlit.dataframe.call_args_list]
        self.assertEqual(len(tables), 4)
        samples = tables[0].set_index("指标")
        self.assertEqual(samples.loc["有效评价", "本批"], "4")
        self.assertEqual(samples.loc["有效评价", "上批"], "5")
        self.assertEqual(samples.loc["有效覆盖率", "本批"], "80.0%")
        sentiment = tables[1].set_index("项目")
        self.assertEqual(sentiment.loc["负面", "本批条数"], 2)
        self.assertEqual(sentiment.loc["负面", "上批条数"], 1)
        self.assertEqual(sentiment.loc["负面", "本批占比"], "50.0%")
        self.assertEqual(sentiment.loc["负面", "上批占比"], "20.0%")
        self.assertEqual(sentiment.loc["负面", "变化（百分点）"], "+30.0")
        self.assertTrue(any("上面的基础统计可独立比较" in call.args[0] for call in streamlit.info.call_args_list))
        self.assertTrue(any("不代表时间趋势" in call.args[0] for call in streamlit.caption.call_args_list))

    def test_compatible_topics_show_separate_theme_table(self):
        previous = comparison_fixture(["positive", "negative"], name="完整上批")
        streamlit, result = self.render(previous=previous)
        self.assertTrue(result["historical_context"]["comparison"]["topics_comparable"])
        self.assertEqual(streamlit.dataframe.call_count, 5)
        self.assertIn("主题", streamlit.dataframe.call_args_list[-1].args[0].columns)

    def test_legacy_flag_does_not_allow_unverified_basic_changes(self):
        prepared = attach_historical_context(self.current, previous_record(self.previous))
        comparison = prepared["historical_context"]["comparison"]
        comparison.pop("basic_comparable", None)
        comparison["distributions"] = {"sentiment": [{"label": "伪造条目"}]}
        streamlit, _ = self.render(prepared=prepared)
        streamlit.dataframe.assert_not_called()
        self.assertTrue(any("基础统计暂不能直接比较" in call.args[0] for call in streamlit.info.call_args_list))

    def test_history_labels_distinguish_sheets_from_same_workbook(self):
        old = previous_record(self.previous)
        new = {**old, "mapping": {"sheet_name": "评价101-200"}}
        self.assertNotEqual(app._history_batch_label(old), app._history_batch_label(new))
        self.assertTrue(app._history_batch_label(old).startswith("评价1-100 · "))
        self.assertIn("顾客评价500条.xlsx", app._history_batch_label(old))
        self.assertIn("2026-10-23", app._history_batch_label(old))

    def test_download_keeps_basic_changes_and_limits_only_partial_topics(self):
        report = attach_historical_context(self.current, previous_record(self.previous))
        html = export_customer_html(report).decode("utf-8")
        section = re.search(r'<section id="batch-comparison">.*?</section>', html, re.DOTALL).group()
        for title in ("样本与覆盖", "情感分布", "评价类别", "优先级"):
            self.assertIn(title, section)
        self.assertIn("+30.0", section)
        self.assertIn("基础统计已在上面独立展示", section)
        self.assertIn("主题覆盖不完整", section)
        self.assertIn("不代表时间趋势", section)

    def test_download_ignores_old_metrics_and_topic_changes_without_new_flag(self):
        report = attach_historical_context(self.current, previous_record(self.previous))
        report = copy.deepcopy(report)
        comparison = report["historical_context"]["comparison"]
        comparison.pop("basic_comparable", None)
        comparison["metrics"] = {"negative_rate": {"current": 1, "previous": 0, "change_pp": 100}}
        comparison["distributions"] = {"sentiment": [{"label": "不应出现的基础条目"}]}
        comparison["topics"] = [{"topic": "不应出现的假主题"}]
        report["historical_context"]["batch"]["filename"] = "<script>fake()</script>.xlsx"
        section = re.search(r'<section id="batch-comparison">.*?</section>',
                            export_customer_html(report).decode("utf-8"), re.DOTALL).group()
        self.assertNotIn("不应出现的基础条目", section)
        self.assertNotIn("不应出现的假主题", section)
        self.assertNotIn("+100.0", section)
        self.assertNotIn("<script>", section)
        self.assertIn("&lt;script&gt;", section)

    def test_remote_exports_pass_selected_comparison_only_for_html(self):
        prepared = attach_historical_context(self.current, previous_record(self.previous))
        for report, selected in ((prepared, True), (self.current, False)):
            with self.subTest(selected=selected):
                streamlit = MagicMock()
                streamlit.session_state = {"customer_active_batch_id": "a" * 32}
                streamlit.columns.return_value = (MagicMock(), MagicMock())
                client = MagicMock()
                client.export.return_value = b"download"
                with patch.object(app, "st", streamlit), patch.object(app, "api_ui_enabled", return_value=True), \
                        patch.object(app, "_job_client", return_value=client):
                    app._render_exports(report, is_demo=False)
                expected_html = call("a" * 32, "html", comparison_id="b" * 32) if selected \
                    else call("a" * 32, "html")
                self.assertEqual(client.export.call_args_list,
                                 [call("a" * 32, "csv"), expected_html])


if __name__ == "__main__":
    unittest.main()
