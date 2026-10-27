"""Account-scoped SQLAlchemy history, with verified local original files.

PostgreSQL is the production database. Tables are installed by Alembic, never
implicitly from a request. SQLite engines are supported only for isolated tests.
Every mutation locks the owning workspace before touching rows or originals;
this also makes chat append and report reanalysis safe across API processes.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import tempfile
from uuid import uuid4
import zipfile

from sqlalchemy import Column, ForeignKey, Index, String, Table, Text, UniqueConstraint, delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from backend.database import Base, get_engine, workspace_root
from backend.accounts import Workspace
from core.history_store import (
    HistoryStore, HistoryStoreError, HistoryNotFoundError, HistoryCorruptionError,
    MAX_ORIGINAL_BYTES, _HASH, _json, _pack, _sha, _unpack, _validate_id,
)


history_batches = Table(
    "history_batches", Base.metadata,
    Column("workspace_id", String(32), ForeignKey("app_workspaces.id", ondelete="CASCADE"), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("content_hash", String(64), nullable=False),
    Column("mapping_hash", String(64), nullable=False),
    Column("filename", Text, nullable=False),
    Column("original_name", String(40), nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("report_json", Text, nullable=False),
    Column("report_hash", String(64), nullable=False),
    Column("report_fingerprint", String(64), nullable=False),
    Column("mapping_json", Text, nullable=False),
    Column("chat_json", Text, nullable=False),
    Column("chat_hash", String(64), nullable=False),
    UniqueConstraint("workspace_id", "content_hash", "mapping_hash", name="uq_history_content_mapping"),
)
Index("ix_history_workspace_created", history_batches.c.workspace_id, history_batches.c.created_at, history_batches.c.id)

history_column_mappings = Table(
    "history_column_mappings", Base.metadata,
    Column("workspace_id", String(32), ForeignKey("app_workspaces.id", ondelete="CASCADE"), primary_key=True),
    Column("signature", String(64), primary_key=True),
    Column("columns_json", Text, nullable=False),
    Column("mapping_json", Text, nullable=False),
    Column("mapping_hash", String(64), nullable=False),
    Column("updated_at", Text, nullable=False),
)


class PgHistoryStore(HistoryStore):
    """HistoryStore-compatible repository, bound to exactly one workspace."""

    def __init__(self, engine=None, *, workspace_id: str, data_dir=None):
        self.workspace_id = _validate_id(workspace_id)
        self.engine = engine if engine is not None else get_engine()
        # Do not call HistoryStore.__init__: that would create a second SQLite
        # database and blur which store owns this account's reports.
        self.root_dir = Path(workspace_root(self.workspace_id, data_dir)).resolve()
        self.originals_dir = self.root_dir / "originals"
        if self.originals_dir.is_symlink() or (hasattr(self.originals_dir, "is_junction") and self.originals_dir.is_junction()):
            raise HistoryStoreError("历史原文件目录不能通过符号链接访问。")
        self.originals_dir.mkdir(parents=True, exist_ok=True)

    def _original_path(self, row):
        if hasattr(self.originals_dir, "is_junction") and self.originals_dir.is_junction():
            raise HistoryCorruptionError("历史原文件路径无效，未访问该文件。")
        path = super()._original_path(row)
        if hasattr(path, "is_junction") and path.is_junction():
            raise HistoryCorruptionError("历史原文件路径无效，未访问该文件。")
        return path

    def _scope(self):
        return history_batches.c.workspace_id == self.workspace_id

    @contextmanager
    def _connection(self, *, write=False):
        try:
            with self.engine.connect() as connection:
                if write and self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                try:
                    if write:
                        # The fixed table name is defined by the account schema.
                        # The row lock coordinates all files/chat mutations in
                        # this workspace without serializing unrelated users.
                        workspaces = Workspace.__table__
                        statement = select(workspaces.c.id).where(workspaces.c.id == self.workspace_id)
                        if self.engine.dialect.name != "sqlite":
                            statement = statement.with_for_update()
                        if connection.execute(statement).first() is None:
                            raise HistoryNotFoundError("所属工作区不存在。")
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except SQLAlchemyError as exc:
            raise HistoryStoreError("历史数据库暂时无法读写，请稍后重试。") from exc

    def _row(self, connection, batch_id):
        row = connection.execute(select(history_batches).where(
            self._scope(), history_batches.c.id == _validate_id(batch_id))).mappings().first()
        if row is None:
            raise HistoryNotFoundError("这份历史报告不存在，可能已删除。")
        return row

    def _write_original(self, identifier, suffix, data):
        """Publish a complete UUID original; return its path only if created."""
        original_name = identifier + suffix
        path = self._original_path({"id": identifier, "original_name": original_name})
        if path.exists():
            # A crash between filesystem publication and DB commit may leave an
            # orphan. Reusing identical bytes is safe; never overwrite others.
            if path.stat().st_size > MAX_ORIGINAL_BYTES or path.read_bytes() != data:
                raise HistoryCorruptionError("历史原文件编号冲突，未覆盖原文件。")
            return None
        descriptor, temporary = tempfile.mkstemp(prefix=".original-", dir=self.originals_dir)
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return path

    def save_batch(self, report, *, filename, file_bytes, mapping, batch_id=None):
        if type(report) is not dict or type(mapping) is not dict:
            raise HistoryStoreError("报告与字段配置必须为字典。")
        if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
            raise HistoryStoreError("原文件名称无效。")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(filename).suffix.lower()
        if suffix not in (".csv", ".xlsx"):
            raise HistoryStoreError("历史记录只保存 .xlsx 或 .csv 原文件。")
        if type(file_bytes) is not bytes or not file_bytes or len(file_bytes) > MAX_ORIGINAL_BYTES:
            raise HistoryStoreError("原文件为空或超过 5 MB，无法保存历史。")
        if batch_id is not None:
            _validate_id(batch_id)
        report_json, mapping_json = _pack(report), _pack(mapping)
        content_hash, mapping_hash = _sha(file_bytes), _sha(mapping_json.encode())
        from core.analysis_agent import report_fingerprint
        try:
            fingerprint = report_fingerprint(report)
        except (ValueError, TypeError, KeyError):
            fingerprint = _sha(report_json.encode())
        now, empty = datetime.now(timezone.utc).isoformat(), _pack([])
        created = None
        try:
            with self._connection(write=True) as connection:
                existing = connection.execute(select(history_batches).where(self._scope(),
                    history_batches.c.content_hash == content_hash,
                    history_batches.c.mapping_hash == mapping_hash)).mappings().first()
                if batch_id is not None:
                    specified = self._row(connection, batch_id)
                    if specified["content_hash"] != content_hash or specified["mapping_hash"] != mapping_hash:
                        raise HistoryStoreError("不能用其他原文件或字段配置覆盖这份历史报告。")
                    if existing is not None and existing["id"] != batch_id:
                        raise HistoryStoreError("这份文件已有其他历史记录。")
                    existing = specified
                if existing is not None:
                    self._read_original_row(existing)
                    values = {"report_json": report_json, "report_hash": _sha(report_json.encode()),
                              "report_fingerprint": fingerprint, "updated_at": now}
                    if fingerprint != existing["report_fingerprint"]:
                        self._purge_comparison_chat(connection, existing["id"])
                        values.update(chat_json=empty, chat_hash=_sha(empty.encode()))
                    connection.execute(update(history_batches).where(self._scope(),
                        history_batches.c.id == existing["id"]).values(**values))
                    return existing["id"]
                identifier = uuid4().hex
                created = self._write_original(identifier, suffix, file_bytes)
                connection.execute(history_batches.insert().values(
                    workspace_id=self.workspace_id, id=identifier, content_hash=content_hash,
                    mapping_hash=mapping_hash, filename=filename, original_name=identifier + suffix,
                    created_at=now, updated_at=now, report_json=report_json,
                    report_hash=_sha(report_json.encode()), report_fingerprint=fingerprint,
                    mapping_json=mapping_json, chat_json=empty, chat_hash=_sha(empty.encode())))
                return identifier
        except BaseException:
            if created is not None:
                created.unlink(missing_ok=True)
            raise

    def load_batch(self, batch_id):
        with self._connection(write=True) as connection:
            row = self._row(connection, batch_id)
            report, mapping = self._report_and_mapping(row)
            return {**self._metadata(row, report, mapping), "report": report,
                    "original_bytes": self._read_original_row(row)}

    def read_original(self, batch_id):
        with self._connection(write=True) as connection:
            return self._read_original_row(self._row(connection, batch_id))

    def page_batches(self, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise HistoryStoreError("历史列表分页参数无效。")
        with self._connection() as connection:
            total = connection.execute(select(func.count()).select_from(history_batches).where(self._scope())).scalar_one()
            rows = connection.execute(select(history_batches).where(self._scope()).order_by(
                history_batches.c.created_at.desc(), history_batches.c.id.desc()).limit(limit).offset(offset)).mappings()
            return {"items": [self._metadata(row, *self._report_and_mapping(row)) for row in rows],
                    "total": total, "limit": limit, "offset": offset}

    def list_batches(self, limit=50):
        return self.page_batches(limit=limit)["items"]

    def find_batch(self, content_hash, mapping):
        if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash) or type(mapping) is not dict:
            raise HistoryStoreError("文件校验值或字段配置无效。")
        with self._connection() as connection:
            row = connection.execute(select(history_batches).where(self._scope(),
                history_batches.c.content_hash == content_hash,
                history_batches.c.mapping_hash == _sha(_pack(mapping).encode()))).mappings().first()
            return self._metadata(row, *self._report_and_mapping(row)) if row else None

    def _purge_comparison_chat(self, connection, previous_id):
        rows = connection.execute(select(history_batches).where(self._scope(),
            history_batches.c.id != previous_id,
            history_batches.c.chat_json.contains(previous_id))).mappings().all()
        for row in rows:
            turns = self._stored_chat(row)
            kept = [turn for turn in turns if turn.get("comparison_batch_id") != previous_id]
            if len(kept) != len(turns):
                self._write_chat(connection, row["id"], kept)

    def delete_batch(self, batch_id):
        moved = None
        try:
            with self._connection(write=True) as connection:
                row = self._row(connection, batch_id)
                path = self._original_path(row)
                self._purge_comparison_chat(connection, batch_id)
                if path.exists():
                    moved = (path, self.originals_dir / (uuid4().hex + ".deleted"))
                    path.rename(moved[1])
                connection.execute(delete(history_batches).where(self._scope(), history_batches.c.id == batch_id))
        except BaseException:
            if moved is not None and moved[1].exists():
                moved[1].rename(moved[0])
            raise
        if moved is not None:
            try:
                moved[1].unlink()
            except OSError as exc:
                raise HistoryStoreError("历史记录已删除，但原文件清理未完成。") from exc

    @staticmethod
    def _stored_chat(row):
        if _sha(row["chat_json"].encode()) != row["chat_hash"]:
            raise HistoryCorruptionError("历史对话校验失败，未加载。")
        turns = _unpack(row["chat_json"])
        if type(turns) is not list or any(type(turn) is not dict for turn in turns):
            raise HistoryCorruptionError("历史对话格式无效。")
        return turns

    def _valid_turns(self, connection, row, turns, *, strict):
        fingerprints, valid = {None: row["report_fingerprint"]}, []
        for turn in turns:
            if type(turn) is not dict:
                raise HistoryStoreError("对话条目格式无效。")
            previous_id = turn.get("comparison_batch_id")
            try:
                if previous_id is not None:
                    _validate_id(previous_id)
                if previous_id not in fingerprints:
                    fingerprints[previous_id] = self._chat_fingerprint(connection, row, previous_id)
                result = turn.get("result", turn)
                if type(result) is dict and result.get("report_fingerprint") not in (None, fingerprints[previous_id]):
                    raise HistoryStoreError("对话属于另一份分析报告，未保存。")
            except HistoryStoreError:
                if strict:
                    raise
                continue
            valid.append(turn)
        return valid

    def _write_chat(self, connection, batch_id, turns):
        payload = _pack(turns)
        connection.execute(update(history_batches).where(self._scope(), history_batches.c.id == batch_id).values(
            chat_json=payload, chat_hash=_sha(payload.encode())))

    def save_chat(self, batch_id, turns):
        if type(turns) is not list:
            raise HistoryStoreError("对话记录必须为列表。")
        with self._connection(write=True) as connection:
            row = self._row(connection, batch_id)
            self._report_and_mapping(row)
            self._valid_turns(connection, row, turns, strict=True)
            self._write_chat(connection, batch_id, turns)

    def append_chat(self, batch_id, turn, max_turns=30):
        """Append under the database workspace lock, avoiding lost updates."""
        if type(max_turns) is not int or not 1 <= max_turns <= 1000:
            raise HistoryStoreError("对话保存数量无效。")
        with self._connection(write=True) as connection:
            row = self._row(connection, batch_id)
            self._report_and_mapping(row)
            self._valid_turns(connection, row, [turn], strict=True)
            turns = self._valid_turns(connection, row, self._stored_chat(row), strict=False)
            turns.append(turn)
            self._write_chat(connection, batch_id, turns[-max_turns:])

    def clear_chat(self, batch_id, comparison_id=None):
        """Clear one conversation scope under the same lock as append_chat."""
        with self._connection(write=True) as connection:
            row = self._row(connection, batch_id)
            if comparison_id is not None:
                self._row(connection, comparison_id)
            turns = self._stored_chat(row)
            self._write_chat(connection, batch_id, [turn for turn in turns
                if turn.get("comparison_batch_id") != comparison_id])

    def load_chat(self, batch_id):
        with self._connection(write=True) as connection:
            row = self._row(connection, batch_id)
            self._report_and_mapping(row)
            return self._valid_turns(connection, row, self._stored_chat(row), strict=False)

    def save_mapping(self, columns, mapping):
        if type(mapping) is not dict:
            raise HistoryStoreError("字段配置必须为字典。")
        columns_json, payload = self._columns(columns), _pack(mapping)
        signature = _sha(columns_json.encode())
        table = history_column_mappings
        with self._connection(write=True) as connection:
            condition = (table.c.workspace_id == self.workspace_id, table.c.signature == signature)
            values = dict(columns_json=columns_json, mapping_json=payload,
                          mapping_hash=_sha(payload.encode()), updated_at=datetime.now(timezone.utc).isoformat())
            if connection.execute(select(table.c.signature).where(*condition)).first() is None:
                connection.execute(table.insert().values(workspace_id=self.workspace_id, signature=signature, **values))
            else:
                connection.execute(update(table).where(*condition).values(**values))

    def get_mapping(self, columns):
        payload = self._columns(columns)
        table = history_column_mappings
        with self._connection() as connection:
            row = connection.execute(select(table).where(table.c.workspace_id == self.workspace_id,
                table.c.signature == _sha(payload.encode()))).mappings().first()
            if row is None:
                return None
            if row["columns_json"] != payload or _sha(row["mapping_json"].encode()) != row["mapping_hash"]:
                raise HistoryCorruptionError("已保存的字段配置校验失败。")
            result = _unpack(row["mapping_json"])
            if type(result) is not dict:
                raise HistoryCorruptionError("已保存的字段配置格式无效。")
            return result

    def export_backup(self):
        """Export this workspace's JSON and verified originals, not a PG dump."""
        archive = io.BytesIO()
        with self._connection(write=True) as connection:
            rows = connection.execute(select(history_batches).where(self._scope()).order_by(history_batches.c.id)).mappings().all()
            mappings = connection.execute(select(history_column_mappings).where(
                history_column_mappings.c.workspace_id == self.workspace_id)).mappings().all()
            records, manifest = [], []
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                for row in rows:
                    self._report_and_mapping(row)
                    self._stored_chat(row)
                    data = self._read_original_row(row)
                    records.append(dict(row))
                    zipped.writestr("originals/" + row["original_name"], data)
                    manifest.append({"id": row["id"], "filename": row["filename"], "sha256": row["content_hash"]})
                for row in mappings:
                    if _sha(row["mapping_json"].encode()) != row["mapping_hash"] or type(_unpack(row["mapping_json"])) is not dict:
                        raise HistoryCorruptionError("已保存的字段配置校验失败。")
                zipped.writestr("history.json", _json({"schema": "customerinsight-workspace-history", "version": 1,
                    "workspace_id": self.workspace_id, "batches": records, "column_mappings": [dict(row) for row in mappings]}))
                zipped.writestr("manifest.json", _json({"schema": "customerinsight-workspace-backup", "version": 1,
                    "workspace_id": self.workspace_id, "created_at": datetime.now(timezone.utc).isoformat(),
                    "scope": "history-and-originals-only", "batches": manifest}))
        return archive.getvalue()

    def import_records(self, records, mappings, originals, *, verify_only=False):
        """Import already verified legacy rows verbatim in one transaction.

        Kept separate from save_batch so old IDs, timestamps, serialized report
        bytes and chat digests survive migration exactly. No existing report or
        remembered mapping is overwritten on conflict.
        """
        created_files, added, skipped = [], 0, 0
        record_keys = set(history_batches.c.keys()) - {"workspace_id"}
        mapping_keys = set(history_column_mappings.c.keys()) - {"workspace_id"}
        for row in records:
            if set(row) != record_keys:
                raise HistoryCorruptionError("旧历史记录字段与受支持版本不一致。")
            _validate_id(row["id"])
            self._report_and_mapping(row)
            self._stored_chat(row)
            data = originals.get(row["id"])
            if type(data) is not bytes or not data or len(data) > MAX_ORIGINAL_BYTES or _sha(data) != row["content_hash"]:
                raise HistoryCorruptionError("旧历史原文件校验失败。")
            self._original_path(row)
        for row in mappings:
            if set(row) != mapping_keys or _sha(row["columns_json"].encode()) != row["signature"]:
                raise HistoryCorruptionError("旧字段配置结构校验失败。")
            self._columns(_unpack(row["columns_json"]))
            if _sha(row["mapping_json"].encode()) != row["mapping_hash"] or type(_unpack(row["mapping_json"])) is not dict:
                raise HistoryCorruptionError("旧字段配置校验失败。")
        try:
            with self._connection(write=True) as connection:
                # Preflight every conflict before publishing any file or row.
                new_records, new_mappings = [], []
                for row in records:
                    candidates = connection.execute(select(history_batches).where(self._scope(),
                        (history_batches.c.id == row["id"]) | (
                            (history_batches.c.content_hash == row["content_hash"]) &
                            (history_batches.c.mapping_hash == row["mapping_hash"])))).mappings().all()
                    if candidates:
                        if len(candidates) != 1 or {key: candidates[0][key] for key in record_keys} != row:
                            raise HistoryStoreError("目标工作区存在冲突历史，已停止导入且未覆盖。")
                        self._read_original_row(candidates[0])
                        skipped += 1
                    else:
                        new_records.append(row)
                table = history_column_mappings
                for row in mappings:
                    current = connection.execute(select(table).where(table.c.workspace_id == self.workspace_id,
                        table.c.signature == row["signature"])).mappings().first()
                    if current is not None:
                        if {key: current[key] for key in mapping_keys} != row:
                            raise HistoryStoreError("目标工作区已有不同字段配置，已停止导入且未覆盖。")
                    else:
                        new_mappings.append(row)
                if not verify_only:
                    for row in new_records:
                        created = self._write_original(row["id"], Path(row["original_name"]).suffix, originals[row["id"]])
                        if created is not None:
                            created_files.append(created)
                        connection.execute(history_batches.insert().values(workspace_id=self.workspace_id, **row))
                        added += 1
                    for row in new_mappings:
                        connection.execute(table.insert().values(workspace_id=self.workspace_id, **row))
                    # Verify stored raw strings and original digests before the
                    # transaction commits, rather than checking decoded values.
                    for expected in records:
                        actual = self._row(connection, expected["id"])
                        if {key: actual[key] for key in record_keys} != expected:
                            raise HistoryCorruptionError("导入后的历史记录校验失败，已回滚。")
                        self._read_original_row(actual)
        except BaseException:
            for path in created_files:
                path.unlink(missing_ok=True)
            raise
        return {"workspace_id": self.workspace_id, "verified": len(records), "imported": added,
                "skipped": skipped, "mappings": len(mappings), "verify_only": verify_only}
