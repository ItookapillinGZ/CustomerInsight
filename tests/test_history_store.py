from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from core.analysis_agent import report_fingerprint
from core.customer_report import build_customer_report
from core.history_workflow import attach_historical_context
from core.history_store import (
    HistoryStore, HistoryStoreError, HistoryNotFoundError, HistoryCorruptionError,
    MAX_ORIGINAL_BYTES, _pack, _unpack,
)


def sample_report(text="杯子保温很好"):
    source = pd.DataFrame({"评价": [text, "希望减少塑料包装"], "门店": ["甲店", "乙店"],
                           "日期": [date(2026, 10, 1), None], "金额": [np.float64(20.5), np.nan]})
    source.attrs["source_rows"] = [2, 7]
    results = pd.DataFrame([
        {"source_row": 2, "processing_status": "success", "sentiment": "positive",
         "category": "product_quality", "priority": "low", "summary": text},
        {"source_row": 7, "processing_status": "success", "sentiment": "neutral",
         "category": "suggestions", "priority": "low", "summary": "希望减塑"},
    ])
    report = build_customer_report(source, results, "评价", "门店")
    report["source_frame"] = source
    report["analysis_meta"] = {"model": "offline-fixture", "requests": 1}
    return report


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "history"
        self.store = HistoryStore(self.root)
        self.report = sample_report()
        self.mapping = {"text_column": "评价", "group_column": "门店", "sheet_name": "顾客评价"}
        self.file_bytes = "评价,门店\n杯子保温很好,甲店\n希望减少塑料包装,乙店\n".encode("utf-8-sig")

    def save(self, **overrides):
        kwargs = {"filename": "客户评价.csv", "file_bytes": self.file_bytes, "mapping": self.mapping}
        kwargs.update(overrides)
        return self.store.save_batch(self.report, **kwargs)

    def test_roundtrip_restart_original_and_all_report_frames(self):
        identifier = self.save()
        restarted = HistoryStore(self.root)
        loaded = restarted.load_batch(identifier)
        self.assertEqual(loaded["filename"], "客户评价.csv")
        self.assertEqual(loaded["original_bytes"], self.file_bytes)
        self.assertEqual(restarted.read_original(identifier), self.file_bytes)
        self.assertEqual(loaded["mapping"], self.mapping)
        self.assertEqual(loaded["report_fingerprint"], report_fingerprint(self.report))
        self.assertEqual(loaded["content_hash"], hashlib.sha256(self.file_bytes).hexdigest())
        for key, value in self.report.items():
            if isinstance(value, pd.DataFrame):
                assert_frame_equal(value, loaded["report"][key])
                self.assertEqual(value.attrs, loaded["report"][key].attrs)
            else:
                self.assertEqual(value, loaded["report"][key])
        self.assertEqual(loaded["report"]["source_frame"].attrs["source_rows"], [2, 7])
        self.assertEqual(loaded["report"]["source_frame"].iloc[0]["日期"], date(2026, 10, 1))

    def test_dataframe_nullable_category_datetime_index_and_attrs_roundtrip(self):
        frame = pd.DataFrame({
            "nullable": pd.Series([1, pd.NA], dtype="Int64"),
            "boolean": pd.Series([True, pd.NA], dtype="boolean"),
            "category": pd.Categorical(["好", None], categories=["好", "差"], ordered=True),
            "string": pd.Series(["甲", pd.NA], dtype="string"),
            "when": pd.to_datetime(["2026-10-20 12:01:01.123456789", None]).tz_localize("Asia/Shanghai"),
            "elapsed": pd.to_timedelta(["1 days", None]),
        })
        frame.index = pd.Index([8, 17], name="source", dtype="int64")
        frame.columns.name = "fields"
        frame.attrs = {"source_rows": [9, 18], "formula_cells": [(9, "when")], "date": date(2026, 10, 20)}
        restored = _unpack(_pack({"frame": frame}))["frame"]
        assert_frame_equal(frame, restored)
        self.assertEqual(frame.attrs, restored.attrs)

    def test_plain_dates_nan_numpy_and_reserved_tags_cannot_confuse_codec(self):
        values = {"date": date(2026, 10, 20), "dt": datetime(2026, 10, 20, 3, 4, tzinfo=timezone.utc),
                  "clock": time(3, 4, 5), "elapsed": timedelta(days=-3, seconds=6),
                  "npint": np.int64(7), "npfloat": np.float32(1.25), "npdate": np.datetime64("2026-10-20"),
                  "tuple": (1, "two"), "nan": float("nan"), "inf": float("inf"), "nat": pd.NaT,
                  "fake": {"t": "pickle", "value": "never execute"}}
        # datetime64[D] is a valid NumPy date; keep its storage unit explicitly.
        restored = _unpack(_pack(values))
        self.assertEqual(restored["date"], values["date"])
        self.assertEqual(restored["dt"], values["dt"])
        self.assertEqual(restored["clock"], values["clock"])
        self.assertEqual(restored["elapsed"], values["elapsed"])
        self.assertIsInstance(restored["npint"], np.int64)
        self.assertIsInstance(restored["npfloat"], np.float32)
        self.assertEqual(restored["npdate"], values["npdate"])
        self.assertEqual(restored["tuple"], values["tuple"])
        self.assertTrue(np.isnan(restored["nan"]))
        self.assertEqual(restored["inf"], float("inf"))
        self.assertIs(restored["nat"], pd.NaT)
        self.assertEqual(restored["fake"], values["fake"])

    def test_duplicate_file_mapping_upserts_and_preserves_matching_chat(self):
        identifier = self.save()
        turn = {"question": "主要认可什么？", "result": {"report_fingerprint": report_fingerprint(self.report), "answer": "保温"}}
        self.store.save_chat(identifier, [turn])
        reverse_mapping = dict(reversed(list(self.mapping.items())))
        self.assertEqual(self.save(mapping=reverse_mapping, filename="不同名称.csv"), identifier)
        self.assertEqual(len(self.store.list_batches()), 1)
        self.assertEqual(self.store.load_chat(identifier), [turn])
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)

    def test_reanalysis_updates_same_identity_and_clears_old_chat(self):
        identifier = self.save()
        self.store.save_chat(identifier, [{"question": "之前的问题"}])
        changed = sample_report("杯子不保温")
        updated = self.store.save_batch(changed, filename="客户评价.csv", file_bytes=self.file_bytes,
                                        mapping=self.mapping, batch_id=identifier)
        self.assertEqual(updated, identifier)
        self.assertEqual(self.store.load_chat(identifier), [])
        self.assertEqual(self.store.load_batch(identifier)["report"]["detail"].iloc[0]["评价"], "杯子不保温")

    def test_different_mapping_is_distinct_and_find_batch_is_exact(self):
        first = self.save()
        other_mapping = {**self.mapping, "group_column": None}
        second = self.save(mapping=other_mapping)
        self.assertNotEqual(first, second)
        digest = hashlib.sha256(self.file_bytes).hexdigest()
        self.assertEqual(self.store.find_batch(digest, self.mapping)["id"], first)
        self.assertEqual(self.store.find_batch(digest, other_mapping)["id"], second)
        self.assertIsNone(self.store.find_batch("0" * 64, self.mapping))
        with self.assertRaises(HistoryStoreError):
            self.save(batch_id=first, mapping=other_mapping)

    def test_mapping_memory_is_exact_ordered_schema_and_persistent(self):
        self.store.save_mapping(["评价", "门店"], self.mapping)
        self.assertEqual(HistoryStore(self.root).get_mapping(["评价", "门店"]), self.mapping)
        self.assertIsNone(self.store.get_mapping(["门店", "评价"]))
        self.assertIsNone(self.store.get_mapping(["评价", "门店", "日期"]))
        updated = {**self.mapping, "group_column": None}
        self.store.save_mapping(["评价", "门店"], updated)
        self.assertEqual(self.store.get_mapping(["评价", "门店"]), updated)

    def test_chat_is_batch_scoped_restartable_and_rejects_other_report(self):
        first = self.save()
        second = self.store.save_batch(sample_report("另一个商品"), filename="第二批.csv", file_bytes=b"review\nother\n", mapping=self.mapping)
        turns = [{"question": "包装怎么样？", "result": {"report_fingerprint": report_fingerprint(self.report), "conclusion": "希望减塑"}}]
        self.store.save_chat(first, turns)
        self.assertEqual(HistoryStore(self.root).load_chat(first), turns)
        self.assertEqual(self.store.load_chat(second), [])
        with self.assertRaises(HistoryStoreError):
            self.store.save_chat(second, turns)
        self.store.save_chat(first, [])
        self.assertEqual(self.store.load_chat(first), [])

    def test_explicit_historical_comparison_chat_roundtrip_and_scope(self):
        current = self.save()
        previous = self.store.save_batch(sample_report("上一批保温很好"), filename="上一批.csv",
                                        file_bytes=b"review\nprevious\n", mapping=self.mapping)
        context = attach_historical_context(self.report, self.store.load_batch(previous))
        turn = {"question": "与上一批有什么变化？", "comparison_batch_id": previous,
                "result": {"report_fingerprint": report_fingerprint(context), "conclusion": "本批有减塑需求"}}
        self.store.save_chat(current, [turn])
        self.assertEqual(HistoryStore(self.root).load_chat(current), [turn])
        with self.assertRaises(HistoryStoreError):
            self.store.save_chat(current, [{**turn, "comparison_batch_id": None}])
        with self.assertRaises(HistoryStoreError):
            self.store.save_chat(current, [{**turn, "result": {"report_fingerprint": report_fingerprint(self.report)}}])
        for other in (current, "0" * 32, "../../escape", [previous], True):
            with self.subTest(previous=other), self.assertRaises(HistoryStoreError):
                self.store.save_chat(current, [{**turn, "comparison_batch_id": other}])
        self.assertEqual(self.store.load_chat(current), [turn])

    def test_changed_or_deleted_comparison_does_not_restore_stale_chat(self):
        current = self.save()
        previous_bytes = b"review\nprevious\n"
        previous = self.store.save_batch(sample_report("上一批原文"), filename="上一批.csv",
                                        file_bytes=previous_bytes, mapping=self.mapping)
        def turn_for_previous():
            context = attach_historical_context(self.report, self.store.load_batch(previous))
            return {"question": "对比上批", "comparison_batch_id": previous,
                    "result": {"report_fingerprint": report_fingerprint(context)}}
        self.store.save_chat(current, [turn_for_previous()])
        self.store.save_batch(sample_report("上一批重新分析"), filename="上一批.csv",
                              file_bytes=previous_bytes, mapping=self.mapping, batch_id=previous)
        self.assertEqual(self.store.load_chat(current), [])
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            raw_chat = connection.execute("SELECT chat_json FROM batches WHERE id=?", (current,)).fetchone()[0]
            self.assertEqual(_unpack(raw_chat), [])
        self.assertEqual(self.store.load_batch(current)["id"], current)
        self.store.save_chat(current, [turn_for_previous()])
        self.store.delete_batch(previous)
        self.assertEqual(self.store.load_chat(current), [])
        self.assertEqual(self.store.load_batch(current)["id"], current)

    def test_delete_purges_dependent_quotes_from_new_backup_and_keeps_unrelated_chat(self):
        current = self.save()
        private_quote = "ONLY_DELETED_PREVIOUS_REVIEW_9f20"
        previous = self.store.save_batch(sample_report(private_quote), filename="上一批.csv",
                                        file_bytes=b"review\nprevious\n", mapping=self.mapping)
        context = attach_historical_context(self.report, self.store.load_batch(previous))
        dependent = {"question": "比较上批", "comparison_batch_id": previous,
                     "result": {"report_fingerprint": report_fingerprint(context), "conclusion": private_quote}}
        unrelated = {"question": "当前表格的问题", "result": {"report_fingerprint": report_fingerprint(self.report)}}
        self.store.save_chat(current, [unrelated, dependent])
        self.store.delete_batch(previous)
        self.assertEqual(self.store.load_chat(current), [unrelated])
        with zipfile.ZipFile(io.BytesIO(self.store.export_backup())) as zipped:
            self.assertNotIn(private_quote.encode(), zipped.read("customerinsight.sqlite3"))
            self.assertNotIn(f"originals/{previous}.csv", zipped.namelist())
    def test_delete_only_validated_batch_and_original_does_not_touch_neighbors(self):
        first = self.save()
        second = self.save(file_bytes=b"review\nsecond\n")
        unrelated = self.root / "keep.csv"
        unrelated.write_bytes(b"keep")
        self.store.delete_batch(first)
        self.assertEqual([item["id"] for item in self.store.list_batches()], [second])
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)
        with self.assertRaises(HistoryNotFoundError):
            self.store.load_batch(first)
        self.assertEqual(self.store.read_original(second), b"review\nsecond\n")

    def test_invalid_identifiers_paths_and_extensions_never_reach_files(self):
        first = self.save(filename="../../safe.csv")
        self.assertEqual(self.store.load_batch(first)["filename"], "safe.csv")
        self.assertEqual(len(list(self.store.originals_dir.glob("*.csv"))), 1)
        for identifier in ("../customerinsight", "' OR 1=1 --", "", "a" * 31, True):
            for function in (self.store.load_batch, self.store.delete_batch, self.store.read_original, self.store.load_chat):
                with self.subTest(identifier=identifier, method=function.__name__), self.assertRaises(HistoryStoreError):
                    function(identifier)
        for filename in (".env", "data.xlsm", "data.xls", "data.xlsx.exe", "\x00.csv"):
            with self.subTest(filename=filename), self.assertRaises(HistoryStoreError):
                self.save(filename=filename)
        self.assertEqual(len(self.store.list_batches()), 1)

    def test_original_size_limit_empty_and_arbitrary_objects_rejected(self):
        for content in (b"", b"a" * (MAX_ORIGINAL_BYTES + 1), "not bytes"):
            with self.assertRaises(HistoryStoreError):
                self.save(file_bytes=content)
        with self.assertRaises(HistoryStoreError):
            self.store.save_batch({"unsafe": object()}, filename="data.csv", file_bytes=b"a", mapping={})
        self.assertEqual(self.store.list_batches(), [])
        self.assertEqual(list(self.store.originals_dir.iterdir()), [])

    def test_missing_changed_and_path_tampered_original_fail_closed(self):
        identifier = self.save()
        path = next(self.store.originals_dir.iterdir())
        path.write_bytes(b"changed")
        with self.assertRaises(HistoryCorruptionError):
            self.store.load_batch(identifier)
        with self.assertRaises(HistoryCorruptionError):
            self.store.export_backup()
        path.unlink()
        with self.assertRaises(HistoryCorruptionError):
            self.store.read_original(identifier)
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute("UPDATE batches SET original_name=? WHERE id=?", ("../other.csv", identifier))
        with self.assertRaises(HistoryCorruptionError):
            self.store.delete_batch(identifier)

    def test_corrupt_report_checksum_and_unknown_serialization_version_fail_closed(self):
        identifier = self.save()
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute("UPDATE batches SET report_json=? WHERE id=?", ("{}", identifier))
        with self.assertRaises(HistoryCorruptionError):
            self.store.load_batch(identifier)
        for payload in ("{}", "[]", "null", "not JSON", json.dumps({"schema": "customerinsight-local-history", "version": 999, "value": None}),
                        json.dumps({"schema": "customerinsight-local-history", "version": 1, "value": {"t": "pickle", "value": "payload"}})):
            with self.assertRaises(HistoryCorruptionError):
                _unpack(payload)

    def test_unknown_database_version_is_preserved_and_rejected(self):
        identifier = self.save()
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaises(HistoryStoreError):
            HistoryStore(self.root)
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)
        self.assertEqual(self.store.read_original(identifier), self.file_bytes)

    def test_failed_delete_transaction_restores_original(self):
        identifier = self.save()
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.execute("CREATE TRIGGER block_delete BEFORE DELETE ON batches BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        with self.assertRaises(HistoryStoreError):
            self.store.delete_batch(identifier)
        self.assertEqual(self.store.read_original(identifier), self.file_bytes)
        self.assertEqual(len(self.store.list_batches()), 1)
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)

    def test_backup_is_consistent_restoreable_and_contains_no_credentials_or_usage(self):
        first = self.save()
        self.store.save_chat(first, [{"question": "hello"}])
        self.store.save_mapping(["评价", "门店"], self.mapping)
        second = self.save(file_bytes=b"review\nsecond\n")
        (self.root / ".env").write_text("KEY=secret", encoding="utf-8")
        (self.root / "product_usage.sqlite3").write_bytes(b"private usage ledger")
        archive = self.store.export_backup()
        restored_dir = Path(self.temp.name) / "restored"
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            names = zipped.namelist()
            self.assertEqual(len(names), 4)
            self.assertEqual(set(names), {"customerinsight.sqlite3", "manifest.json",
                f"originals/{first}.csv", f"originals/{second}.csv"})
            manifest = json.loads(zipped.read("manifest.json"))
            self.assertEqual(len(manifest["batches"]), 2)
            zipped.extractall(restored_dir)
        restored = HistoryStore(restored_dir)
        self.assertEqual(restored.read_original(first), self.file_bytes)
        self.assertEqual(restored.read_original(second), b"review\nsecond\n")
        self.assertEqual(restored.load_chat(first), [{"question": "hello"}])
        self.assertEqual(restored.get_mapping(["评价", "门店"]), self.mapping)
        self.assertEqual(len(restored.list_batches()), 2)

    def test_concurrent_writers_deduplicate_one_file_and_keep_distinct_files(self):
        def write(index):
            return HistoryStore(self.root).save_batch(self.report, filename="data.csv", file_bytes=self.file_bytes,
                mapping=dict(reversed(list(self.mapping.items()))) if index % 2 else self.mapping)
        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(write, range(12)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.store.list_batches()), 1)
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            different = list(pool.map(lambda i: self.store.save_batch(self.report, filename="data.csv",
                file_bytes=f"review\n{i}\n".encode(), mapping=self.mapping), range(8)))
        self.assertEqual(len(set(different)), 8)
        self.assertEqual(len(self.store.list_batches()), 9)

    def test_default_paths_honor_data_directory_then_history_parent(self):
        first = Path(self.temp.name) / "data-dir"
        second = Path(self.temp.name) / "from-db" / "chosen.sqlite3"
        with patch.dict(os.environ, {"APP_DATA_DIR": str(first), "APP_HISTORY_DB_PATH": str(second)}):
            self.assertEqual(HistoryStore().root_dir, first.resolve())
        with patch.dict(os.environ, {"APP_DATA_DIR": "", "APP_HISTORY_DB_PATH": str(second)}):
            fallback = HistoryStore()
            self.assertEqual(fallback.root_dir, second.parent.resolve())
            self.assertEqual(fallback.db_path.name, "customerinsight.sqlite3")

    def test_limit_validation_and_latest_first_metadata(self):
        first = self.save()
        second = self.save(file_bytes=b"review\nnext\n")
        listed = self.store.list_batches(limit=1)
        self.assertEqual(listed[0]["id"], second)
        self.assertEqual(listed[0]["total_rows"], 2)
        self.assertEqual(listed[0]["success_rows"], 2)
        self.assertEqual(listed[0]["analysis_meta"], self.report["analysis_meta"])
        self.assertNotEqual(first, second)
        for invalid in (True, 0, -1, 1001, "10"):
            with self.assertRaises(HistoryStoreError):
                self.store.list_batches(invalid)


if __name__ == "__main__":
    unittest.main()
