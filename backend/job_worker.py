"""One-request checkpoints, fenced leases and conservative paid-call recovery.

Messages contain only a job identifier. Originals, inputs, budgets, results and
usage are read from the immutable database snapshot. No provider call is retried
automatically after its outcome becomes unknown.
"""

from __future__ import annotations

import logging
import hashlib
import os
import threading
import time
from uuid import uuid4

from ai.client import get_openai_client
from ai.grouped_batch import analyze_grouped_batch
from core.config import Config
from core.csv_io import results_to_dataframe
from core.customer_report import build_customer_report
from core.history_store import HistoryStore, _unpack
from core.history_workflow import date_scope
from core.product_service import ProductLimitError, ProductRequestBudget
from models.enums import ProcessingStatus
from models.models import FeedbackResult

from backend.dispatch import RedisSemaphore, SlotUnavailable, lease_seconds
from backend.job_store import JobBudgetError, JobConflictError, JobStore
from backend.observability import emit_event

logger = logging.getLogger(__name__)
_TERMINAL = {"completed", "partial", "failed", "cancelled", "needs_review"}


class _Trace:
    def __init__(self, worker, job, group):
        self.worker, self.job, self.group = worker, job, group
        self.permit = None
        self.sent = False
        self.response_seen = False
        self.transport_unknown = False
        self.usage = {}
        self.stopped = False
        self.error_code = None
        self.lost = threading.Event()
        self.done = threading.Event()
        self.guard = threading.Lock()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def _identity(self):
        return self.job["id"], self.group["id"], self.group["lease_token"]

    def _heartbeat(self):
        while not self.done.wait(max(0.05, self.worker.lease_seconds / 3)):
            try:
                self.worker.store.renew_lease(*self._identity(), self.worker.lease_seconds)
                with self.guard:
                    if self.permit and not self.worker.semaphore.renew(self.permit):
                        self.lost.set()
            except Exception:
                self.lost.set()
            if self.lost.is_set():
                self.stopped = True
                return

    def start(self):
        self.thread.start()

    def close(self):
        self.done.set()
        self.thread.join(timeout=5)
        self._release()

    def _release(self):
        with self.guard:
            token, self.permit = self.permit, None
        self.worker.semaphore.release(token)

    def before_attempt(self):
        if self.sent or self.lost.is_set():
            raise JobConflictError("本组不能再次发送模型请求。")
        try:
            # Renew the fence immediately before reserving external capacity.
            self.worker.store.renew_lease(*self._identity(), self.worker.lease_seconds)
            with self.guard:
                self.permit = self.worker.semaphore.acquire()
            # The daily ledger is reserved first. A crash between these two
            # transactions can conservatively consume daily quota without a
            # transport, but can never allow an unbudgeted paid request.
            self.worker.budget.reserve(self.job["id"])
            self.worker.store.mark_sent(*self._identity())
            self.sent = True
        except SlotUnavailable:
            self.error_code = "request_capacity_unavailable"
            self.stopped = True
            self._release()
            raise
        except ProductLimitError:
            self.error_code = "daily_budget_exceeded"
            self.stopped = True
            self._release()
            raise
        except JobBudgetError:
            self.error_code = "request_budget_exhausted"
            self.stopped = True
            self._release()
            raise
        except Exception:
            self.error_code = "job_fence_rejected"
            self.stopped = True
            self._release()
            raise

    def response_received(self, usage):
        self._release()
        self.response_seen = True
        self.usage = usage if isinstance(usage, dict) else {}
        # Persist usage before parsing or Pydantic validation can fail.
        self.worker.store.checkpoint_usage(*self._identity(), usage=self.usage)

    def request_failed(self, error):
        self._release()
        if not self.response_seen and not isinstance(getattr(error, "status_code", None), int):
            # Timeouts and broken connections cannot tell us whether the
            # provider accepted or charged for the request. Preserve the
            # reservation and block every remaining group pending review.
            self.transport_unknown = True
            self.stopped = True
            self.error_code = "transport_outcome_unknown"
        if getattr(error, "status_code", None) in {401, 403}:
            self.stopped = True
            self.error_code = "provider_authorization_failed"


class JobWorker:
    """Inject transport only in isolated tests; production has no fake mode."""

    def __init__(self, store, *, history=None, budget=None, semaphore=None,
                 analysis_runner=None, client_factory=None, lease_duration=None):
        self.store = store
        self.history = history or HistoryStore(store.root_dir)
        self.budget = budget or ProductRequestBudget()
        self.semaphore = semaphore or RedisSemaphore()
        self.analysis_runner = analysis_runner or analyze_grouped_batch
        self.client_factory = client_factory or get_openai_client
        self.lease_seconds = lease_duration if lease_duration is not None else lease_seconds()
        self.worker_id = uuid4().hex

    @staticmethod
    def _configuration_matches(job):
        saved = job["config"]
        checks = {
            "model": Config.OPENAI_MODEL, "api_mode": Config.API_MODE,
            "chat_output_mode": Config.CHAT_OUTPUT_MODE,
            "chat_token_parameter": Config.CHAT_TOKEN_PARAMETER,
            "max_output_tokens": Config.MAX_OUTPUT_TOKENS,
            "reasoning_effort": Config.OPENAI_REASONING_EFFORT,
            "timeout_seconds": Config.REQUEST_TIMEOUT_SECONDS,
            "endpoint_hash": hashlib.sha256((Config.OPENAI_BASE_URL or "default").encode("utf-8")).hexdigest(),
            "prompt_version": "v3_grouped_aspects_v2",
        }
        return all(key not in saved or saved[key] == value for key, value in checks.items())

    def _finalize(self, job_id):
        job = self.store.get_job(job_id)
        if job["status"] not in _TERMINAL:
            return job
        if job.get("batch_id"):
            return job
        prepared = self._prepare_report(job_id)
        # Serialize export with job creation/resume. An older force-analysis
        # generation must never overwrite the newer source/mapping report.
        # This guard covers local database/file work only, never model I/O.
        with self.store.report_guard(job_id) as allowed:
            if not allowed:
                return self.store.get_job(job_id)
            current = self.store.get_job(job_id)
            prior = prepared[0]
            if any(current[key] != prior[key] for key in ("actual_requests", "status", "updated_at")):
                # A resume may finish between preparation and acquiring the
                # export fence. Rebuild only in that exceptional race.
                prepared = self._prepare_report(job_id)
            return self._export_report(job_id, prepared)

    def _prepare_report(self, job_id):
        job = self.store.get_job(job_id)
        # A report can be refreshed after a safe manual resume. Its business
        # identity remains the source hash + field mapping, matching history.
        config, request = job["config"], job["request"]
        frame = _unpack(config["frame_snapshot"])
        mapping = request.get("mapping") or {
            "sheet_name": request.get("sheet_name"),
            "encoding": None if job["filename"].lower().endswith(".xlsx") else request.get("encoding", "utf-8-sig"),
            "text_column": request["text_column"], "group_column": request.get("group_column"),
            "date_column": request.get("date_column"),
        }
        results = [FeedbackResult.model_validate(item) for item in self.store.get_results(job_id)]
        token_count = job.get("usage", {}).get("total_tokens")
        metadata = {
            "mode": "grouped_async", "prompt_version": config.get("prompt_version", "v3_grouped_aspects_v2"),
            "planned_requests": job["total_groups"], "actual_requests": job["actual_requests"],
            "batch_size": config.get("batch_size", 10), "max_characters": config.get("max_characters", 12000),
            "run_status": job["status"], "model": config.get("model", Config.OPENAI_MODEL),
            "job_id": job_id, "total_tokens": token_count,
            "usage_reported_requests": sum(bool(group.get("usage")) for group in job["groups"]),
            "elapsed_seconds": round(max(0, time.time() - job["created_at"]), 3),
        }
        report = build_customer_report(frame, results_to_dataframe(results), request["text_column"],
                                       request.get("group_column"), analysis_meta=metadata)
        report["source_frame"] = frame.copy(deep=True)
        report["period"] = date_scope(frame, request.get("date_column"))
        return job, report, mapping

    def _export_report(self, job_id, prepared):
        job, report, mapping = prepared
        batch_id = self.history.save_batch(report, filename=job["filename"],
                                           file_bytes=self.store.original(job_id), mapping=mapping)
        self.history.save_mapping(report["source_frame"].columns.tolist(), mapping)
        return self.store.set_report(job_id, batch_id)

    def _safe_failure(self, job, group, *, uncertain):
        try:
            self.store.record_failure(job["id"], group["id"], group["lease_token"], uncertain=uncertain)
        except JobConflictError:
            # A newer lease or explicit cancellation already fenced this owner.
            pass

    def run(self, job_id):
        started = time.perf_counter()
        initial = self.store.get_job(job_id)
        emit_event("job_started", job_id=job_id, workspace_id=initial.get("scope"),
                   total_rows=initial["total_rows"], request_count=initial["actual_requests"],
                   max_requests=initial["max_requests"])
        try:
            result = self._run(job_id)
        except BaseException as error:
            emit_event("job_finished", job_id=job_id, workspace_id=initial.get("scope"),
                       status="interrupted", error_type=type(error).__name__,
                       elapsed_ms=(time.perf_counter() - started) * 1000)
            raise
        emit_event("job_finished", job_id=job_id, workspace_id=result.get("scope"), status=result["status"],
                   request_count=result["actual_requests"], success_rows=result["success_rows"],
                   elapsed_ms=(time.perf_counter() - started) * 1000)
        return result

    def _run(self, job_id):
        self.store.expire_leases(job_id)
        job = self.store.get_job(job_id)
        if job["status"] in _TERMINAL:
            return self._finalize(job_id)
        if not self._configuration_matches(job):
            self.store.pause_job(job_id, "configuration_changed")
            return self._finalize(job_id)
        client = None
        try:
            while True:
                group = self.store.claim_group(job_id, self.worker_id, self.lease_seconds)
                if group is None:
                    break
                group_started = time.perf_counter()
                emit_event("group_started", job_id=job_id, group_id=group["id"], workspace_id=job.get("scope"),
                           total_rows=len(group["rows"]), queue_wait_ms=max(0, time.time() - group["created_at"]) * 1000)
                trace = _Trace(self, job, group)
                expected = {item["feedback_id"]: FeedbackResult.model_validate(item) for item in group["rows"]}
                received = {}

                def on_result(raw):
                    result = FeedbackResult.model_validate(raw.model_dump() if isinstance(raw, FeedbackResult) else raw)
                    original = expected.get(result.feedback_id)
                    if (original is None or result.source_row != original.source_row
                            or result.original_text != original.original_text or result.duplicate_of != original.duplicate_of):
                        raise ValueError("模型结果无法关联到本组原始评价。")
                    previous = received.get(result.feedback_id)
                    if previous is not None and previous.model_dump() != result.model_dump():
                        raise ValueError("同一评价不能提交不同的结果。")
                    received[result.feedback_id] = result

                trace.start()
                try:
                    if client is None:
                        client = self.client_factory()
                    results = self.analysis_runner(
                        list(expected.values()), client=client, max_workers=1, prompt_version="v3",
                        batch_size=job["config"].get("batch_size", 10),
                        max_characters=job["config"].get("max_characters", 12000),
                        request_control_factory=lambda _group_id: trace, result_callback=on_result,
                    )
                    for result in results:
                        on_result(result)
                    if not trace.sent:
                        self._safe_failure(job, group, uncertain=False)
                        if trace.error_code == "daily_budget_exceeded":
                            self.store.pause_job(job_id, "daily_budget_exhausted")
                        elif trace.error_code == "request_capacity_unavailable":
                            self.store.pause_job(job_id, "execution_paused")
                        break
                    if trace.transport_unknown:
                        self._safe_failure(job, group, uncertain=True)
                        emit_event("group_interrupted", job_id=job_id, group_id=group["id"],
                                   workspace_id=job.get("scope"), sent=trace.sent, status="uncertain")
                        break
                    if set(received) != set(expected) or any(
                        row.processing_status == ProcessingStatus.PENDING for row in received.values()
                    ):
                        raise ValueError("已发送请求没有完整的终态结果。")
                    failed = any(row.processing_status != ProcessingStatus.SUCCESS for row in received.values())
                    self.store.checkpoint_group(
                        job_id, group["id"], group["lease_token"],
                        results=[received[key].model_dump(mode="json") for key in expected],
                        usage=trace.usage, error="validated_group_failure" if failed else None,
                    )
                    emit_event("group_checkpoint", job_id=job_id, group_id=group["id"], workspace_id=job.get("scope"),
                               status="failed" if failed else "succeeded", total_tokens=trace.usage.get("total_tokens"),
                               latency_ms=(time.perf_counter() - group_started) * 1000)
                    if trace.stopped:
                        if trace.error_code == "provider_authorization_failed":
                            self.store.pause_job(job_id, "execution_paused")
                        break
                except BaseException:
                    self._safe_failure(job, group, uncertain=trace.sent)
                    logger.warning("analysis_group_interrupted job_id=%s group_id=%s sent=%s",
                                   job_id, group["id"], trace.sent)
                    emit_event("group_interrupted", job_id=job_id, group_id=group["id"],
                               workspace_id=job.get("scope"), sent=trace.sent, status="interrupted")
                    if not trace.sent:
                        self.store.pause_job(job_id, "execution_paused")
                    self._finalize(job_id)
                    raise
                finally:
                    trace.close()
                job = self.store.get_job(job_id)
            return self._finalize(job_id)
        finally:
            if client is not None:
                client.close()


def default_worker():
    from backend.database import database_enabled
    if database_enabled():
        from backend.tenancy import TenantJobWorker
        return TenantJobWorker()
    from backend.service import _root_directory
    root = _root_directory()
    return JobWorker(JobStore(root), history=HistoryStore(root), budget=ProductRequestBudget())
