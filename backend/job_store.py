"""Durable, single-workspace analysis jobs and transactional dispatch outbox.

Redis/Celery deliver notifications; SQLite owns progress. An expired lease that
never reserved a transport is safe to run again. A reserved transport whose
outcome is unknown is quarantined, because local deduplication cannot guarantee
that an upstream model did not receive or charge for that request.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4


_ID = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL = frozenset({"completed", "failed", "cancelled", "needs_review"})
_PAUSE_CODES = frozenset({"daily_budget_exhausted", "configuration_changed", "execution_paused"})
_MAX_ORIGINAL_BYTES = 5 * 1024 * 1024
_MAX_JSON_BYTES = 50 * 1024 * 1024
_JOB_COLUMNS = ("id,scope,dedupe_key,source_key,report_superseded_by,status,filename,original_hash,request_json,config_json,"
                "max_requests,actual_requests,total_rows,total_groups,cancel_requested,"
                "batch_id,error_code,created_at,updated_at")


class JobStoreError(ValueError):
    """Safe storage or transition error; never contains original text or keys."""


class JobNotFoundError(JobStoreError):
    pass


class JobConflictError(JobStoreError):
    pass


class JobBudgetError(JobStoreError):
    pass


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise JobStoreError("任务编号格式无效。")
    return value


def _json(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise JobStoreError("任务数据不是有效的 JSON。") from None
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise JobStoreError("任务数据超过存储上限。")
    return encoded


def _positive(value, maximum=10000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise JobStoreError("任务限制必须为有效的正整数。")
    return value


def _lease_duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 86400:
        raise JobStoreError("任务租约期限无效。")
    return float(value)


class JobStore:
    """SQLite repository, usable by independent API and worker processes.

    Every writer obtains BEGIN IMMEDIATE. A lease has a fresh fencing token;
    expired or superseded workers cannot commit results. Job originals are
    stored in the job database rather than the expiring upload directory, so
    the snapshot and the outbox commit atomically with job creation.
    """

    def __init__(self, data_dir, *, clock=None):
        self.root_dir = Path(data_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.root_dir / "analysis_jobs.sqlite3"
        if self.path.is_symlink():
            raise JobStoreError("任务存储目录不可用。")
        self.clock = clock or time.time
        self._report_context = threading.local()
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL, source_key TEXT, report_superseded_by TEXT,
                    status TEXT NOT NULL,
                    filename TEXT NOT NULL, original BLOB NOT NULL,
                    original_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    config_json TEXT NOT NULL, max_requests INTEGER NOT NULL,
                    actual_requests INTEGER NOT NULL DEFAULT 0,
                    total_rows INTEGER NOT NULL, total_groups INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    batch_id TEXT, error_code TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_dedupe
                    ON analysis_jobs(scope, dedupe_key, created_at DESC);
                CREATE INDEX IF NOT EXISTS jobs_recent
                    ON analysis_jobs(created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS job_idempotency (
                    scope TEXT NOT NULL, key_hash TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL, job_id TEXT NOT NULL,
                    PRIMARY KEY(scope,key_hash),
                    FOREIGN KEY(job_id) REFERENCES analysis_jobs(id)
                );
                CREATE TABLE IF NOT EXISTS analysis_groups (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    group_index INTEGER NOT NULL, input_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL, status TEXT NOT NULL,
                    lease_token TEXT, lease_until REAL, worker_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT, usage_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(job_id,group_index),
                    FOREIGN KEY(job_id) REFERENCES analysis_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS groups_ready
                    ON analysis_groups(job_id,status,group_index);
                CREATE TABLE IF NOT EXISTS analysis_results (
                    job_id TEXT NOT NULL, feedback_id TEXT NOT NULL,
                    source_row INTEGER NOT NULL, group_id TEXT,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,feedback_id),
                    UNIQUE(job_id,source_row),
                    FOREIGN KEY(job_id) REFERENCES analysis_jobs(id),
                    FOREIGN KEY(group_id) REFERENCES analysis_groups(id)
                );
                CREATE TABLE IF NOT EXISTS analysis_attempts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, lease_token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT, created_at REAL NOT NULL, completed_at REAL,
                    FOREIGN KEY(job_id) REFERENCES analysis_jobs(id),
                    FOREIGN KEY(group_id) REFERENCES analysis_groups(id)
                );
                CREATE TABLE IF NOT EXISTS job_outbox (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, created_at REAL NOT NULL, published_at REAL,
                    FOREIGN KEY(job_id) REFERENCES analysis_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS outbox_pending ON job_outbox(status,created_at);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_jobs)")}
            for name in ("source_key", "report_superseded_by"):
                if name not in columns:
                    try:
                        conn.execute(f"ALTER TABLE analysis_jobs ADD COLUMN {name} TEXT")
                    except sqlite3.OperationalError:
                        # Another process may have finished the same additive
                        # migration while this constructor waited for its lock.
                        current = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_jobs)")}
                        if name not in current:
                            raise
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_source ON analysis_jobs(scope,source_key)")

    @contextmanager
    def _connection(self, *, write=False):
        guarded = getattr(self._report_context, "connection", None)
        if guarded is not None:
            # The guard owns commit/rollback, including set_report and any
            # worker metadata reads inside its bounded local export section.
            yield guarded
            return
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _job(self, conn, job_id):
        row = conn.execute(f"SELECT {_JOB_COLUMNS} FROM analysis_jobs WHERE id=?", (_identifier(job_id),)).fetchone()
        if row is None:
            raise JobNotFoundError("分析任务不存在。")
        return row

    def _group(self, conn, job_id, group_id, lease_token, *, require_active=True):
        self._job(conn, job_id)
        row = conn.execute("SELECT * FROM analysis_groups WHERE id=? AND job_id=?",
                           (_identifier(group_id), job_id)).fetchone()
        if (row is None or row["lease_token"] != lease_token
                or (require_active and (row["status"] not in {"leased", "sent"}
                                        or row["lease_until"] <= self.clock()))):
            raise JobConflictError("分组租约已失效，不能提交或发送请求。")
        return row

    @staticmethod
    def _enqueue(conn, job_id, now):
        # Duplicate publication is safe: the group lease/result transaction
        # remains authoritative. A publisher acknowledges only after publish.
        existing = conn.execute("SELECT id FROM job_outbox WHERE job_id=? AND status='pending'",
                                (job_id,)).fetchone()
        if existing:
            return existing["id"]
        outbox_id = uuid4().hex
        conn.execute("INSERT INTO job_outbox(id,job_id,status,created_at) VALUES(?,?,'pending',?)",
                     (outbox_id, job_id, now))
        return outbox_id

    @staticmethod
    def _result_values(job_id, result, group_id=None):
        if not isinstance(result, dict):
            raise JobStoreError("逐行结果无效。")
        feedback_id, source_row = result.get("feedback_id"), result.get("source_row")
        if (not isinstance(feedback_id, str) or not 1 <= len(feedback_id) <= 255
                or type(source_row) is not int or source_row < 2
                or not isinstance(result.get("original_text"), str)):
            raise JobStoreError("逐行结果缺少稳定的原行编号。")
        return job_id, feedback_id, source_row, group_id, _json(result)

    def create_job(self, *, original, filename, request, config, groups,
                   initial_results=None, max_requests, idempotency_key=None,
                   force=False, scope="workspace"):
        """Snapshot input and enqueue atomically; return ``(job, created)``.

        request/config must be public execution settings, never credentials.
        The caller takes a snapshot of model and prompt identity in config.
        """
        _positive(max_requests)
        if not isinstance(original, bytes) or not 0 < len(original) <= _MAX_ORIGINAL_BYTES:
            raise JobStoreError("任务原文件为空或超过 5 MB。")
        if not isinstance(filename, str) or not 0 < len(filename) <= 255 or "\x00" in filename:
            raise JobStoreError("任务文件名无效。")
        if not isinstance(scope, str) or not 0 < len(scope) <= 255:
            raise JobStoreError("任务工作区无效。")
        if not isinstance(request, dict) or not isinstance(config, dict):
            raise JobStoreError("任务执行配置无效。")
        if not isinstance(groups, list) or len(groups) > 10000:
            raise JobStoreError("任务分组无效。")
        initial_results = [] if initial_results is None else initial_results
        if not isinstance(initial_results, list):
            raise JobStoreError("任务初始结果无效。")
        request_json, config_json = _json(request), _json(config)
        # Temporary upload IDs, consent and transport budgets do not change
        # the meaning of the requested analysis. Mapping does.
        semantic_request = {key: value for key, value in request.items()
                            if key not in {"upload_id", "allow_model_call", "max_requests", "force"}}
        original_hash = hashlib.sha256(original).hexdigest()
        dedupe_key = hashlib.sha256(_json({"original": original_hash,
                                          "request": semantic_request,
                                          "config": config}).encode("utf-8")).hexdigest()
        source_key = hashlib.sha256(_json({"scope": scope, "original": original_hash,
                                          "request": semantic_request}).encode("utf-8")).hexdigest()
        key_hash = None
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 255:
                raise JobStoreError("幂等键必须为 1 到 255 个字符。")
            key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        serialized_groups, row_ids = [], {}
        for group in groups:
            if not isinstance(group, list) or not group:
                raise JobStoreError("分析分组不能为空。")
            for item in group:
                values = self._result_values("", item)
                if values[1] in row_ids:
                    raise JobStoreError("同一评价不能出现在多个请求分组。")
                row_ids[values[1]] = values[2]
            encoded = _json(group)
            serialized_groups.append((encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()))
        initial_values = [self._result_values("", item) for item in initial_results]
        if len({item[1] for item in initial_values}) != len(initial_values):
            raise JobStoreError("任务初始结果编号重复。")
        for item in initial_values:
            if item[1] in row_ids and row_ids[item[1]] != item[2]:
                raise JobStoreError("任务原行编号不一致。")
            row_ids[item[1]] = item[2]
        if len(set(row_ids.values())) != len(row_ids):
            raise JobStoreError("任务原行编号重复。")
        now, job_id, created = self.clock(), uuid4().hex, False
        with self._connection(write=True) as conn:
            if key_hash:
                previous = conn.execute("SELECT * FROM job_idempotency WHERE scope=? AND key_hash=?",
                                        (scope, key_hash)).fetchone()
                if previous:
                    if previous["dedupe_key"] != dedupe_key:
                        raise JobConflictError("此幂等键已经用于不同的分析输入。")
                    return self._projection(conn, previous["job_id"]), False
            previous = None if force else conn.execute(
                "SELECT id FROM analysis_jobs WHERE scope=? AND dedupe_key=? ORDER BY rowid DESC LIMIT 1",
                (scope, dedupe_key)).fetchone()
            if previous:
                job_id = previous["id"]
            else:
                created = True
                conn.execute("""INSERT INTO analysis_jobs
                    (id,scope,dedupe_key,source_key,status,filename,original,original_hash,request_json,
                     config_json,max_requests,total_rows,total_groups,created_at,updated_at)
                    VALUES(?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, scope, dedupe_key, source_key, filename.replace("\\", "/").rsplit("/", 1)[-1],
                     original, original_hash, request_json, config_json, max_requests,
                     len(row_ids), len(groups), now, now))
                for index, (encoded, digest) in enumerate(serialized_groups):
                    conn.execute("""INSERT INTO analysis_groups
                        (id,job_id,group_index,input_json,input_hash,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,'pending',?,?)""",
                        (uuid4().hex, job_id, index, encoded, digest, now, now))
                conn.executemany("INSERT INTO analysis_results VALUES(?,?,?,?,?)",
                                 [(job_id,) + item[1:] for item in initial_values])
                self._enqueue(conn, job_id, now)
            if key_hash:
                conn.execute("INSERT INTO job_idempotency VALUES(?,?,?,?)",
                             (scope, key_hash, dedupe_key, job_id))
            return self._projection(conn, job_id), created

    def _projection(self, conn, job_id):
        record = dict(self._job(conn, job_id))
        newer_generation = self._newer_job(conn, record)
        record["request"] = json.loads(record.pop("request_json"))
        record["config"] = json.loads(record.pop("config_json"))
        record["cancel_requested"] = bool(record["cancel_requested"])
        groups = []
        for group in conn.execute("SELECT * FROM analysis_groups WHERE job_id=? ORDER BY group_index", (job_id,)):
            item = dict(group)
            item["rows"] = json.loads(item.pop("input_json"))
            item["usage"] = json.loads(item.pop("usage_json"))
            groups.append(item)
        record["groups"] = groups
        record["completed_groups"] = sum(item["status"] == "succeeded" for item in groups)
        record["failed_groups"] = sum(item["status"] == "failed" for item in groups)
        record["uncertain_groups"] = sum(item["status"] == "uncertain" for item in groups)
        results = [json.loads(row[0]) for row in conn.execute(
            "SELECT result_json FROM analysis_results WHERE job_id=? ORDER BY source_row", (job_id,))]
        statuses = [row.get("processing_status") for row in results]
        record["completed_rows"] = sum(status != "pending" for status in statuses)
        by_id = {row["feedback_id"]: row for row in results}
        first_for_text = {}
        for row in results:
            text = row["original_text"].strip()
            if text:
                first_for_text.setdefault(text, row["feedback_id"])
        reused_rows = 0
        for row in results:
            text = row["original_text"].strip()
            reference = by_id.get(row.get("duplicate_of"))
            if (row.get("processing_status") == "skipped_duplicate" and reference
                    and reference.get("processing_status") == "success"
                    and reference["feedback_id"] == first_for_text.get(text)
                    and reference["feedback_id"] != row["feedback_id"]
                    and reference["original_text"].strip() == text):
                reused_rows += 1
        record["model_success_rows"] = statuses.count("success")
        record["success_rows"] = record["model_success_rows"] + reused_rows
        record["request_count"] = record["actual_requests"]
        record["can_resume"] = bool(
            any(item["status"] == "pending" or (item["status"] == "cancelled" and not item["attempt_count"])
                for item in groups)
            and not any(item["status"] in {"uncertain", "sent", "leased"} for item in groups)
            and not record["report_superseded_by"]
            and not newer_generation
        )
        record["usage"] = {}
        for group in groups:
            for key, value in group["usage"].items():
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    record["usage"][key] = record["usage"].get(key, 0) + value
        return record

    def get_job(self, job_id):
        with self._connection() as conn:
            return self._projection(conn, job_id)

    def list_jobs(self, *, limit=20, offset=0, scope="workspace"):
        _positive(limit, maximum=100)
        if type(offset) is not int or offset < 0:
            raise JobStoreError("分页位置无效。")
        with self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM analysis_jobs WHERE scope=?", (scope,)).fetchone()[0]
            ids = conn.execute("SELECT id FROM analysis_jobs WHERE scope=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                               (scope, limit, offset)).fetchall()
            return {"items": [self._projection(conn, row["id"]) for row in ids], "total": total,
                    "limit": limit, "offset": offset}

    def original(self, job_id):
        with self._connection() as conn:
            job = self._job(conn, job_id)
            data = bytes(conn.execute("SELECT original FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()[0])
            if hashlib.sha256(data).hexdigest() != job["original_hash"]:
                raise JobStoreError("任务原件完整性校验失败。")
            return data

    def get_results(self, job_id):
        with self._connection() as conn:
            self._job(conn, job_id)
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT result_json FROM analysis_results WHERE job_id=? ORDER BY source_row", (job_id,))]

    def attempts(self, job_id):
        with self._connection() as conn:
            self._job(conn, job_id)
            result = []
            for row in conn.execute("SELECT * FROM analysis_attempts WHERE job_id=? ORDER BY created_at,id", (job_id,)):
                item = dict(row)
                item["usage"] = json.loads(item.pop("usage_json"))
                result.append(item)
            return result

    def _refresh(self, conn, job_id, now):
        job = self._job(conn, job_id)
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM analysis_groups WHERE job_id=? GROUP BY status", (job_id,))}
        has_success = any(json.loads(row[0]).get("processing_status") == "success"
                          for row in conn.execute("SELECT result_json FROM analysis_results WHERE job_id=?", (job_id,)))
        error_code = job["error_code"]
        if counts.get("uncertain"):
            status, error_code = "needs_review", "transport_outcome_unknown"
        elif job["cancel_requested"]:
            status = "running" if counts.get("sent") else "cancelled"
        elif counts.get("leased") or counts.get("sent"):
            status = "running"
        elif counts.get("pending") and error_code in _PAUSE_CODES:
            status = "partial" if has_success else "failed"
        elif counts.get("pending") and job["actual_requests"] >= job["max_requests"]:
            status = "partial" if has_success else "failed"
            error_code = "request_budget_exhausted"
        elif counts.get("pending"):
            status = "running" if has_success else "queued"
        elif counts.get("failed") or counts.get("cancelled"):
            status = "partial" if has_success else "failed"
        else:
            status, error_code = "completed", None
        conn.execute("UPDATE analysis_jobs SET status=?,error_code=?,updated_at=? WHERE id=?",
                     (status, error_code, now, job_id))

    def _expire(self, conn, job_id, now):
        condition = "AND job_id=?" if job_id else ""
        args = (now, job_id) if job_id else (now,)
        expired = conn.execute(f"SELECT * FROM analysis_groups WHERE status IN ('leased','sent') AND lease_until<=? {condition}", args).fetchall()
        changed = set()
        for group in expired:
            job = self._job(conn, group["job_id"])
            if group["status"] == "sent":
                status, error = "uncertain", "请求发送后的工作进程中断，结果未知；未自动重试。"
                conn.execute("UPDATE analysis_attempts SET status='uncertain',error=?,completed_at=? WHERE lease_token=? AND status='sent'",
                             (error, now, group["lease_token"]))
            else:
                status, error = ("cancelled" if job["cancel_requested"] else "pending"), None
            conn.execute("UPDATE analysis_groups SET status=?,error=?,lease_token=NULL,lease_until=NULL,worker_id=NULL,updated_at=? WHERE id=?",
                         (status, error, now, group["id"]))
            changed.add(group["job_id"])
        for changed_id in changed:
            self._refresh(conn, changed_id, now)
            refreshed = self._job(conn, changed_id)
            if (refreshed["status"] not in _TERMINAL and not refreshed["cancel_requested"]
                    and refreshed["error_code"] not in _PAUSE_CODES):
                self._enqueue(conn, changed_id, now)
        return len(expired)

    def expire_leases(self, job_id=None):
        with self._connection(write=True) as conn:
            if job_id is not None:
                self._job(conn, job_id)
            return self._expire(conn, job_id, self.clock())

    def claim_group(self, job_id, worker_id, lease_seconds=300):
        duration = _lease_duration(lease_seconds)
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 255:
            raise JobStoreError("工作进程标识无效。")
        now = self.clock()
        with self._connection(write=True) as conn:
            self._job(conn, job_id)
            self._expire(conn, job_id, now)
            job = self._job(conn, job_id)
            if (job["cancel_requested"] or job["status"] in _TERMINAL
                    or job["error_code"] in _PAUSE_CODES):
                return None
            if job["actual_requests"] >= job["max_requests"]:
                self._refresh(conn, job_id, now)
                return None
            row = conn.execute("SELECT * FROM analysis_groups WHERE job_id=? AND status='pending' ORDER BY group_index LIMIT 1", (job_id,)).fetchone()
            if row is None:
                self._refresh(conn, job_id, now)
                return None
            token = uuid4().hex
            conn.execute("UPDATE analysis_groups SET status='leased',lease_token=?,lease_until=?,worker_id=?,updated_at=? WHERE id=?",
                         (token, now + duration, worker_id, now, row["id"]))
            conn.execute("UPDATE analysis_jobs SET status='running',updated_at=? WHERE id=?", (now, job_id))
            result = dict(row)
            result.update(status="leased", lease_token=token, lease_until=now + duration, worker_id=worker_id)
            result["rows"] = json.loads(result.pop("input_json"))
            result["usage"] = json.loads(result.pop("usage_json"))
            return result

    def renew_lease(self, job_id, group_id, lease_token, lease_seconds=300):
        duration, now = _lease_duration(lease_seconds), self.clock()
        with self._connection(write=True) as conn:
            self._group(conn, job_id, group_id, lease_token)
            conn.execute("UPDATE analysis_groups SET lease_until=?,updated_at=? WHERE id=?", (now + duration, now, group_id))
        return now + duration

    def checkpoint_usage(self, job_id, group_id, lease_token, usage):
        """Save reported billing usage before attempting to parse model JSON."""
        if not isinstance(usage, dict):
            raise JobStoreError("模型用量记录无效。")
        encoded, now = _json(usage), self.clock()
        with self._connection(write=True) as conn:
            group = self._group(conn, job_id, group_id, lease_token)
            if group["status"] != "sent":
                raise JobConflictError("尚未发送的分组不能记录模型用量。")
            conn.execute("UPDATE analysis_groups SET usage_json=?,updated_at=? WHERE id=?", (encoded, now, group_id))
            conn.execute("UPDATE analysis_attempts SET usage_json=? WHERE lease_token=?", (encoded, lease_token))

    def pause_job(self, job_id, error_code="execution_paused"):
        """Keep unsent inputs, but require explicit resume after a hard stop."""
        if error_code not in _PAUSE_CODES:
            raise JobStoreError("任务暂停原因无效。")
        now = self.clock()
        with self._connection(write=True) as conn:
            job = self._job(conn, job_id)
            if job["status"] == "completed":
                return self._projection(conn, job_id)
            conn.execute("UPDATE analysis_jobs SET error_code=?,updated_at=? WHERE id=?", (error_code, now, job_id))
            self._refresh(conn, job_id, now)
            return self._projection(conn, job_id)

    def mark_sent(self, job_id, group_id, lease_token):
        """Reserve exactly one transport for this lease, before sending bytes."""
        now = self.clock()
        with self._connection(write=True) as conn:
            group = self._group(conn, job_id, group_id, lease_token)
            previous = conn.execute("SELECT id FROM analysis_attempts WHERE lease_token=?", (lease_token,)).fetchone()
            if previous:
                # Calling this twice is an application error: returning an
                # existing reservation could be mistaken for send permission.
                raise JobConflictError("该分组已经登记一次请求，不能重复发送。")
            job = self._job(conn, job_id)
            if job["cancel_requested"]:
                raise JobConflictError("任务已请求取消，不能发起新请求。")
            if job["actual_requests"] >= job["max_requests"]:
                raise JobBudgetError("本任务模型请求预算已经用完。")
            if group["status"] != "leased":
                raise JobConflictError("该分组不处于可发送状态。")
            attempt_id = uuid4().hex
            conn.execute("INSERT INTO analysis_attempts(id,job_id,group_id,lease_token,status,created_at) VALUES(?,?,?,?,'sent',?)",
                         (attempt_id, job_id, group_id, lease_token, now))
            conn.execute("UPDATE analysis_groups SET status='sent',attempt_count=attempt_count+1,updated_at=? WHERE id=?", (now, group_id))
            conn.execute("UPDATE analysis_jobs SET actual_requests=actual_requests+1,updated_at=? WHERE id=?", (now, job_id))
            return attempt_id

    def checkpoint_group(self, job_id, group_id, lease_token, *, results, usage=None, error=None):
        """Commit one complete validated response, or a known terminal failure."""
        if not isinstance(results, list) or (error is not None and not isinstance(error, str)):
            raise JobStoreError("分组检查点无效。")
        if usage is not None and not isinstance(usage, dict):
            raise JobStoreError("模型用量记录无效。")
        now = self.clock()
        with self._connection(write=True) as conn:
            group = self._group(conn, job_id, group_id, lease_token)
            usage_json = group["usage_json"] if usage is None else _json(usage)
            if group["status"] != "sent":
                raise JobConflictError("尚未登记发送的分组不能提交模型结果。")
            expected = {row["feedback_id"]: row["source_row"] for row in json.loads(group["input_json"])}
            originals = {row["feedback_id"]: row for row in json.loads(group["input_json"])}
            values = [self._result_values(job_id, result, group_id) for result in results]
            actual = {item[1]: item[2] for item in values}
            if (len(actual) != len(values) or any(expected.get(key) != value for key, value in actual.items())
                    or (error is None and actual != expected)
                    or any(result["original_text"] != originals.get(result["feedback_id"], {}).get("original_text")
                           for result in results)):
                raise JobStoreError("分组结果必须完整对应本组评价，不能写入其他原行。")
            # Values are Pydantic-validated by the worker. The repository still
            # enforces identity/row ownership and an indivisible group commit.
            for item in values:
                conn.execute("""INSERT INTO analysis_results VALUES(?,?,?,?,?)
                    ON CONFLICT(job_id,feedback_id) DO UPDATE SET
                    source_row=excluded.source_row,group_id=excluded.group_id,result_json=excluded.result_json""", item)
            status = "failed" if error else "succeeded"
            safe_error = "分组分析未完成，请查看受控的任务诊断信息。" if error else None
            conn.execute("UPDATE analysis_groups SET status=?,usage_json=?,error=?,lease_until=NULL,updated_at=? WHERE id=?",
                         (status, usage_json, safe_error, now, group_id))
            conn.execute("UPDATE analysis_attempts SET status=?,usage_json=?,error=?,completed_at=? WHERE lease_token=?",
                         (status, usage_json, safe_error, now, lease_token))
            self._refresh(conn, job_id, now)
            return self._projection(conn, job_id)

    complete_group = checkpoint_group

    def record_failure(self, job_id, group_id, lease_token, error=None, *, uncertain=True):
        """Release a safe unsent lease, or quarantine an unknown sent attempt."""
        now = self.clock()
        with self._connection(write=True) as conn:
            group = self._group(conn, job_id, group_id, lease_token)
            job = self._job(conn, job_id)
            if group["status"] == "leased":
                status = "cancelled" if job["cancel_requested"] else "pending"
                safe_error = None
            else:
                status = "uncertain" if uncertain else "failed"
                safe_error = ("模型请求结果未知，需要人工核对；未自动重试。" if uncertain
                              else "分组模型请求失败，未自动重试。")
                conn.execute("UPDATE analysis_attempts SET status=?,error=?,completed_at=? WHERE lease_token=?",
                             (status, safe_error, now, lease_token))
            conn.execute("UPDATE analysis_groups SET status=?,error=?,lease_token=NULL,lease_until=NULL,worker_id=NULL,updated_at=? WHERE id=?",
                         (status, safe_error, now, group_id))
            self._refresh(conn, job_id, now)
            return self._projection(conn, job_id)

    def cancel_job(self, job_id):
        now = self.clock()
        with self._connection(write=True) as conn:
            job = self._job(conn, job_id)
            if job["status"] == "completed":
                return self._projection(conn, job_id)
            conn.execute("UPDATE analysis_jobs SET cancel_requested=1,updated_at=? WHERE id=?", (now, job_id))
            # Sent requests may finish and checkpoint. Unsent leased work is
            # fenced immediately, so even a stale worker cannot send it.
            conn.execute("UPDATE analysis_groups SET status='cancelled',lease_token=NULL,lease_until=NULL,worker_id=NULL,updated_at=? WHERE job_id=? AND status IN ('pending','leased')",
                         (now, job_id))
            self._refresh(conn, job_id, now)
            return self._projection(conn, job_id)

    def resume_job(self, job_id, *, max_requests=None):
        if max_requests is not None:
            _positive(max_requests)
        now = self.clock()
        with self._connection(write=True) as conn:
            self._job(conn, job_id)
            self._expire(conn, job_id, now)
            job = self._job(conn, job_id)
            if job["status"] == "completed":
                return self._projection(conn, job_id)
            if job["report_superseded_by"] or self._newer_job(conn, job):
                raise JobConflictError("该来源已经创建更新的分析任务，请使用最新任务。")
            if conn.execute("SELECT 1 FROM analysis_groups WHERE job_id=? AND status='uncertain'", (job_id,)).fetchone():
                raise JobConflictError("存在结果未知的模型请求，需要先人工核对，不能自动恢复。")
            if max_requests is not None:
                if max_requests < job["actual_requests"]:
                    raise JobStoreError("新预算不能小于已经发生的模型请求数。")
                conn.execute("UPDATE analysis_jobs SET max_requests=? WHERE id=?", (max_requests, job_id))
            # Only cancelled groups with no transport reservation are safe.
            conn.execute("UPDATE analysis_groups SET status='pending',error=NULL,updated_at=? WHERE job_id=? AND status='cancelled' AND attempt_count=0",
                         (now, job_id))
            conn.execute("UPDATE analysis_jobs SET cancel_requested=0,error_code=NULL,updated_at=? WHERE id=?", (now, job_id))
            self._refresh(conn, job_id, now)
            refreshed = self._job(conn, job_id)
            if refreshed["status"] not in _TERMINAL and refreshed["actual_requests"] < refreshed["max_requests"]:
                # A completed/partial report may already be archived. Keep its
                # old history record, but require refreshed archival after the
                # explicitly resumed job makes new progress.
                conn.execute("UPDATE analysis_jobs SET batch_id=NULL WHERE id=?", (job_id,))
                self._enqueue(conn, job_id, now)
            return self._projection(conn, job_id)

    @staticmethod
    def _newer_job(conn, job):
        # rowid is insertion order even when clocks match or move backwards.
        if job["source_key"]:
            return conn.execute("""SELECT id FROM analysis_jobs WHERE scope=? AND source_key=?
                AND rowid>(SELECT rowid FROM analysis_jobs WHERE id=?) ORDER BY rowid DESC LIMIT 1""",
                (job["scope"], job["source_key"], job["id"])).fetchone()
        # Compatibility for a repository created before source_key existed.
        return conn.execute("""SELECT id FROM analysis_jobs WHERE scope=? AND original_hash=?
            AND request_json=? AND rowid>(SELECT rowid FROM analysis_jobs WHERE id=?)
            ORDER BY rowid DESC LIMIT 1""",
            (job["scope"], job["original_hash"], job["request_json"], job["id"])).fetchone()

    @contextmanager
    def report_guard(self, job_id):
        """Fence a bounded local history export against newer generations.

        The worker builds its report first, then holds this SQLite writer lock
        only across HistoryStore.save_batch + set_report. No model calls or
        other network operations may occur here. A forced creation/resume or
        second finalizer must wait until the export finishes. Process death
        releases the SQLite lock, without relying on a wall-clock lease that
        could expire while a stale exporter was still writing another DB.
        """
        with self._connection(write=True) as conn:
            job = self._job(conn, job_id)
            newer = self._newer_job(conn, job)
            if newer:
                conn.execute("UPDATE analysis_jobs SET report_superseded_by=?,updated_at=? WHERE id=?",
                             (newer["id"], self.clock(), job_id))
            allowed = (not newer and not job["report_superseded_by"] and not job["batch_id"]
                       and job["status"] in {"completed", "partial", "failed", "cancelled", "needs_review"})
            self._report_context.connection = conn
            try:
                yield allowed
            finally:
                del self._report_context.connection

    def set_report(self, job_id, batch_id):
        _identifier(batch_id)
        with self._connection(write=True) as conn:
            job = self._job(conn, job_id)
            if job["batch_id"] and job["batch_id"] != batch_id:
                raise JobConflictError("任务已经关联另一份报告。")
            conn.execute("UPDATE analysis_jobs SET batch_id=?,updated_at=? WHERE id=?", (batch_id, self.clock(), job_id))
            return self._projection(conn, job_id)

    @staticmethod
    def _has_related_jobs(conn, batch_id, original_hash=None):
        _identifier(batch_id)
        if original_hash is not None and (not isinstance(original_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", original_hash)):
            raise JobStoreError("原文件校验值无效。")
        # A newer generation may not have a batch_id yet. Conservatively keep
        # any report whose original is also retained by a durable job, even if
        # that job selected another sheet/mapping from the same workbook.
        if original_hash is None:
            row = conn.execute("SELECT 1 FROM analysis_jobs WHERE batch_id=? LIMIT 1", (batch_id,)).fetchone()
        else:
            row = conn.execute("SELECT 1 FROM analysis_jobs WHERE batch_id=? OR original_hash=? LIMIT 1",
                               (batch_id, original_hash)).fetchone()
        return row is not None

    def has_related_jobs(self, batch_id, original_hash=None):
        """Whether durable tasks still retain this report or its original."""
        with self._connection() as conn:
            return self._has_related_jobs(conn, batch_id, original_hash)

    @contextmanager
    def history_delete_guard(self, batch_id, original_hash=None):
        """Keep task creation/export excluded through a local history delete.

        When False is yielded, deletion must not proceed: this phase does not
        yet erase durable job snapshots. When True is yielded, the caller may
        delete that history entry locally before leaving the context. Never
        make model/network calls while holding this short writer transaction.
        """
        with self._connection(write=True) as conn:
            allowed = not self._has_related_jobs(conn, batch_id, original_hash)
            previous = getattr(self._report_context, "connection", None)
            self._report_context.connection = conn
            try:
                yield allowed
            finally:
                if previous is None:
                    del self._report_context.connection
                else:
                    self._report_context.connection = previous

    def pending_messages(self, limit=100):
        _positive(limit, maximum=1000)
        with self._connection() as conn:
            return [dict(row) | {"payload": {"job_id": row["job_id"]}}
                    for row in conn.execute("SELECT * FROM job_outbox WHERE status='pending' ORDER BY created_at,id LIMIT ?", (limit,))]

    def enqueue_job(self, job_id):
        now = self.clock()
        with self._connection(write=True) as conn:
            job = self._job(conn, job_id)
            if (job["cancel_requested"] or job["status"] in _TERMINAL
                    or job["error_code"] in _PAUSE_CODES):
                return None
            return self._enqueue(conn, job_id, now)

    def recover_dispatches(self, age_seconds=60):
        """Recover a notification lost after broker publication/Redis restart.

        Sent and leased groups are handled only by their lease expiry path.
        Recovery never clears a pause, cancellation or uncertain transport.
        """
        if type(age_seconds) not in (int, float) or not math.isfinite(age_seconds) or age_seconds < 0:
            raise JobStoreError("任务消息恢复期限无效。")
        now, recovered = self.clock(), 0
        with self._connection(write=True) as conn:
            self._expire(conn, None, now)
            jobs = conn.execute("""SELECT id FROM analysis_jobs j
                WHERE (
                  (status IN ('queued','running','partial') AND cancel_requested=0
                   AND EXISTS (SELECT 1 FROM analysis_groups g WHERE g.job_id=j.id AND g.status='pending'))
                  OR (status IN ('completed','partial','failed','cancelled','needs_review')
                      AND batch_id IS NULL AND report_superseded_by IS NULL)
                )
                AND NOT EXISTS (SELECT 1 FROM analysis_groups g WHERE g.job_id=j.id AND g.status IN ('leased','sent'))
                AND NOT EXISTS (SELECT 1 FROM job_outbox o WHERE o.job_id=j.id AND o.status='pending')
                AND COALESCE((SELECT MAX(COALESCE(o.published_at,o.created_at)) FROM job_outbox o WHERE o.job_id=j.id),j.created_at)<=?
                ORDER BY created_at,id""", (now - age_seconds,)).fetchall()
            for row in jobs:
                job = self._job(conn, row["id"])
                needs_finalization = (job["batch_id"] is None and job["status"]
                                      in {"completed", "partial", "failed", "cancelled", "needs_review"})
                if (not needs_finalization and (job["error_code"] in _PAUSE_CODES
                        or job["actual_requests"] >= job["max_requests"])):
                    continue
                self._enqueue(conn, job["id"], now)
                recovered += 1
            return recovered

    def acknowledge_message(self, message_id):
        with self._connection(write=True) as conn:
            conn.execute("UPDATE job_outbox SET status='published',attempts=attempts+1,published_at=? WHERE id=? AND status='pending'",
                         (self.clock(), _identifier(message_id)))

    def fail_message(self, message_id, error=None):
        with self._connection(write=True) as conn:
            conn.execute("UPDATE job_outbox SET attempts=attempts+1,last_error=? WHERE id=? AND status='pending'",
                         ("任务消息尚未发送，等待重试。", _identifier(message_id)))

