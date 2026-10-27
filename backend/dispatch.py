"""Durable outbox publishing and a shared, token-owned Redis request semaphore."""

from __future__ import annotations

import logging
import os
import signal
import threading
from uuid import uuid4

import redis

from core.config import Config

logger = logging.getLogger(__name__)


def lease_seconds():
    value = int(os.getenv("APP_JOB_LEASE_SECONDS", str(int(max(120, Config.REQUEST_TIMEOUT_SECONDS + 60)))))
    if not max(90, Config.REQUEST_TIMEOUT_SECONDS + 60) <= value <= 86400:
        raise ValueError("APP_JOB_LEASE_SECONDS 必须覆盖模型超时并预留至少 60 秒。")
    return value


class SlotUnavailable(RuntimeError):
    """No upstream call may start without a live distributed permit."""


class RedisSemaphore:
    # Redis time prevents worker clock skew from granting excess capacity.
    _ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
local last = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
redis.call('PEXPIREAT', KEYS[1], math.floor(tonumber(last[2])) + 1000)
return 1
"""
    _RENEW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
local last = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
redis.call('PEXPIREAT', KEYS[1], math.floor(tonumber(last[2])) + 1000)
return 1
"""
    _RELEASE = """
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('ZCARD', KEYS[1]) == 0 then redis.call('DEL', KEYS[1]) end
return removed
"""

    def __init__(self, client=None, *, limit=None, ttl_seconds=None):
        from backend.celery_app import redis_url
        self.client = client or redis.Redis.from_url(
            redis_url(), socket_connect_timeout=3, socket_timeout=3,
            decode_responses=True,
        )
        self.limit = int(os.getenv("APP_GLOBAL_INFLIGHT", "2")) if limit is None else limit
        self.ttl_seconds = lease_seconds() if ttl_seconds is None else ttl_seconds
        if type(self.limit) is not int or not 1 <= self.limit <= 16 or self.ttl_seconds <= 0:
            raise ValueError("全局模型并发配置无效。")
        self.key = "customerinsight:upstream:permits:v1"

    def acquire(self):
        token = uuid4().hex
        try:
            granted = self.client.eval(self._ACQUIRE, 1, self.key, token,
                                       self.limit, int(self.ttl_seconds * 1000))
        except redis.RedisError:
            raise SlotUnavailable("共享请求服务暂时不可用，未发送模型请求。") from None
        if not granted:
            raise SlotUnavailable("共享请求槽位正忙，任务已保留。")
        return token

    def renew(self, token):
        try:
            return bool(self.client.eval(self._RENEW, 1, self.key, token,
                                         int(self.ttl_seconds * 1000)))
        except redis.RedisError:
            return False

    def release(self, token):
        if token is not None:
            try:
                self.client.eval(self._RELEASE, 1, self.key, token)
            except redis.RedisError:
                # Expiry releases only this token; never delete another owner's lock.
                logger.warning("request_permit_release_deferred")


def dispatch_pending(store=None, *, celery=None, limit=50):
    """Publish immutable IDs, then acknowledge; crashes may duplicate safely.

    A published notification can be lost across a broker failure. The sweep
    re-enqueues inactive jobs from SQLite after a grace period, while sent or
    uncertain groups remain fenced from automatic paid retries.
    """
    if store is None:
        store = default_job_store()
    if celery is None:
        from backend.celery_app import celery_app
        celery = celery_app
    store.expire_leases()
    store.recover_dispatches()
    published, deferred = 0, 0
    for message in store.pending_messages(limit=limit):
        try:
            celery.send_task(
                "customerinsight.process_job", args=[message["job_id"]],
                task_id=message["id"], queue="analysis", serializer="json",
                ignore_result=True, retry=False,
            )
        except Exception:
            store.fail_message(message["id"])
            deferred += 1
            logger.warning("analysis_dispatch_deferred job_id=%s", message["job_id"])
            from backend.observability import emit_event
            emit_event("dispatch_deferred", job_id=message["job_id"], deferred=1)
            # Broker outages should not multiply connection attempts per job.
            break
        else:
            store.acknowledge_message(message["id"])
            published += 1
            from backend.observability import emit_event
            emit_event("dispatch_published", job_id=message["job_id"], published=1)
    return {"published": published, "deferred": deferred}


def default_job_store():
    from backend.database import database_enabled
    if database_enabled():
        from backend.pg_jobs import PgJobStore
        return PgJobStore()
    from backend.job_store import JobStore
    from backend.service import _root_directory
    return JobStore(_root_directory())


def main():
    """Run publication independently of long-running analysis queue consumers."""
    from backend.celery_app import celery_app
    from backend.job_store import JobStore
    from backend.service import _root_directory
    from backend.operations import ServiceHeartbeat
    from backend.observability import emit_event

    interval = int(os.getenv("APP_DISPATCH_INTERVAL_SECONDS", "5"))
    if not 1 <= interval <= 300:
        raise ValueError("任务投递检查间隔必须为 1 到 300 秒。")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()

    def shutdown(_signal, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    store = default_job_store()
    heartbeat = ServiceHeartbeat("dispatcher", interval=interval, ttl=max(20, interval * 3 + 10))
    logger.info("analysis_dispatcher_started interval_seconds=%s", interval)
    emit_event("dispatcher_started", interval_seconds=interval)
    try:
        while not stop.is_set():
            try:
                outcome = dispatch_pending(store, celery=celery_app)
                heartbeat.pulse()
                if outcome["published"]:
                    logger.info("analysis_notifications_published count=%s", outcome["published"])
            except Exception as error:
                # A blocked or failing dispatch loop stops renewing readiness.
                logger.warning("analysis_dispatcher_deferred error_type=%s", type(error).__name__)
            stop.wait(interval)
    finally:
        heartbeat.close()
        emit_event("dispatcher_stopped")
        logger.info("analysis_dispatcher_stopped")


if __name__ == "__main__":
    main()

