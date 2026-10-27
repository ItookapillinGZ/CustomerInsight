from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4
import zipfile

from sqlalchemy import delete, select, update, create_engine

from backend.accounts import Workspace
from backend.database import Base, get_engine
from backend.import_legacy import import_history
from backend.pg_history import PgHistoryStore, history_batches, history_column_mappings
from core.analysis_agent import report_fingerprint
from core.history_store import HistoryStore, HistoryStoreError, HistoryNotFoundError, HistoryCorruptionError
from core.history_workflow import attach_historical_context
from tests.test_history_store import sample_report


class PgHistoryTests(unittest.TestCase):
    """Same storage contract on isolated SQLite and real PostgreSQL if supplied."""
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="customerinsight-pg-history-", dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        url = os.getenv("TEST_PG_URL")
        if url:
            # Private schema prevents concurrent agents' integration runs from
            # modifying or dropping each other's accounts/tables.
            self.schema = "test_history_" + uuid4().hex
            admin = get_engine(url)
            with admin.begin() as connection:
                connection.exec_driver_sql('CREATE SCHEMA "' + self.schema + '"')
            self.engine = create_engine(url, connect_args={"options": "-csearch_path=" + self.schema}, hide_parameters=True)
            def cleanup():
                self.engine.dispose()
                with admin.begin() as connection:
                    connection.exec_driver_sql('DROP SCHEMA "' + self.schema + '" CASCADE')
            self.addCleanup(cleanup)
        else:
            self.engine = get_engine("sqlite:///" + (self.root / "target.sqlite3").as_posix())
            self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.first_scope, self.second_scope = uuid4().hex, uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(Workspace.__table__.insert(), [
                {"id": self.first_scope, "created_at": 1.0},
                {"id": self.second_scope, "created_at": 1.0}])
        self.store = PgHistoryStore(self.engine, workspace_id=self.first_scope, data_dir=self.root / "data")
        self.other = PgHistoryStore(self.engine, workspace_id=self.second_scope, data_dir=self.root / "data")
        self.report = sample_report()
        self.mapping = {"text_column": "评价", "group_column": "门店", "sheet_name": "顾客评价"}
        self.original = "评价,门店\n杯子保温很好,甲店\n希望减少塑料包装,乙店\n".encode("utf-8-sig")

    def save(self, store=None, **overrides):
        values = {"filename": "客户评价.csv", "file_bytes": self.original, "mapping": self.mapping}
        values.update(overrides)
        return (store or self.store).save_batch(self.report, **values)

    def test_roundtrip_exact_codec_original_and_no_implicit_sqlite(self):
        identifier = self.save()
        restarted = PgHistoryStore(self.engine, workspace_id=self.first_scope, data_dir=self.root / "data")
        loaded = restarted.load_batch(identifier)
        self.assertEqual(loaded["original_bytes"], self.original)
        self.assertEqual(loaded["mapping"], self.mapping)
        self.assertEqual(loaded["report_fingerprint"], report_fingerprint(self.report))
        self.assertEqual(loaded["report"]["source_frame"].attrs["source_rows"], [2, 7])
        self.assertEqual(loaded["report"]["source_frame"].iloc[0]["评价"], "杯子保温很好")
        self.assertFalse((restarted.root_dir / "customerinsight.sqlite3").exists())

    def test_all_read_write_routes_deny_other_scope(self):
        identifier = self.save()
        for method in (self.other.load_batch, self.other.read_original, self.other.delete_batch, self.other.load_chat):
            with self.subTest(method=method.__name__), self.assertRaises(HistoryNotFoundError):
                method(identifier)
        with self.assertRaises(HistoryNotFoundError):
            self.other.save_chat(identifier, [])
        with self.assertRaises(HistoryNotFoundError):
            self.other.append_chat(identifier, {"question": "cross account"})
        with self.assertRaises(HistoryNotFoundError):
            self.save(self.other, batch_id=identifier)
        self.assertEqual(self.other.list_batches(), [])
        self.assertIsNone(self.other.find_batch(hashlib.sha256(self.original).hexdigest(), self.mapping))
        self.assertEqual(self.store.read_original(identifier), self.original)

    def test_deduplication_is_workspace_scoped_and_concurrent(self):
        def save(_):
            return self.save(PgHistoryStore(self.engine, workspace_id=self.first_scope, data_dir=self.root / "data"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(save, range(12)))
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)
        other_id = self.save(self.other)
        self.assertNotEqual(other_id, identifiers[0])
        self.assertNotEqual(self.store.originals_dir, self.other.originals_dir)
        self.assertEqual(self.other.read_original(other_id), self.original)

    def test_pagination_count_and_mapping_preferences_scoped(self):
        for index in range(5):
            self.save(file_bytes=f"review\n{index}\n".encode())
        self.save(self.other)
        page = self.store.page_batches(limit=2, offset=2)
        self.assertEqual((page["total"], len(page["items"]), page["offset"]), (5, 2, 2))
        self.assertEqual(self.other.page_batches()["total"], 1)
        self.store.save_mapping(["评价", "门店"], self.mapping)
        self.assertEqual(self.store.get_mapping(["评价", "门店"]), self.mapping)
        self.assertIsNone(self.other.get_mapping(["评价", "门店"]))
        self.assertIsNone(self.store.get_mapping(["门店", "评价"]))
        for limit, offset in ((True, 0), (0, 0), (1001, 0), (10, -1), (10, True)):
            with self.assertRaises(HistoryStoreError):
                self.store.page_batches(limit, offset)

    def test_chat_append_across_instances_keeps_each_turn_and_bounds_history(self):
        identifier = self.save()
        def append(index):
            store = PgHistoryStore(self.engine, workspace_id=self.first_scope, data_dir=self.root / "data")
            store.append_chat(identifier, {"question": str(index), "result": {
                "report_fingerprint": report_fingerprint(self.report)}}, max_turns=30)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(append, range(20)))
        self.assertEqual({turn["question"] for turn in self.store.load_chat(identifier)}, {str(i) for i in range(20)})
        self.store.append_chat(identifier, {"question": "latest"}, max_turns=3)
        self.assertEqual(len(self.store.load_chat(identifier)), 3)
        self.store.save_chat(identifier, [])
        self.assertEqual(self.store.load_chat(identifier), [])

    def test_chat_report_and_comparison_fingerprints_checked_in_scope(self):
        current = self.save()
        other_id = self.save(self.other)
        with self.assertRaises(HistoryStoreError):
            self.store.append_chat(current, {"question": "cross", "comparison_batch_id": other_id})
        with self.assertRaises(HistoryStoreError):
            self.store.append_chat(current, {"question": "stale", "result": {"report_fingerprint": "wrong"}})
        previous = self.save(file_bytes=b"review\nprevious\n")
        context = attach_historical_context(self.report, self.store.load_batch(previous))
        turn = {"question": "compare", "comparison_batch_id": previous,
                "result": {"report_fingerprint": report_fingerprint(context)}}
        self.store.append_chat(current, turn)
        self.assertEqual(self.store.load_chat(current), [turn])
        self.store.save_batch(sample_report("changed"), filename="previous.csv", file_bytes=b"review\nprevious\n", mapping=self.mapping)
        self.assertEqual(self.store.load_chat(current), [])

    def test_delete_purges_comparisons_and_preserves_other_accounts(self):
        current = self.save()
        previous = self.save(file_bytes=b"review\nprevious\n")
        other_id = self.save(self.other)
        context = attach_historical_context(self.report, self.store.load_batch(previous))
        self.store.append_chat(current, {"question": "compare", "comparison_batch_id": previous,
            "result": {"report_fingerprint": report_fingerprint(context)}})
        self.store.delete_batch(previous)
        self.assertEqual(self.store.load_chat(current), [])
        self.assertEqual(self.other.read_original(other_id), self.original)
        self.assertEqual(self.store.page_batches()["total"], 1)

    def test_backup_contains_only_own_verified_history_and_no_accounts(self):
        identifier = self.save()
        self.store.append_chat(identifier, {"question": "own question"})
        other_id = self.save(self.other, file_bytes=b"private_other_marker\n")
        with zipfile.ZipFile(io.BytesIO(self.store.export_backup())) as zipped:
            self.assertEqual(set(zipped.namelist()), {"history.json", "manifest.json", f"originals/{identifier}.csv"})
            history = zipped.read("history.json")
            self.assertNotIn(other_id.encode(), history)
            self.assertNotIn(b"private_other_marker", b"".join(zipped.read(name) for name in zipped.namelist()))
            self.assertNotIn(b"password_hash", history)
            self.assertNotIn(b"token_hash", history)
            self.assertEqual(json.loads(history)["workspace_id"], self.first_scope)

    def test_corrupt_original_report_chat_and_mapping_fail_closed(self):
        identifier = self.save()
        original_path = self.store.originals_dir / (identifier + ".csv")
        original_path.write_bytes(b"changed")
        with self.assertRaises(HistoryCorruptionError):
            self.store.read_original(identifier)
        original_path.write_bytes(self.original)
        with self.engine.begin() as connection:
            connection.execute(update(history_batches).where(history_batches.c.workspace_id == self.first_scope,
                history_batches.c.id == identifier).values(chat_json="{}"))
        with self.assertRaises(HistoryCorruptionError):
            self.store.load_chat(identifier)
        with self.assertRaises(HistoryCorruptionError):
            self.store.export_backup()

    def _legacy(self):
        legacy = HistoryStore(self.root / "legacy")
        identifier = legacy.save_batch(self.report, filename="legacy.csv", file_bytes=self.original, mapping=self.mapping)
        legacy.save_chat(identifier, [{"question": "legacy question"}])
        legacy.save_mapping(["评价", "门店"], self.mapping)
        return legacy, identifier

    def test_import_preserves_ids_raw_hashes_and_source_readonly(self):
        legacy, identifier = self._legacy()
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in legacy.root_dir.rglob("*") if path.is_file() and not path.name.endswith(("-wal", "-shm"))}
        result = import_history(legacy.root_dir, self.store)
        self.assertEqual((result["imported"], result["verified"]), (1, 1))
        self.assertEqual(self.store.read_original(identifier), self.original)
        self.assertEqual(self.store.load_chat(identifier), [{"question": "legacy question"}])
        with closing(sqlite3.connect(legacy.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            source = dict(connection.execute("SELECT * FROM batches WHERE id=?", (identifier,)).fetchone())
        with self.engine.connect() as connection:
            destination = dict(connection.execute(select(history_batches).where(
                history_batches.c.workspace_id == self.first_scope, history_batches.c.id == identifier)).mappings().one())
        destination.pop("workspace_id")
        self.assertEqual(source, destination)
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in legacy.root_dir.rglob("*") if path.is_file() and not path.name.endswith(("-wal", "-shm"))})
        self.assertEqual(import_history(legacy.root_dir, self.store)["skipped"], 1)
        # The same old ID can be imported into another account, without aliasing
        # a row or original belonging to the first account.
        self.assertEqual(import_history(legacy.root_dir, self.other)["imported"], 1)
        self.store.delete_batch(identifier)
        self.assertEqual(self.other.read_original(identifier), self.original)

    def test_import_verify_only_and_conflicts_leave_target_unchanged(self):
        legacy, identifier = self._legacy()
        result = import_history(legacy.root_dir, self.store, verify_only=True)
        self.assertEqual((result["imported"], result["verified"]), (0, 1))
        self.assertEqual(self.store.list_batches(), [])
        self.save()
        with self.assertRaises(HistoryStoreError):
            import_history(legacy.root_dir, self.store)
        self.assertEqual(self.store.page_batches()["total"], 1)
        self.assertEqual(len(list(self.store.originals_dir.iterdir())), 1)

    def test_import_rejects_legacy_jobs_without_moving_them(self):
        legacy, _identifier = self._legacy()
        with closing(sqlite3.connect(legacy.root_dir / "analysis_jobs.sqlite3")) as connection, connection:
            connection.execute("CREATE TABLE analysis_jobs (id TEXT PRIMARY KEY,status TEXT)")
            connection.execute("INSERT INTO analysis_jobs VALUES ('keep','completed')")
        with self.assertRaisesRegex(HistoryStoreError, "任务"):
            import_history(legacy.root_dir, self.store)
        self.assertEqual(self.store.list_batches(), [])
        self.assertTrue(legacy.db_path.exists())

    def test_import_bad_original_rolls_back_all_and_preserves_source(self):
        legacy, identifier = self._legacy()
        (legacy.originals_dir / (identifier + ".csv")).write_bytes(b"corrupt source")
        with self.assertRaises(HistoryCorruptionError):
            import_history(legacy.root_dir, self.store)
        self.assertEqual(self.store.list_batches(), [])
        self.assertEqual(list(self.store.originals_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
