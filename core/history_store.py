"""Local, single-workspace history. No executable or pickle serialization.

The database and its UUID-named originals belong to the deployment owner. This
is persistence for one trusted workspace, not an account or tenant boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import uuid4
import zipfile

import numpy as np
import pandas as pd


SCHEMA_VERSION = 1
MAX_ORIGINAL_BYTES = 5 * 1024 * 1024
MAX_SERIALIZED_BYTES = 50 * 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DTYPE = re.compile(
    r"(?:object|bool|boolean|string|str|(?:u?int|float|complex)(?:8|16|32|64|128|256)?|"
    r"(?:U?Int|Float)(?:8|16|32|64)|datetime64\[(?:s|ms|us|ns)(?:, [^\]\x00]{1,100})?\]|"
    r"timedelta64\[(?:s|ms|us|ns)\])\Z"
)


class HistoryStoreError(ValueError):
    """A safe, user-readable history storage failure."""


class HistoryNotFoundError(HistoryStoreError):
    """The requested local batch does not exist."""


class HistoryCorruptionError(HistoryStoreError):
    """Stored data no longer matches its declared schema or digest."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _dtype(value) -> str:
    name = str(value)
    if not _DTYPE.fullmatch(name):
        raise HistoryStoreError("历史数据包含暂不支持的数据类型，请先转换为普通表格字段。")
    return name


def _encode_index(index, depth):
    if isinstance(index, pd.RangeIndex):
        return {"kind": "range", "start": index.start, "stop": index.stop, "step": index.step,
                "name": _encode(index.name, depth + 1)}
    if isinstance(index, pd.MultiIndex):
        return {"kind": "multi", "levels": [_encode_index(item, depth + 1) for item in index.levels],
                "codes": [item.tolist() for item in index.codes], "names": _encode(list(index.names), depth + 1)}
    return {"kind": "index", "dtype": _dtype(index.dtype), "name": _encode(index.name, depth + 1),
            "values": [_encode(item, depth + 1) for item in index.tolist()]}


def _encode(value, depth=0):
    if depth > 80:
        raise HistoryStoreError("历史数据嵌套过深，无法安全保存。")
    if value is None or type(value) in (bool, int, str):
        return value
    if value is pd.NA:
        return {"t": "pd_na"}
    if value is pd.NaT:
        return {"t": "pd_nat"}
    if isinstance(value, pd.Timestamp):
        scalar = value.asm8
        return {"t": "timestamp", "value": int(scalar.view("i8")),
                "unit": np.datetime_data(scalar.dtype)[0], "tz": str(value.tz) if value.tz is not None else None}
    if isinstance(value, pd.Timedelta):
        return {"t": "pd_timedelta", "value": int(value.value)}
    if isinstance(value, np.generic):
        if value.dtype.kind not in "biufcMmUS":
            raise HistoryStoreError("历史数据包含不支持的数值类型。")
        if value.dtype.kind in "Mm":
            return {"t": "np_time", "dtype": str(value.dtype), "value": int(value.view("i8"))}
        return {"t": "np_scalar", "dtype": str(value.dtype), "value": _encode(value.item(), depth + 1)}
    if type(value) is float:
        return value if math.isfinite(value) else {"t": "float", "value": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if type(value) is complex:
        return {"t": "complex", "real": _encode(value.real, depth + 1), "imag": _encode(value.imag, depth + 1)}
    if isinstance(value, datetime):
        return {"t": "datetime", "value": value.isoformat(), "fold": value.fold}
    if isinstance(value, date):
        return {"t": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"t": "time", "value": value.isoformat(), "fold": value.fold}
    if isinstance(value, timedelta):
        return {"t": "timedelta", "days": value.days, "seconds": value.seconds, "microseconds": value.microseconds}
    if isinstance(value, pd.DataFrame):
        dtypes = []
        for _, series in value.items():
            if isinstance(series.dtype, pd.CategoricalDtype):
                dtypes.append({"category": _encode_index(series.cat.categories, depth + 1), "ordered": series.cat.ordered})
            else:
                dtypes.append({"dtype": _dtype(series.dtype)})
        return {"t": "frame", "index": _encode_index(value.index, depth + 1),
                "columns": _encode_index(value.columns, depth + 1), "dtypes": dtypes,
                "data": [[_encode(item, depth + 1) for item in value.iloc[:, position].tolist()]
                         for position in range(len(value.columns))], "attrs": _encode(value.attrs, depth + 1)}
    if type(value) is dict:
        entries = [[_encode(key, depth + 1), _encode(item, depth + 1)] for key, item in value.items()]
        entries.sort(key=lambda pair: _json(pair[0]))
        return {"t": "dict", "value": entries}
    if type(value) in (list, tuple):
        return {"t": "tuple" if type(value) is tuple else "list", "value": [_encode(item, depth + 1) for item in value]}
    raise HistoryStoreError("历史数据包含不能安全保存的对象。")


def _decode_index(item, depth):
    kind = item["kind"]
    if kind == "range":
        return pd.RangeIndex(item["start"], item["stop"], item["step"], name=_decode(item["name"], depth + 1))
    if kind == "multi":
        return pd.MultiIndex(levels=[_decode_index(level, depth + 1) for level in item["levels"]],
                             codes=item["codes"], names=_decode(item["names"], depth + 1), verify_integrity=True)
    if kind == "index":
        return pd.Index([_decode(value, depth + 1) for value in item["values"]], dtype=_dtype(item["dtype"]),
                        name=_decode(item["name"], depth + 1), tupleize_cols=False)
    raise HistoryCorruptionError("历史表格索引格式无效。")


def _decode(value, depth=0):
    if depth > 80:
        raise HistoryCorruptionError("历史数据嵌套格式无效。")
    if value is None or type(value) in (str, bool, int, float):
        return value
    if type(value) is not dict:
        raise HistoryCorruptionError("历史数据格式无效。")
    tag = value.get("t")
    if tag == "pd_na":
        return pd.NA
    if tag == "pd_nat":
        return pd.NaT
    if tag == "float":
        return {"nan": float("nan"), "inf": float("inf"), "-inf": -float("inf")}[value["value"]]
    if tag == "complex":
        return complex(_decode(value["real"], depth + 1), _decode(value["imag"], depth + 1))
    if tag == "timestamp":
        if value["unit"] not in ("s", "ms", "us", "ns"):
            raise HistoryCorruptionError("历史时间格式无效。")
        return pd.Timestamp(value["value"], unit=value["unit"], tz=value["tz"])
    if tag == "pd_timedelta":
        return pd.Timedelta(value["value"], unit="ns")
    if tag == "np_scalar":
        dtype = np.dtype(value["dtype"])
        if dtype.kind not in "biufcUS" or dtype.itemsize > 8192:
            raise HistoryCorruptionError("历史数值类型无效。")
        return dtype.type(_decode(value["value"], depth + 1))
    if tag == "np_time":
        dtype = np.dtype(value["dtype"])
        if dtype.kind not in "Mm" or dtype.itemsize != 8:
            raise HistoryCorruptionError("历史时间类型无效。")
        return np.array(value["value"], dtype="i8").view(dtype)[()]
    if tag == "datetime":
        return datetime.fromisoformat(value["value"]).replace(fold=value["fold"])
    if tag == "date":
        return date.fromisoformat(value["value"])
    if tag == "time":
        return time.fromisoformat(value["value"]).replace(fold=value["fold"])
    if tag == "timedelta":
        return timedelta(days=value["days"], seconds=value["seconds"], microseconds=value["microseconds"])
    if tag in ("list", "tuple"):
        result = [_decode(item, depth + 1) for item in value["value"]]
        return tuple(result) if tag == "tuple" else result
    if tag == "dict":
        result = {}
        for key, item in value["value"]:
            key = _decode(key, depth + 1)
            if key in result:
                raise HistoryCorruptionError("历史数据包含重复字段。")
            result[key] = _decode(item, depth + 1)
        return result
    if tag == "frame":
        index, columns = _decode_index(value["index"], depth + 1), _decode_index(value["columns"], depth + 1)
        if len(columns) != len(value["dtypes"]) or len(columns) != len(value["data"]):
            raise HistoryCorruptionError("历史表格字段数不一致。")
        series = []
        for cells, descriptor in zip(value["data"], value["dtypes"]):
            if len(cells) != len(index):
                raise HistoryCorruptionError("历史表格行数不一致。")
            dtype = (pd.CategoricalDtype(_decode_index(descriptor["category"], depth + 1), descriptor["ordered"])
                     if "category" in descriptor else _dtype(descriptor["dtype"]))
            series.append(pd.Series([_decode(item, depth + 1) for item in cells], dtype=dtype))
        result = pd.concat(series, axis=1) if series else pd.DataFrame(index=range(len(index)))
        result.index, result.columns = index, columns
        attrs = _decode(value["attrs"], depth + 1)
        if type(attrs) is not dict:
            raise HistoryCorruptionError("历史表格元数据格式无效。")
        result.attrs = attrs
        return result
    raise HistoryCorruptionError("历史数据包含未知格式，未加载。")


def _pack(value) -> str:
    result = _json({"schema": "customerinsight-local-history", "version": SCHEMA_VERSION, "value": _encode(value)})
    if len(result.encode("utf-8")) > MAX_SERIALIZED_BYTES:
        raise HistoryStoreError("历史分析结果超过本机保存上限。")
    return result


def _unpack(raw: str):
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_SERIALIZED_BYTES:
            raise HistoryCorruptionError("历史数据长度无效。")
        content = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(content) is not dict:
            raise HistoryCorruptionError("历史数据格式无效。")
        if content.get("schema") != "customerinsight-local-history" or content.get("version") != SCHEMA_VERSION:
            raise HistoryCorruptionError("历史数据版本不受支持。")
        return _decode(content["value"])
    except HistoryStoreError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        raise HistoryCorruptionError("历史数据损坏，未加载；请保留备份。") from exc


def _validate_id(batch_id: str) -> str:
    if not isinstance(batch_id, str) or not _ID.fullmatch(batch_id):
        raise HistoryStoreError("历史记录编号无效。")
    return batch_id


class HistoryStore:
    """Persistent reports, original files, column preferences and per-batch chat."""

    def __init__(self, root_dir=None):
        if root_dir is None:
            root_dir = os.getenv("APP_DATA_DIR") or (Path(os.environ["APP_HISTORY_DB_PATH"]).parent
                                                     if os.getenv("APP_HISTORY_DB_PATH") else Path(".runtime") / "history")
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.originals_dir = self.root_dir / "originals"
        self.db_path = self.root_dir / "customerinsight.sqlite3"
        if self.originals_dir.is_symlink() or self.db_path.is_symlink():
            raise HistoryStoreError("历史目录不能通过符号链接指向其他文件。")
        self.originals_dir.mkdir(exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise HistoryStoreError("历史数据库版本不受支持，请保留原文件并更新应用。")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, mapping_hash TEXT NOT NULL,
                    filename TEXT NOT NULL, original_name TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, report_json TEXT NOT NULL, report_hash TEXT NOT NULL,
                    report_fingerprint TEXT NOT NULL, mapping_json TEXT NOT NULL,
                    chat_json TEXT NOT NULL, chat_hash TEXT NOT NULL,
                    UNIQUE(content_hash, mapping_hash)
                );
                CREATE TABLE IF NOT EXISTS column_mappings (
                    signature TEXT PRIMARY KEY, columns_json TEXT NOT NULL, mapping_json TEXT NOT NULL,
                    mapping_hash TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=15)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise HistoryStoreError("历史数据库无法读写，请检查本机数据目录。") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _row(self, connection, batch_id):
        row = connection.execute("SELECT * FROM batches WHERE id=?", (_validate_id(batch_id),)).fetchone()
        if row is None:
            raise HistoryNotFoundError("这份历史报告不存在，可能已删除。")
        return row

    def _original_path(self, row):
        identifier = _validate_id(row["id"])
        name = row["original_name"]
        if name not in (identifier + ".xlsx", identifier + ".csv"):
            raise HistoryCorruptionError("历史原文件路径无效，未访问该文件。")
        path = self.originals_dir / name
        if self.originals_dir.is_symlink() or path.is_symlink() or path.resolve().parent != self.originals_dir:
            raise HistoryCorruptionError("历史原文件路径无效，未访问该文件。")
        return path

    def _read_original_row(self, row):
        path = self._original_path(row)
        try:
            if path.stat().st_size > MAX_ORIGINAL_BYTES:
                raise HistoryCorruptionError("历史原文件大小异常，未加载。")
            data = path.read_bytes()
        except OSError as exc:
            raise HistoryCorruptionError("历史原文件缺失或无法读取。") from exc
        if not _HASH.fullmatch(row["content_hash"]) or _sha(data) != row["content_hash"]:
            raise HistoryCorruptionError("历史原文件校验失败，未加载。")
        return data

    def _report_and_mapping(self, row):
        if _sha(row["report_json"].encode("utf-8")) != row["report_hash"]:
            raise HistoryCorruptionError("历史分析结果校验失败，未加载。")
        if _sha(row["mapping_json"].encode("utf-8")) != row["mapping_hash"]:
            raise HistoryCorruptionError("历史字段配置校验失败，未加载。")
        report, mapping = _unpack(row["report_json"]), _unpack(row["mapping_json"])
        if type(report) is not dict or type(mapping) is not dict:
            raise HistoryCorruptionError("历史报告格式无效。")
        return report, mapping

    @staticmethod
    def _metadata(row, report, mapping):
        metrics = report.get("metrics", {})
        return {"id": row["id"], "filename": row["filename"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "total_rows": metrics.get("total_rows", 0),
                "success_rows": metrics.get("success_rows", 0), "text_column": report.get("text_column"),
                "group_column": report.get("group_column"), "mapping": mapping, "content_hash": row["content_hash"],
                "analysis_meta": report.get("analysis_meta", {}), "report_fingerprint": row["report_fingerprint"]}

    def save_batch(self, report: dict, *, filename: str, file_bytes: bytes, mapping: dict,
                   batch_id: str | None = None) -> str:
        if type(report) is not dict or type(mapping) is not dict:
            raise HistoryStoreError("报告与字段配置必须为字典。")
        if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
            raise HistoryStoreError("原文件名称无效。")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(filename).suffix.lower()
        if suffix not in (".xlsx", ".csv"):
            raise HistoryStoreError("历史记录只保存 .xlsx 或 .csv 原文件。")
        if type(file_bytes) is not bytes or not file_bytes or len(file_bytes) > MAX_ORIGINAL_BYTES:
            raise HistoryStoreError("原文件为空或超过 5 MB，无法保存历史。")
        if batch_id is not None:
            _validate_id(batch_id)
        report_json, mapping_json = _pack(report), _pack(mapping)
        content_hash, mapping_hash = _sha(file_bytes), _sha(mapping_json.encode("utf-8"))
        from core.analysis_agent import report_fingerprint
        try:
            fingerprint = report_fingerprint(report)
        except (ValueError, TypeError, KeyError):
            fingerprint = _sha(report_json.encode("utf-8"))
        now, empty_chat = datetime.now(timezone.utc).isoformat(), _pack([])
        created_file = None
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT * FROM batches WHERE content_hash=? AND mapping_hash=?",
                                              (content_hash, mapping_hash)).fetchone()
                if batch_id is not None:
                    specified = self._row(connection, batch_id)
                    if specified["content_hash"] != content_hash or specified["mapping_hash"] != mapping_hash:
                        raise HistoryStoreError("不能用其他原文件或字段配置覆盖这份历史报告。")
                    if existing is not None and existing["id"] != batch_id:
                        raise HistoryStoreError("这份文件已有其他历史记录。")
                    existing = specified
                if existing is not None:
                    self._read_original_row(existing)
                    changed = fingerprint != existing["report_fingerprint"]
                    if changed:
                        self._purge_comparison_chat(connection, existing["id"])
                    connection.execute("""UPDATE batches SET report_json=?,report_hash=?,report_fingerprint=?,updated_at=?,
                        chat_json=CASE WHEN ? THEN ? ELSE chat_json END,
                        chat_hash=CASE WHEN ? THEN ? ELSE chat_hash END WHERE id=?""",
                        (report_json, _sha(report_json.encode("utf-8")), fingerprint, now, changed, empty_chat,
                         changed, _sha(empty_chat.encode("utf-8")), existing["id"]))
                    return existing["id"]
                identifier = uuid4().hex
                original_name = identifier + suffix
                if self.originals_dir.is_symlink() or self.originals_dir.resolve().parent != self.root_dir:
                    raise HistoryStoreError("历史原文件目录无效，未保存。")
                target = self.originals_dir / original_name
                # The filename never participates in a path. UUID files are created
                # inside the transaction so concurrent duplicate imports converge.
                with target.open("xb") as destination:
                    created_file = target
                    destination.write(file_bytes)
                    destination.flush()
                    os.fsync(destination.fileno())
                connection.execute("INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier, content_hash, mapping_hash, filename, original_name, now, now, report_json,
                     _sha(report_json.encode("utf-8")), fingerprint, mapping_json, empty_chat, _sha(empty_chat.encode("utf-8"))))
                return identifier
        except BaseException:
            if created_file is not None:
                created_file.unlink(missing_ok=True)
            raise

    def load_batch(self, batch_id: str) -> dict:
        with self._connection() as connection:
            connection.execute("BEGIN")
            row = self._row(connection, batch_id)
            report, mapping = self._report_and_mapping(row)
            original_bytes = self._read_original_row(row)
            return {**self._metadata(row, report, mapping), "report": report, "original_bytes": original_bytes}

    def read_original(self, batch_id: str) -> bytes:
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._read_original_row(self._row(connection, batch_id))

    def list_batches(self, limit=50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise HistoryStoreError("历史列表数量必须介于 1 至 1000。")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM batches ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
            return [self._metadata(row, *self._report_and_mapping(row)) for row in rows]

    def find_batch(self, content_hash: str, mapping: dict) -> dict | None:
        if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash) or type(mapping) is not dict:
            raise HistoryStoreError("文件校验值或字段配置无效。")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM batches WHERE content_hash=? AND mapping_hash=?",
                                     (content_hash, _sha(_pack(mapping).encode("utf-8")))).fetchone()
            return self._metadata(row, *self._report_and_mapping(row)) if row else None

    def delete_batch(self, batch_id: str) -> None:
        moved = None
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._row(connection, batch_id)
                path = self._original_path(row)
                self._purge_comparison_chat(connection, batch_id)
                # Stage removal until SQLite commit succeeds. A failed commit
                # restores the original; a missing original can still be deleted.
                if path.exists():
                    moved = (path, self.originals_dir / (uuid4().hex + ".deleted"))
                    try:
                        path.rename(moved[1])
                    except OSError as exc:
                        moved = None
                        raise HistoryStoreError("原文件无法删除，历史记录已保留。") from exc
                connection.execute("DELETE FROM batches WHERE id=?", (batch_id,))
        except BaseException:
            if moved is not None:
                moved[1].rename(moved[0])
            raise
        if moved is not None:
            try:
                moved[1].unlink()
            except OSError as exc:
                raise HistoryStoreError("历史记录已删除，但原文件清理未完成；请检查本机数据目录。") from exc

    def clear_chat(self, batch_id: str, comparison_id=None) -> None:
        """Filter one comparison conversation in a single write transaction."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, batch_id)
            if comparison_id is not None:
                self._row(connection, _validate_id(comparison_id))
            if _sha(row["chat_json"].encode("utf-8")) != row["chat_hash"]:
                raise HistoryCorruptionError("历史对话校验失败，未更改。")
            turns = _unpack(row["chat_json"])
            if type(turns) is not list or any(type(turn) is not dict for turn in turns):
                raise HistoryCorruptionError("历史对话格式无效，未更改。")
            payload = _pack([turn for turn in turns if turn.get("comparison_batch_id") != comparison_id])
            connection.execute("UPDATE batches SET chat_json=?,chat_hash=? WHERE id=?",
                               (payload, _sha(payload.encode("utf-8")), batch_id))

    def save_chat(self, batch_id: str, turns: list) -> None:
        if type(turns) is not list:
            raise HistoryStoreError("对话记录必须为列表。")
        payload = _pack(turns)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, batch_id)
            fingerprints = {None: row["report_fingerprint"]}
            for turn in turns:
                if type(turn) is not dict:
                    raise HistoryStoreError("对话条目格式无效。")
                previous_id = turn.get("comparison_batch_id")
                if previous_id is not None:
                    _validate_id(previous_id)
                if previous_id not in fingerprints:
                    fingerprints[previous_id] = self._chat_fingerprint(connection, row, previous_id)
                result = turn.get("result", turn)
                if type(result) is dict and result.get("report_fingerprint") not in (None, fingerprints[previous_id]):
                    raise HistoryStoreError("对话属于另一份分析报告，未保存。")
            connection.execute("UPDATE batches SET chat_json=?,chat_hash=? WHERE id=?",
                               (payload, _sha(payload.encode("utf-8")), batch_id))

    def load_chat(self, batch_id: str) -> list:
        with self._connection() as connection:
            connection.execute("BEGIN")
            row = self._row(connection, batch_id)
            self._report_and_mapping(row)
            if _sha(row["chat_json"].encode("utf-8")) != row["chat_hash"]:
                raise HistoryCorruptionError("历史对话校验失败，未加载。")
            turns = _unpack(row["chat_json"])
            if type(turns) is not list or any(type(turn) is not dict for turn in turns):
                raise HistoryCorruptionError("历史对话格式无效。")
            valid, fingerprints = [], {None: row["report_fingerprint"]}
            for turn in turns:
                previous_id = turn.get("comparison_batch_id")
                try:
                    if previous_id not in fingerprints:
                        fingerprints[previous_id] = self._chat_fingerprint(connection, row, previous_id)
                    result = turn.get("result", turn)
                    if type(result) is dict and result.get("report_fingerprint") not in (None, fingerprints[previous_id]):
                        continue
                except (HistoryStoreError, TypeError):
                    # The explicitly compared batch may have been reanalysed or
                    # deleted since this chat. Do not restore those stale facts.
                    continue
                valid.append(turn)
            return valid

    def _chat_fingerprint(self, connection, row, previous_id):
        """Rebuild only the comparison explicitly named in this saved turn."""
        from core.analysis_agent import report_fingerprint
        from core.history_workflow import attach_historical_context

        _validate_id(previous_id)
        if previous_id == row["id"]:
            raise HistoryStoreError("不能将当前报告与自身作为历史对比。")
        previous_row = self._row(connection, previous_id)
        previous_report, previous_mapping = self._report_and_mapping(previous_row)
        self._read_original_row(previous_row)
        base_report, _ = self._report_and_mapping(row)
        previous = {**self._metadata(previous_row, previous_report, previous_mapping), "report": previous_report}
        return report_fingerprint(attach_historical_context(base_report, previous))

    @staticmethod
    def _purge_comparison_chat(connection, previous_id):
        """Drop dependent turns in the same transaction as delete/reanalysis.

        This is logical deletion. It does not erase previously downloaded ZIPs
        or claim forensic removal from the deployment owner's physical disk.
        """
        rows = connection.execute("SELECT id,chat_json,chat_hash FROM batches WHERE id<>? AND instr(chat_json,?)>0",
                                  (previous_id, previous_id)).fetchall()
        for row in rows:
            if _sha(row["chat_json"].encode("utf-8")) != row["chat_hash"]:
                raise HistoryCorruptionError("有关联的历史对话校验失败，未更改历史记录。")
            turns = _unpack(row["chat_json"])
            if type(turns) is not list or any(type(turn) is not dict for turn in turns):
                raise HistoryCorruptionError("有关联的历史对话格式无效，未更改历史记录。")
            kept = [turn for turn in turns if turn.get("comparison_batch_id") != previous_id]
            if len(kept) != len(turns):
                payload = _pack(kept)
                connection.execute("UPDATE batches SET chat_json=?,chat_hash=? WHERE id=?",
                                   (payload, _sha(payload.encode("utf-8")), row["id"]))

    @staticmethod
    def _columns(columns):
        if type(columns) is not list or not columns or any(not isinstance(column, str) or not column for column in columns):
            raise HistoryStoreError("字段名称列表无效。")
        return _pack(columns)

    def save_mapping(self, columns: list[str], mapping: dict) -> None:
        if type(mapping) is not dict:
            raise HistoryStoreError("字段配置必须为字典。")
        columns_json, payload = self._columns(columns), _pack(mapping)
        with self._connection() as connection:
            connection.execute("""INSERT INTO column_mappings VALUES(?,?,?,?,?) ON CONFLICT(signature) DO UPDATE SET
                columns_json=excluded.columns_json,mapping_json=excluded.mapping_json,
                mapping_hash=excluded.mapping_hash,updated_at=excluded.updated_at""",
                (_sha(columns_json.encode("utf-8")), columns_json, payload, _sha(payload.encode("utf-8")),
                 datetime.now(timezone.utc).isoformat()))

    def get_mapping(self, columns: list[str]) -> dict | None:
        payload = self._columns(columns)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM column_mappings WHERE signature=?", (_sha(payload.encode("utf-8")),)).fetchone()
            if row is None:
                return None
            if row["columns_json"] != payload or _sha(row["mapping_json"].encode("utf-8")) != row["mapping_hash"]:
                raise HistoryCorruptionError("已保存的字段配置校验失败。")
            result = _unpack(row["mapping_json"])
            if type(result) is not dict:
                raise HistoryCorruptionError("已保存的字段配置格式无效。")
            return result

    def export_backup(self) -> bytes:
        """Export a consistent SQLite snapshot and only its verified originals."""
        archive = io.BytesIO()
        with tempfile.TemporaryDirectory(prefix="customerinsight-backup-") as directory:
            snapshot = Path(directory) / "customerinsight.sqlite3"
            with self._connection() as connection:
                # A write reservation serializes save/delete while originals and
                # the independent backup connection are copied at one revision.
                connection.execute("BEGIN IMMEDIATE")
                source = sqlite3.connect(self.db_path, timeout=15)
                destination = sqlite3.connect(snapshot)
                try:
                    source.backup(destination)
                    # Compact the exported snapshot, so unused SQLite cells and
                    # freelist pages do not carry logically deleted chat text.
                    destination.execute("VACUUM")
                finally:
                    destination.close()
                    source.close()
                rows = connection.execute("SELECT * FROM batches ORDER BY id").fetchall()
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                    zipped.write(snapshot, "customerinsight.sqlite3")
                    manifest = []
                    for row in rows:
                        self._report_and_mapping(row)
                        original = self._read_original_row(row)
                        zipped.writestr("originals/" + row["original_name"], original)
                        manifest.append({"id": row["id"], "filename": row["filename"], "sha256": row["content_hash"]})
                    zipped.writestr("manifest.json", _json({"schema": "customerinsight-local-backup", "version": SCHEMA_VERSION,
                        "created_at": datetime.now(timezone.utc).isoformat(), "batches": manifest}))
        return archive.getvalue()
