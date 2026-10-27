"""Read-only, explicit-owner import of the previous SQLite history format.

This module does not infer an owner and never initializes or mutates the source
HistoryStore. The administration CLI chooses an account/workspace explicitly.
Legacy background jobs have a different recovery protocol and are not imported.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from core.history_store import HistoryStore, HistoryStoreError, HistoryCorruptionError, SCHEMA_VERSION


def _readonly(path):
    if path.is_symlink() or not path.is_file():
        raise HistoryStoreError("旧数据库路径无效，请提供原数据目录。")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _check_jobs(source):
    path = source / "analysis_jobs.sqlite3"
    if not path.exists():
        return
    with closing(_readonly(path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "analysis_jobs" not in tables:
            raise HistoryStoreError("旧任务数据库结构不受支持，请保留阶段二部署并先核对任务。")
        count = connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0]
        if count:
            raise HistoryStoreError("旧任务库含有任务；本工具只迁移历史，请保留阶段二部署并单独处理旧任务后再切换。")


def import_history(source_dir, history_store, *, verify_only=False):
    """Verify and atomically import legacy reports into the supplied workspace.

    Stop the old deployment during migration. SQLite is opened in read-only
    mode; original report/chat strings and their existing digests are retained.
    verify_only performs source and destination conflict checks without inserts.
    """
    source = Path(source_dir)
    if source.is_symlink():
        raise HistoryStoreError("旧数据目录不能为符号链接。")
    source = source.resolve()
    if source == history_store.root_dir or history_store.root_dir in source.parents:
        raise HistoryStoreError("旧数据目录必须与目标工作区分开。")
    try:
        _check_jobs(source)
        with closing(_readonly(source / "customerinsight.sqlite3")) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise HistoryStoreError("旧历史数据库版本不受支持，请保留原文件。")
            connection.execute("BEGIN")
            rows = [dict(row) for row in connection.execute("SELECT * FROM batches ORDER BY id")]
            mappings = [dict(row) for row in connection.execute("SELECT * FROM column_mappings ORDER BY signature")]
            # Reuse only pure validation/serialization helpers, not __init__.
            legacy = HistoryStore.__new__(HistoryStore)
            legacy.root_dir, legacy.originals_dir = source, source / "originals"
            originals = {}
            for row in rows:
                legacy._report_and_mapping(row)
                history_store._stored_chat(row)
                originals[row["id"]] = legacy._read_original_row(row)
            result = history_store.import_records(rows, mappings, originals, verify_only=verify_only)
            connection.rollback()
        return result
    except sqlite3.Error as exc:
        raise HistoryCorruptionError("旧历史数据库无法只读核对，未执行导入。") from exc
