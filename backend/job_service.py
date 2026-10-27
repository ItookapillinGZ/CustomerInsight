"""HTTP orchestration for durable analysis jobs; model transport is worker-only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path

from ai.grouped_batch import plan_review_groups
from core.config import Config
from core.csv_io import prepare_feedback_rows
from core.excel_io import prepare_excel_feedback_rows
from core.history_store import _pack
from core.product_service import _grouping_settings

from backend.job_store import JobStore
from backend.service import ApiError, identifier


MAX_JOB_ROWS = 1000
LOGGER = logging.getLogger("customerinsight.jobs")


def execution_config(frame):
    size, characters = _grouping_settings()
    return {
        "model": Config.OPENAI_MODEL, "api_mode": Config.API_MODE,
        "chat_output_mode": Config.CHAT_OUTPUT_MODE,
        "chat_token_parameter": Config.CHAT_TOKEN_PARAMETER,
        "max_output_tokens": Config.MAX_OUTPUT_TOKENS,
        "reasoning_effort": Config.OPENAI_REASONING_EFFORT,
        "timeout_seconds": Config.REQUEST_TIMEOUT_SECONDS,
        "endpoint_hash": hashlib.sha256((Config.OPENAI_BASE_URL or "default").encode()).hexdigest(),
        "prompt_version": "v3_grouped_aspects_v2", "batch_size": size,
        "max_characters": characters, "frame_snapshot": _pack(frame),
    }


def public_job(job):
    fields = ("id", "status", "filename", "total_rows", "completed_rows", "success_rows",
              "total_groups", "completed_groups", "failed_groups", "uncertain_groups",
              "max_requests", "batch_id", "error_code", "can_resume", "cancel_requested")
    result = {key: job[key] for key in fields}
    result["request_count"] = job["request_count"]
    result["cached"] = False
    for key in ("created_at", "updated_at"):
        result[key] = datetime.fromtimestamp(job[key], timezone.utc).isoformat()
    return result


class JobService:
    def __init__(self, workspace, *, store=None, dispatcher=None):
        self.workspace, self._store = workspace, store
        self.dispatcher = dispatcher

    @property
    def store(self):
        if self._store is None:
            self._store = JobStore(self.workspace.root_dir)
        return self._store

    def _dispatch(self):
        # Creation and outbox were committed together. Redis unavailability
        # never turns a committed job into a lost upload or an HTTP retry trap.
        try:
            if self.dispatcher is not None:
                self.dispatcher(self.store)
            else:
                from backend.dispatch import dispatch_pending
                dispatch_pending(store=self.store)
        except Exception as error:
            LOGGER.warning("outbox_dispatch_deferred error_type=%s", type(error).__name__)

    def create(self, request):
        manifest, data = self.workspace._upload(request.upload_id)
        frame = self.workspace._frame(manifest, data, request.sheet_name, request.encoding)
        selected = [column for column in (request.text_column, request.group_column, request.date_column) if column is not None]
        if len(set(selected)) != len(selected) or any(column not in frame.columns for column in selected):
            raise ApiError(422, "invalid_columns", "请为评价、分组和日期选择不同且存在的字段。")
        excel = Path(manifest["filename"]).suffix.lower() == ".xlsx"
        mapping = {"sheet_name": frame.attrs.get("sheet_name"), "encoding": None if excel else request.encoding,
                   "text_column": request.text_column, "group_column": request.group_column,
                   "date_column": request.date_column}
        existing = self.workspace.history.find_batch(manifest["content_hash"], mapping)
        if existing and not request.force:
            record = self.workspace.history.load_batch(existing["id"])
            metrics = record["report"]["metrics"]
            complete = (not metrics.get("pending_rows", 0) and not metrics.get("failed_rows", 0)
                        and metrics.get("success_rows", 0) == metrics.get("total_rows", 0))
            return {
                "id": None, "status": "completed" if complete else "partial", "cached": True,
                "batch_id": existing["id"], "filename": record["filename"],
                "total_rows": metrics["total_rows"], "completed_rows": metrics["total_rows"] - metrics.get("pending_rows", 0),
                "success_rows": metrics["success_rows"], "total_groups": 0, "completed_groups": 0,
                "failed_groups": 0, "uncertain_groups": 0, "request_count": 0, "max_requests": 0,
                "can_resume": False, "cancel_requested": False, "error_code": None,
                "created_at": record["created_at"], "updated_at": record["updated_at"],
            }, False
        prepare = prepare_excel_feedback_rows if excel else prepare_feedback_rows
        rows = prepare(frame, request.text_column, max_rows=MAX_JOB_ROWS)
        config = execution_config(frame)
        groups = plan_review_groups(rows, batch_size=config["batch_size"],
                                    max_characters=config["max_characters"], max_rows=MAX_JOB_ROWS)
        self.workspace._consent(request.allow_model_call, request.max_requests, len(groups))
        if not groups:
            raise ApiError(422, "no_valid_reviews", "没有可分析的评价，请检查文本列。")
        # A queued job does not reserve a whole day's allowance. Workers reserve
        # each transport atomically; multiple accepted jobs may wait for quota.
        job, created = self.store.create_job(
            original=data, filename=manifest["filename"], request=mapping, config=config,
            groups=[[row.model_dump(mode="json") for row in group] for group in groups],
            initial_results=[row.model_dump(mode="json") for row in rows],
            max_requests=request.max_requests, idempotency_key=request.idempotency_key,
            force=request.force,
            **({"scope": self.store.scope} if getattr(self.store, "scope", None) else {}),
        )
        self._dispatch()
        return public_job(self.store.get_job(job["id"])), created

    def get(self, job_id):
        self.store.expire_leases(identifier(job_id))
        return public_job(self.store.get_job(job_id))

    def list(self, *, limit, offset):
        self.store.expire_leases()
        page = self.store.list_jobs(limit=limit, offset=offset)
        return {**page, "items": [public_job(job) for job in page["items"]]}

    def cancel(self, job_id):
        result = self.store.cancel_job(identifier(job_id))
        self._dispatch()
        return public_job(result)

    def resume(self, job_id, request):
        current = self.store.get_job(identifier(job_id))
        if not current["can_resume"]:
            raise ApiError(409, "job_not_resumable", "此任务没有可安全恢复的分组，请先核对任务状态。")
        remaining = sum(group["status"] in {"pending", "cancelled"} and not group["attempt_count"] for group in current["groups"])
        self.workspace._consent(request.allow_model_call, request.max_requests,
                                current["request_count"] + remaining)
        self.store.resume_job(job_id, max_requests=request.max_requests)
        self._dispatch()
        return public_job(self.store.get_job(job_id))
