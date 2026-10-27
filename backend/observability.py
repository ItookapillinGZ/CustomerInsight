"""Allowlisted JSON events: never format arbitrary upstream messages or cells."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
import re
import sys

_LOGGER = logging.getLogger("customerinsight.events")
_EVENTS = {"http_request", "http_error", "job_started", "job_finished", "group_started",
           "group_checkpoint", "group_interrupted", "dispatch_published", "dispatch_deferred",
           "worker_started", "worker_stopped", "dispatcher_started", "dispatcher_stopped"}
_IDENTIFIERS = {"job_id", "group_id", "workspace_id"}
_NUMBERS = {"elapsed_ms", "latency_ms", "queue_wait_ms", "request_count", "max_requests",
            "total_rows", "success_rows", "total_tokens", "prompt_tokens", "completion_tokens",
            "published", "deferred", "interval_seconds"}
_STATUSES = {"queued", "running", "partial", "completed", "failed", "cancelled", "needs_review",
             "succeeded", "uncertain", "interrupted"}


class _JsonHandler(logging.Handler):
    def emit(self, record):
        # Resolve stderr at emission time: Streamlit/test captures may replace it.
        try:
            sys.stderr.write(json.dumps(record.event_payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            pass


def event_payload(event, **fields):
    if event not in _EVENTS:
        raise ValueError("Unknown operational event")
    result = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event}
    for key, value in fields.items():
        if key in _IDENTIFIERS and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
            result[key] = value
        elif key == "request_id" and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value):
            result[key] = value
        elif key in _NUMBERS and type(value) in (int, float) and 0 <= value <= 10**18 and math.isfinite(value):
            result[key] = round(value, 3) if type(value) is float else value
        elif key == "status_code" and type(value) is int and 100 <= value <= 599:
            result[key] = value
        elif key == "status" and isinstance(value, str) and value in _STATUSES:
            result[key] = value
        elif key == "method" and isinstance(value, str) and value in {"GET", "POST", "DELETE", "PATCH", "PUT", "HEAD", "OPTIONS"}:
            result[key] = value
        elif key == "route" and isinstance(value, str) and (value == "unmatched" or re.fullmatch(r"/[a-zA-Z0-9_/{}/.-]{0,200}", value)):
            result[key] = value
        elif key == "error_type" and isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", value):
            result[key] = value
        elif key == "sent" and type(value) is bool:
            result[key] = value
    return result


def emit_event(event, **fields):
    # Diagnostics must never interrupt a paid request or a saved checkpoint.
    try:
        payload = event_payload(event, **fields)
        if not _LOGGER.handlers:
            _LOGGER.addHandler(_JsonHandler())
            _LOGGER.setLevel(logging.INFO)
            _LOGGER.propagate = False
        _LOGGER.info(event, extra={"event_payload": payload})
    except Exception:
        pass
