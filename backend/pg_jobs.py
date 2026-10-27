"""PostgreSQL durable jobs, sharing the proven offline job state machine.

The SQL bridge below has two deliberately small responsibilities: bind qmark
parameters through SQLAlchemy and translate SQLite insertion order to an
identity column. It never interpolates values or changes selection predicates.
Workspace ownership is checked by repository methods and enforced again by
composite foreign keys on all child records.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import math
import os
from pathlib import Path
import re
import threading
import time

from sqlalchemy import (BigInteger, Column, Float, ForeignKey, ForeignKeyConstraint,
                        Identity, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint, text)

from backend.database import Base, get_engine, workspace_root
from backend.job_store import (JobNotFoundError, JobStore, JobStoreError,
                               _identifier, _positive, _TERMINAL, _PAUSE_CODES)


# A separate lock namespace from history/account locks prevents a report guard
# from deadlocking while the local history repository commits its own DB work.
_WRITE_LOCK = 73639021006710603


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id = Column(String(32), primary_key=True)
    scope = Column(String(32), ForeignKey("app_workspaces.id"), nullable=False)
    generation = Column(BigInteger().with_variant(Integer, "sqlite"), Identity(), nullable=False, unique=True)
    dedupe_key = Column(String(64), nullable=False)
    source_key = Column(String(64))
    report_superseded_by = Column(String(32))
    status = Column(String(24), nullable=False)
    filename = Column(String(255), nullable=False)
    original = Column(LargeBinary, nullable=False)
    original_hash = Column(String(64), nullable=False)
    request_json = Column(Text, nullable=False)
    config_json = Column(Text, nullable=False)
    max_requests = Column(Integer, nullable=False)
    actual_requests = Column(Integer, nullable=False, server_default="0")
    total_rows = Column(Integer, nullable=False)
    total_groups = Column(Integer, nullable=False)
    cancel_requested = Column(Integer, nullable=False, server_default="0")
    batch_id = Column(String(32))
    error_code = Column(String(80))
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint("scope", "id", name="uq_jobs_scope_id"),
        ForeignKeyConstraint(["scope", "report_superseded_by"], ["analysis_jobs.scope", "analysis_jobs.id"], name="fk_jobs_superseded_scope"),
        Index("ix_pg_jobs_dedupe", "scope", "dedupe_key", "generation"),
        Index("ix_pg_jobs_source", "scope", "source_key"),
        Index("ix_pg_jobs_recent", "scope", "created_at", "id"),
    )


class JobIdempotency(Base):
    __tablename__ = "job_idempotency"
    scope = Column(String(32), ForeignKey("app_workspaces.id"), primary_key=True)
    key_hash = Column(String(64), primary_key=True)
    dedupe_key = Column(String(64), nullable=False)
    job_id = Column(String(32), nullable=False)
    __table_args__ = (ForeignKeyConstraint(["scope", "job_id"], ["analysis_jobs.scope", "analysis_jobs.id"], name="fk_job_idempotency_scope"),)


class AnalysisGroup(Base):
    __tablename__ = "analysis_groups"
    id = Column(String(32), primary_key=True)
    job_id = Column(String(32), nullable=False)
    scope = Column(String(32), ForeignKey("app_workspaces.id"), nullable=False)
    group_index = Column(Integer, nullable=False)
    input_json = Column(Text, nullable=False)
    input_hash = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False)
    lease_token = Column(String(32))
    lease_until = Column(Float)
    worker_id = Column(String(255))
    attempt_count = Column(Integer, nullable=False, server_default="0")
    error = Column(Text)
    usage_json = Column(Text, nullable=False, server_default="{}")
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["scope", "job_id"], ["analysis_jobs.scope", "analysis_jobs.id"], name="fk_groups_job_scope"),
        UniqueConstraint("job_id", "group_index", name="uq_groups_job_index"),
        UniqueConstraint("scope", "job_id", "id", name="uq_groups_scope_job_id"),
        Index("ix_pg_groups_ready", "job_id", "status", "group_index"),
    )


class AnalysisResult(Base):
    __tablename__ = "analysis_results"
    job_id = Column(String(32), primary_key=True)
    feedback_id = Column(String(255), primary_key=True)
    source_row = Column(Integer, nullable=False)
    group_id = Column(String(32))
    result_json = Column(Text, nullable=False)
    scope = Column(String(32), ForeignKey("app_workspaces.id"), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["scope", "job_id"], ["analysis_jobs.scope", "analysis_jobs.id"], name="fk_results_job_scope"),
        ForeignKeyConstraint(["scope", "job_id", "group_id"], ["analysis_groups.scope", "analysis_groups.job_id", "analysis_groups.id"], name="fk_results_group_scope"),
        UniqueConstraint("job_id", "source_row", name="uq_results_source_row"),
    )


class AnalysisAttempt(Base):
    __tablename__ = "analysis_attempts"
    id = Column(String(32), primary_key=True)
    job_id = Column(String(32), nullable=False)
    group_id = Column(String(32), nullable=False)
    scope = Column(String(32), ForeignKey("app_workspaces.id"), nullable=False)
    lease_token = Column(String(32), nullable=False, unique=True)
    status = Column(String(24), nullable=False)
    usage_json = Column(Text, nullable=False, server_default="{}")
    error = Column(Text)
    created_at = Column(Float, nullable=False)
    completed_at = Column(Float)
    __table_args__ = (
        ForeignKeyConstraint(["scope", "job_id"], ["analysis_jobs.scope", "analysis_jobs.id"], name="fk_attempts_job_scope"),
        ForeignKeyConstraint(["scope", "job_id", "group_id"], ["analysis_groups.scope", "analysis_groups.job_id", "analysis_groups.id"], name="fk_attempts_group_scope"),
    )


class JobOutbox(Base):
    __tablename__ = "job_outbox"
    id = Column(String(32), primary_key=True)
    job_id = Column(String(32), nullable=False)
    scope = Column(String(32), ForeignKey("app_workspaces.id"), nullable=False)
    status = Column(String(24), nullable=False)
    attempts = Column(Integer, nullable=False, server_default="0")
    last_error = Column(Text)
    created_at = Column(Float, nullable=False)
    published_at = Column(Float)
    __table_args__ = (
        ForeignKeyConstraint(["scope", "job_id"], ["analysis_jobs.scope", "analysis_jobs.id"], name="fk_outbox_job_scope"),
        Index("ix_pg_outbox_pending", "status", "created_at"),
    )


class _Row(Mapping):
    """Support the keyed and positional reads used by the SQLite state machine."""

    def __init__(self, row):
        self.mapping = row._mapping
        self.values = tuple(row)

    def __getitem__(self, key):
        return self.values[key] if isinstance(key, (int, slice)) else self.mapping[key]

    def __iter__(self):
        return iter(self.mapping)

    def __len__(self):
        return len(self.mapping)


class _Result:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        row = self.result.fetchone()
        return _Row(row) if row is not None else None

    def fetchall(self):
        return [_Row(row) for row in self.result.fetchall()]

    def __iter__(self):
        return (_Row(row) for row in self.result)


class _Connection:
    """Finite SQL adaptation, never a general SQLite compatibility layer."""

    def __init__(self, connection):
        self.raw = connection
        self._scopes = {}

    def _statement(self, statement, parameters):
        statement = re.sub(r"\browid\b", "generation", statement)
        values = tuple(parameters)
        # Parent scope is read inside the same writer transaction and the
        # composite FK protects against accidental ownership mismatch.
        match = re.match(r"\s*INSERT INTO (analysis_groups|analysis_results|analysis_attempts|job_outbox)\b", statement, re.I)
        if match:
            table = match.group(1).lower()
            job_id = values[0] if table == "analysis_results" else values[1]
            if job_id not in self._scopes:
                parent = self.raw.execute(text("SELECT scope FROM analysis_jobs WHERE id=:id"), {"id": job_id}).first()
                if parent is None:
                    raise JobNotFoundError("分析任务不存在。")
                self._scopes[job_id] = parent[0]
            if table == "analysis_results":
                statement = re.sub(r"INSERT INTO analysis_results\s+VALUES\s*\(([^)]+)\)",
                                   r"INSERT INTO analysis_results(job_id,feedback_id,source_row,group_id,result_json,scope) VALUES(\1,?)",
                                   statement, count=1, flags=re.I)
            else:
                statement = re.sub(r"(INSERT INTO " + table + r"\s*\()([^)]+)(\)\s*VALUES\s*\()([^)]+)(\))",
                                   r"\1\2,scope\3\4,?\5", statement, count=1, flags=re.I)
            values += (self._scopes[job_id],)
        # SQLite test engines have no identity support on non-primary columns;
        # production PostgreSQL always allocates generation via IDENTITY.
        if self.raw.dialect.name == "sqlite" and re.match(r"\s*INSERT INTO analysis_jobs\b", statement, re.I):
            generation = self.raw.execute(text("SELECT COALESCE(MAX(generation),0)+1 FROM analysis_jobs")).scalar_one()
            statement = re.sub(r"(INSERT INTO analysis_jobs\s*\()([^)]+)(\)\s*VALUES\s*\()([^)]+)(\))",
                               r"\1\2,generation\3\4,?\5", statement, count=1, flags=re.I)
            values += (generation,)
        if statement.count("?") != len(values):
            raise RuntimeError("Unexpected job repository statement binding")
        iterator = iter(range(len(values)))
        bound = re.sub(r"\?", lambda _: f":p{next(iterator)}", statement)
        return text(bound), {f"p{index}": value for index, value in enumerate(values)}

    def execute(self, statement, parameters=()):
        statement, parameters = self._statement(statement, parameters)
        return _Result(self.raw.execute(statement, parameters))

    def executemany(self, statement, parameters):
        # Initial rows are bounded by the job admission limit and belong to one
        # transaction. Per-row bindings keep scope inference explicit.
        for values in parameters:
            self.execute(statement, values)


class PgJobStore(JobStore):
    """Tenant-bound API repository, or trusted unbound worker repository.

    DDL is owned by Alembic: construction does not create or alter tables.
    Writes use a short, repository-specific PostgreSQL advisory transaction lock
    to preserve the SQLite state machine's serial writer/fencing guarantees.
    No transport or model invocation may occur while this transaction is open.
    """

    def __init__(self, engine=None, workspace_id=None, data_dir=None, *, clock=None):
        self.engine = engine or get_engine()
        self.scope = None if workspace_id is None else _identifier(workspace_id)
        self.workspace_id = self.scope
        self.root_data_dir = Path(data_dir or os.getenv("APP_DATA_DIR") or Path(".runtime") / "history").resolve()
        self.root_dir = workspace_root(self.scope, data_dir=self.root_data_dir) if self.scope else self.root_data_dir
        self.clock = clock or time.time
        self._report_context = threading.local()
        self._sqlite_lock = threading.RLock()

    @contextmanager
    def _connection(self, *, write=False):
        guarded = getattr(self._report_context, "connection", None)
        if guarded is not None:
            yield guarded
            return
        with self._sqlite_lock:
            with self.engine.begin() as conn:
                if write and conn.dialect.name == "postgresql":
                    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _WRITE_LOCK})
                yield _Connection(conn)

    def _job(self, conn, job_id):
        job_id = _identifier(job_id)
        if self.scope is None:
            return super()._job(conn, job_id)
        # Avoid exposing either ownership or the existence of another tenant's
        # ID. Every inherited per-job operation reaches this check first.
        from backend.job_store import _JOB_COLUMNS
        row = conn.execute(f"SELECT {_JOB_COLUMNS} FROM analysis_jobs WHERE id=? AND scope=?", (job_id, self.scope)).fetchone()
        if row is None:
            raise JobNotFoundError("分析任务不存在。")
        return row

    def create_job(self, *, scope="workspace", **kwargs):
        if self.scope is not None:
            if scope not in ("workspace", self.scope):
                raise JobNotFoundError("分析任务不存在。")
            scope = self.scope
        else:
            scope = _identifier(scope)
        return super().create_job(scope=scope, **kwargs)

    def list_jobs(self, *, limit=20, offset=0, scope="workspace"):
        if self.scope is not None:
            if scope not in ("workspace", self.scope):
                raise JobNotFoundError("分析任务不存在。")
            scope = self.scope
        elif scope == "workspace":
            _positive(limit, maximum=100)
            if type(offset) is not int or offset < 0:
                raise JobStoreError("分页位置无效。")
            with self._connection() as conn:
                total = conn.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0]
                ids = conn.execute("SELECT id FROM analysis_jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
                return {"items": [self._projection(conn, row["id"]) for row in ids], "total": total,
                        "limit": limit, "offset": offset}
        else:
            scope = _identifier(scope)
        return super().list_jobs(limit=limit, offset=offset, scope=scope)

    def _expire(self, conn, job_id, now):
        if self.scope is None or job_id is not None:
            return super()._expire(conn, job_id, now)
        ids = conn.execute("SELECT id FROM analysis_jobs WHERE scope=?", (self.scope,)).fetchall()
        return sum(super(PgJobStore, self)._expire(conn, row["id"], now) for row in ids)

    def pending_messages(self, limit=100):
        if self.scope is None:
            return super().pending_messages(limit)
        _positive(limit, maximum=1000)
        with self._connection() as conn:
            return [dict(row) | {"payload": {"job_id": row["job_id"]}}
                    for row in conn.execute("SELECT * FROM job_outbox WHERE scope=? AND status='pending' ORDER BY created_at,id LIMIT ?", (self.scope, limit))]

    def _message(self, conn, message_id):
        message_id = _identifier(message_id)
        row = conn.execute("SELECT job_id FROM job_outbox WHERE id=? AND scope=?", (message_id, self.scope)).fetchone()
        if row is None:
            raise JobNotFoundError("分析任务不存在。")

    def acknowledge_message(self, message_id):
        if self.scope is None:
            return super().acknowledge_message(message_id)
        with self._connection(write=True) as conn:
            self._message(conn, message_id)
            conn.execute("UPDATE job_outbox SET status='published',attempts=attempts+1,published_at=? WHERE id=? AND status='pending'", (self.clock(), message_id))

    def fail_message(self, message_id, error=None):
        if self.scope is None:
            return super().fail_message(message_id, error)
        with self._connection(write=True) as conn:
            self._message(conn, message_id)
            conn.execute("UPDATE job_outbox SET attempts=attempts+1,last_error=? WHERE id=? AND status='pending'", ("任务消息尚未发送，等待重试。", message_id))

    def recover_dispatches(self, age_seconds=60):
        if self.scope is None:
            return super().recover_dispatches(age_seconds)
        if type(age_seconds) not in (int, float) or not math.isfinite(age_seconds) or age_seconds < 0:
            raise JobStoreError("任务消息恢复期限无效。")
        now, recovered = self.clock(), 0
        with self._connection(write=True) as conn:
            self._expire(conn, None, now)
            jobs = conn.execute("""SELECT id FROM analysis_jobs j WHERE scope=? AND (
                (status IN ('queued','running','partial') AND cancel_requested=0
                 AND EXISTS (SELECT 1 FROM analysis_groups g WHERE g.job_id=j.id AND g.status='pending'))
                OR (status IN ('completed','partial','failed','cancelled','needs_review') AND batch_id IS NULL AND report_superseded_by IS NULL))
                AND NOT EXISTS (SELECT 1 FROM analysis_groups g WHERE g.job_id=j.id AND g.status IN ('leased','sent'))
                AND NOT EXISTS (SELECT 1 FROM job_outbox o WHERE o.job_id=j.id AND o.status='pending')
                AND COALESCE((SELECT MAX(COALESCE(o.published_at,o.created_at)) FROM job_outbox o WHERE o.job_id=j.id),j.created_at)<=?
                ORDER BY created_at,id""", (self.scope, now - age_seconds)).fetchall()
            for row in jobs:
                job = self._job(conn, row["id"])
                needs_finalization = (job["batch_id"] is None and job["status"] in _TERMINAL | {"partial"})
                if not needs_finalization and (job["error_code"] in _PAUSE_CODES or job["actual_requests"] >= job["max_requests"]):
                    continue
                self._enqueue(conn, job["id"], now)
                recovered += 1
            return recovered

    def _has_related_jobs(self, conn, batch_id, original_hash=None):
        if self.scope is None:
            return super()._has_related_jobs(conn, batch_id, original_hash)
        _identifier(batch_id)
        if original_hash is not None and (not isinstance(original_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", original_hash)):
            raise JobStoreError("原文件校验值无效。")
        if original_hash is None:
            row = conn.execute("SELECT 1 FROM analysis_jobs WHERE scope=? AND batch_id=? LIMIT 1", (self.scope, batch_id)).fetchone()
        else:
            row = conn.execute("SELECT 1 FROM analysis_jobs WHERE scope=? AND (batch_id=? OR original_hash=?) LIMIT 1", (self.scope, batch_id, original_hash)).fetchone()
        return row is not None
