"""Bounded readiness, service leases and tenant-scoped operational summaries."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time

import redis


PREFIX = "customerinsight:service:v1:"
SCHEMA_REVISION = "20261022_01"

# Fixed per-role indices keep readiness independent of Redis keyspace size.
# Redis TIME avoids clock skew between containers; at most 32 leases are read.
_PULSE = """-- service-pulse
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
redis.call('SET',KEYS[1],ARGV[1],'PX',ARGV[2])
redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now)
redis.call('ZADD',KEYS[2],now+tonumber(ARGV[2]),KEYS[1])
local last=redis.call('ZRANGE',KEYS[2],-1,-1,'WITHSCORES')
redis.call('PEXPIREAT',KEYS[2],tonumber(last[2])+1000)
return 1
"""
_CLOSE = """-- service-close
if redis.call('GET',KEYS[1])==ARGV[1] then
  redis.call('DEL',KEYS[1]); redis.call('ZREM',KEYS[2],KEYS[1])
  if redis.call('ZCARD',KEYS[2])==0 then redis.call('DEL',KEYS[2]) end
  return 1
end
return 0
"""
_ALIVE = """-- service-alive
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local keys=redis.call('ZRANGEBYSCORE',KEYS[1],now+1,'+inf','LIMIT',0,32)
for _,key in ipairs(keys) do if redis.call('EXISTS',key)==1 then return 1 end end
return 0
"""


def redis_client():
    from backend.celery_app import redis_url
    return redis.Redis.from_url(redis_url(), decode_responses=True,
                               socket_connect_timeout=1, socket_timeout=1)


def heartbeat_key(role, hostname=None):
    if role not in {"worker", "dispatcher"}:
        raise ValueError("Unknown service role")
    identity = hashlib.sha256((hostname or socket.gethostname()).encode()).hexdigest()[:24]
    return PREFIX + role + ":" + identity


class ServiceHeartbeat:
    """A renewable, owned Redis TTL; no model calls or customer payloads."""
    def __init__(self, role, *, client=None, interval=5, ttl=20, hostname=None):
        from uuid import uuid4
        if not 0 < interval < ttl <= 3600:
            raise ValueError("Invalid heartbeat timing")
        self.role, self.key = role, heartbeat_key(role, hostname)
        self.index = PREFIX + role + ":index"
        self.client = client if client is not None else redis_client()
        self.interval, self.ttl = interval, int(ttl)
        self.token = uuid4().hex
        self.stop = threading.Event()
        self.thread = None

    def pulse(self):
        try:
            return bool(self.client.eval(_PULSE, 2, self.key, self.index, self.token, self.ttl * 1000))
        except redis.RedisError:
            return False

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return self
        self.stop.clear()
        self.pulse()
        def renew():
            while not self.stop.wait(self.interval):
                self.pulse()
        self.thread = threading.Thread(target=renew, name="service-heartbeat", daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        try:
            self.client.eval(_CLOSE, 2, self.key, self.index, self.token)
        except redis.RedisError:
            pass


class ReadinessProbe:
    def __init__(self, *, engine=None, client=None, queued=None, data_dir=None):
        self.engine = engine
        self.client = client
        self.data_dir = data_dir
        self.queued = (os.getenv("APP_ANALYSIS_MODE", "direct").strip().lower() == "queued") if queued is None else queued

    def _database(self):
        from backend.database import database_enabled
        owned = None
        try:
            engine = self.engine
            if engine is None and database_enabled():
                # A readiness probe must not wait for a saturated request pool.
                from sqlalchemy import create_engine
                from sqlalchemy.engine import make_url
                from sqlalchemy.pool import NullPool
                url = make_url(os.environ["APP_DATABASE_URL"])
                if url.get_backend_name() != "postgresql":
                    return False
                if url.drivername == "postgresql":
                    url = url.set(drivername="postgresql+psycopg")
                owned = engine = create_engine(url, poolclass=NullPool,
                    connect_args={"connect_timeout": 2}, echo=False, hide_parameters=True)
            if engine is not None:
                with engine.connect() as connection:
                    if engine.dialect.name == "postgresql":
                        connection.exec_driver_sql("SET LOCAL statement_timeout = 1500")
                        revisions = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
                        if revisions != [SCHEMA_REVISION]:
                            return False
                    return connection.exec_driver_sql("SELECT 1").scalar_one() == 1
            # Local legacy mode is lazy-created. Check the configured directory
            # without instantiating or migrating a history database.
            from backend.service import _root_directory
            root = Path(self.data_dir) if self.data_dir is not None else _root_directory()
            while not root.exists() and root != root.parent:
                root = root.parent
            return root.is_dir() and os.access(root, os.W_OK)
        except Exception:
            return False
        finally:
            if owned is not None:
                owned.dispose()

    def check(self):
        checks = {"database": self._database(), "redis": True, "worker": True, "dispatcher": True}
        if self.queued:
            checks.update(redis=False, worker=False, dispatcher=False)
            try:
                client = self.client if self.client is not None else redis_client()
                checks["redis"] = bool(client.ping())
                if checks["redis"]:
                    for role in ("worker", "dispatcher"):
                        checks[role] = bool(client.eval(_ALIVE, 1, PREFIX + role + ":index"))
            except Exception:
                checks.update(redis=False, worker=False, dispatcher=False)
        return {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks,
                "queue_required": bool(self.queued), "model_checked": False}


def _percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


def _summary(jobs, groups, attempts, pending_outbox, budget, now):
    durations, tokens = [], Counter()
    for row in attempts:
        if row["completed_at"] is not None:
            durations.append(max(0, (row["completed_at"] - row["created_at"]) * 1000))
        try:
            usage = json.loads(row["usage_json"])
        except (ValueError, TypeError):
            usage = {}
        for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
            value = usage.get(key) if isinstance(usage, dict) else None
            if type(value) is int and value >= 0:
                tokens[key] += value
    states = {row["status"]: row["count"] for row in jobs}
    queued_at = [row["oldest"] for row in jobs if row["status"] == "queued" and row["oldest"] is not None]
    return {"jobs_by_status": states, "groups_by_status": {row["status"]: row["count"] for row in groups},
            "reserved_requests": sum(row["requests"] for row in jobs),
            "pending_notifications": pending_outbox,
            "oldest_queued_seconds": round(max(0, now - min(queued_at)), 3) if queued_at else 0,
            "attempt_window": {"sample_size": len(attempts), "limit": 1000, "tokens": dict(tokens),
                "latency_ms": {"count": len(durations), "p50": _percentile(durations, .5), "p95": _percentile(durations, .95)}},
            "model_automatic_retries": 0,
            "daily_budget": {"scope": "deployment", "limit": budget.daily_limit, "remaining": budget.remaining()}}


def operations_snapshot(store, *, budget, now=None):
    """Read aggregates only; never expire a lease or load original/config text."""
    now = time.time() if now is None else now
    if hasattr(store, "engine"):
        from sqlalchemy import select, func
        from backend.pg_jobs import AnalysisJob, AnalysisGroup, AnalysisAttempt, JobOutbox
        if not store.scope:
            raise ValueError("Metrics require an authenticated workspace")
        j, g, a, o = (model.__table__ for model in (AnalysisJob, AnalysisGroup, AnalysisAttempt, JobOutbox))
        with store.engine.connect() as conn:
            jobs = conn.execute(select(j.c.status, func.count().label("count"),
                func.sum(j.c.actual_requests).label("requests"), func.min(j.c.created_at).label("oldest"))
                .where(j.c.scope == store.scope).group_by(j.c.status)).mappings().all()
            groups = conn.execute(select(g.c.status, func.count().label("count"))
                .where(g.c.scope == store.scope).group_by(g.c.status)).mappings().all()
            attempts = conn.execute(select(a.c.created_at, a.c.completed_at, a.c.usage_json)
                .where(a.c.scope == store.scope).order_by(a.c.created_at.desc(), a.c.id.desc()).limit(1000)).mappings().all()
            pending = conn.execute(select(func.count()).select_from(o).where(o.c.scope == store.scope, o.c.status == "pending")).scalar_one()
    elif Path(store.path).is_file():
        with sqlite3.connect(Path(store.path).as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            jobs = conn.execute("SELECT status,COUNT(*) AS count,SUM(actual_requests) AS requests,MIN(created_at) AS oldest FROM analysis_jobs GROUP BY status").fetchall()
            groups = conn.execute("SELECT status,COUNT(*) AS count FROM analysis_groups GROUP BY status").fetchall()
            attempts = conn.execute("SELECT created_at,completed_at,usage_json FROM analysis_attempts ORDER BY created_at DESC,id DESC LIMIT 1000").fetchall()
            pending = conn.execute("SELECT COUNT(*) FROM job_outbox WHERE status='pending'").fetchone()[0]
    else:
        jobs, groups, attempts, pending = [], [], [], 0
    return _summary(jobs, groups, attempts, pending, budget, now)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=["worker", "dispatcher", "readiness"])
    args = parser.parse_args()
    if args.check == "readiness":
        result = ReadinessProbe().check()
        print(json.dumps(result))
        return 0 if result["status"] == "ready" else 1
    try:
        return 0 if redis_client().exists(heartbeat_key(args.check)) else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
