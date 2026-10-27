"""JSON-only work messages; durable business state lives in JobStore, not Redis."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from celery import Celery
from celery.signals import worker_ready, worker_shutdown
from core.config import Config  # Load the same deployment settings as the API.


def redis_url():
    value = os.getenv("APP_REDIS_URL", "redis://redis:6379/0")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.fragment:
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError("APP_REDIS_URL 配置无效。") from None
    return value


celery_app = Celery("customerinsight", broker=redis_url(), backend=None)
celery_app.conf.update(
    accept_content=["json"], task_serializer="json", result_serializer="json",
    task_ignore_result=True, task_store_errors_even_if_ignored=False,
    task_acks_late=True, task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=True, worker_prefetch_multiplier=1,
    worker_concurrency=1, task_default_queue="analysis",
    broker_connection_timeout=3, broker_connection_max_retries=3,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 3600, "socket_connect_timeout": 3,
                              "socket_timeout": 3},
    task_publish_retry=False, worker_hijack_root_logger=False,
    worker_enable_remote_control=False, enable_utc=True, timezone="UTC",
    beat_schedule={"durable-outbox": {
        "task": "customerinsight.dispatch_outbox", "schedule": 15.0,
    }},
)
# Both conventional CLI spellings are supported.
app = celery_app
_service_heartbeat = None


@worker_ready.connect
def advertise_worker(**_kwargs):
    global _service_heartbeat
    from backend.operations import ServiceHeartbeat
    from backend.observability import emit_event
    if _service_heartbeat is not None:
        _service_heartbeat.close()
    _service_heartbeat = ServiceHeartbeat("worker").start()
    emit_event("worker_started")


@worker_shutdown.connect
def withdraw_worker(**_kwargs):
    global _service_heartbeat
    from backend.observability import emit_event
    if _service_heartbeat is not None:
        _service_heartbeat.close()
        _service_heartbeat = None
    emit_event("worker_stopped")


class TaskExecutionError(RuntimeError):
    """A redacted task error, safe for Celery traceback and backend logs."""


@celery_app.task(name="customerinsight.process_job", ignore_result=True)
def process_job(job_id):
    from backend.job_worker import default_worker
    # A malformed broker payload cannot supply a path, model, or arbitrary task.
    from backend.service import identifier
    try:
        result = default_worker().run(identifier(job_id))
    except Exception as error:
        from backend.observability import emit_event
        emit_event("job_finished", job_id=job_id, status="interrupted", error_type=type(error).__name__)
        # Worker already saved its last checkpoint/unknown state. Celery must
        # not print a provider exception body or automatically retry a model.
        raise TaskExecutionError("Analysis task requires status review.") from None
    # Celery logs task return values at INFO even when result storage is off.
    # Never return the internal snapshot, original rows or provider settings.
    return {"job_id": result["id"], "status": result["status"],
            "request_count": result["actual_requests"]}


@celery_app.task(name="customerinsight.dispatch_outbox", ignore_result=True)
def dispatch_outbox():
    from backend.dispatch import dispatch_pending
    return dispatch_pending()
