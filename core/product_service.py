"""Single-instance customer app limits; no uploaded content is persisted."""

import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ai.grouped_batch import MAX_GROUP_CHARACTERS, MAX_GROUP_SIZE, analyze_grouped_batch, plan_review_groups
from core.analysis_agent import (
    DEFAULT_MAX_MODEL_CALLS, DEFAULT_MAX_TOOL_CALLS, MAX_HISTORY_TURNS, AnalysisAgentResult, estimate_agent_requests,
    prepare_agent_history, report_fingerprint, run_analysis_agent,
)
from core.config import Config
from models.enums import ProcessingStatus

_REQUEST_SLOTS = threading.BoundedSemaphore(2)
_AGENT_RUN_SLOT = threading.BoundedSemaphore(1)


def _shared_request_gate():
    """Queued deployments share upstream capacity with the background worker."""
    if os.getenv("APP_ANALYSIS_MODE", "direct").strip().lower() == "queued":
        from backend.dispatch import RedisSemaphore
        return RedisSemaphore()
    return None


class ProductLimitError(ValueError):
    pass


def _integer_setting(name, default, minimum=1, maximum=10000):
    try:
        value = int(os.getenv(name, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ProductLimitError(f"{name} 配置无效，请联系管理员。") from None


def service_access_password():
    return os.getenv("APP_ACCESS_PASSWORD", "").strip() or None


def requires_access_password():
    value = os.getenv("APP_PUBLIC_MODE", "false").strip().lower()
    if value not in {"true", "1", "yes", "false", "0", "no"}:
        raise ProductLimitError("APP_PUBLIC_MODE 配置无效，必须填写 true 或 false。")
    return value in {"true", "1", "yes"}


def _grouping_settings(batch_size=None, max_characters=None):
    size = _integer_setting("APP_LLM_BATCH_SIZE", 10, maximum=MAX_GROUP_SIZE) if batch_size is None else batch_size
    characters = (_integer_setting("APP_LLM_BATCH_MAX_CHARACTERS", 12000,
                                   minimum=Config.MAX_TEXT_LENGTH, maximum=MAX_GROUP_CHARACTERS)
                  if max_characters is None else max_characters)
    if type(size) is not int or not 1 <= size <= MAX_GROUP_SIZE:
        raise ProductLimitError(f"每组评价数量必须为1到{MAX_GROUP_SIZE}的整数。")
    if type(characters) is not int or not 1 <= characters <= MAX_GROUP_CHARACTERS:
        raise ProductLimitError(f"每组文本长度限制必须为1到{MAX_GROUP_CHARACTERS}的整数。")
    return size, characters


def estimate_customer_requests(rows, *, batch_size=None, max_characters=None, max_rows=None):
    size, characters = _grouping_settings(batch_size, max_characters)
    return len(plan_review_groups(rows, batch_size=size, max_characters=characters, max_rows=max_rows))


class CustomerAnalysisResults(list):
    """Compatible row results with per-run, session-owned operational metadata."""

    def __init__(self, rows, analysis_meta):
        super().__init__(rows)
        self.analysis_meta = dict(analysis_meta)


class ProductRequestBudget:
    """Atomically count each transport attempt before sending, including failures.

    SQLite stores only dates, random run IDs and counts, never feedback or keys.
    Deploy one process instance with a persistent volume for durable daily caps.
    """

    def __init__(self, path=None, daily_limit=None):
        self.path = Path(path or os.getenv("APP_USAGE_DB_PATH", ".runtime/product_usage.sqlite3"))
        self.daily_limit = daily_limit if daily_limit is not None else _integer_setting("APP_DAILY_REQUEST_LIMIT", 50)
        if type(self.daily_limit) is not int or self.daily_limit < 1:
            raise ProductLimitError("每日请求额度必须为正整数。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS attempts(day TEXT NOT NULL, run_id TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,run_id))")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=15, isolation_level=None)

    def _day(self):
        return datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    def remaining(self):
        with closing(self._connect()) as conn:
            used = conn.execute("SELECT COALESCE(SUM(count),0) FROM attempts WHERE day=?", (self._day(),)).fetchone()[0]
        return max(0, self.daily_limit - used)

    def reserve(self, run_id):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            day = self._day()
            used = conn.execute("SELECT COALESCE(SUM(count),0) FROM attempts WHERE day=?", (day,)).fetchone()[0]
            if used >= self.daily_limit:
                raise ProductLimitError("今日分析额度已用完，请稍后再试或联系管理员。")
            conn.execute("INSERT INTO attempts(day,run_id,count) VALUES(?,?,1) ON CONFLICT(day,run_id) DO UPDATE SET count=count+1", (day, run_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class ProductRunControl:
    def __init__(self, budget, max_attempts, run_id=None):
        self.budget, self.max_attempts = budget, max_attempts
        self.run_id = run_id or uuid4().hex
        self.attempts = 0
        self.usage = {}
        self.stopped = False
        self.lock = threading.Lock()

    def trace(self, row_id):
        return ProductTrace(self, row_id)


class ProductTrace:
    def __init__(self, control, row_id):
        self.control, self.row_id = control, row_id
        self.attempts = 0
        self.token_usage = []
        self._slot_acquired = False
        self._distributed_gate = None
        self._distributed_token = None

    @property
    def stopped(self):
        return self.control.stopped

    def before_attempt(self):
        try:
            if not _REQUEST_SLOTS.acquire(timeout=max(1, Config.REQUEST_TIMEOUT_SECONDS)):
                raise ProductLimitError("服务正忙，请稍后再试。")
            self._slot_acquired = True
            # Start the Redis lease after local waiting, so its lifetime covers
            # the provider timeout rather than waiting + provider timeout.
            self._distributed_gate = _shared_request_gate()
            if self._distributed_gate is not None:
                self._distributed_token = self._distributed_gate.acquire()
            with self.control.lock:
                if self.control.stopped or self.control.attempts >= self.control.max_attempts:
                    raise ProductLimitError("本次分析已达到请求上限。")
                self.control.budget.reserve(self.control.run_id)
                self.control.attempts += 1
                self.attempts += 1
        except BaseException:
            self._release_slot()
            raise

    def _release_slot(self):
        if self._slot_acquired:
            self._slot_acquired = False
            _REQUEST_SLOTS.release()
        gate, token = self._distributed_gate, self._distributed_token
        self._distributed_gate = self._distributed_token = None
        if gate is not None:
            gate.release(token)

    def response_received(self, usage):
        self._release_slot()
        self.token_usage.append(usage)
        with self.control.lock:
            self.control.usage[self.row_id] = list(self.token_usage)

    def request_failed(self, error):
        try:
            if getattr(error, "status_code", None) in {401, 403}:
                with self.control.lock:
                    self.control.stopped = True
        finally:
            self._release_slot()


def get_service_status():
    budget = ProductRequestBudget()
    size, characters = _grouping_settings()
    return {"configured": bool(Config.OPENAI_API_KEY), "remaining_today": budget.remaining(),
            "daily_limit": budget.daily_limit, "request_batch_size": size,
            "max_batch_characters": characters,
            "agent_requests_per_run": estimate_agent_requests()}


def run_customer_report_agent(report: dict, question: str, *, budget=None, client=None,
                              control=None, history=None, progress_callback=None) -> AnalysisAgentResult:
    """Run an adaptive report agent under a conservative per-turn daily cap."""
    if requires_access_password() and not service_access_password():
        raise ProductLimitError("服务尚未开放，请联系管理员配置访问码。")
    if not isinstance(report, dict) or not isinstance(report.get("metrics"), dict):
        raise ProductLimitError("本次分析报告无效，请重新上传并分析。")
    # Snapshot and validate history before reserving capacity or sending a call.
    # A follow-up never reads conversation state from another browser session.
    conversation = None
    if history is not None:
        if not isinstance(history, list):
            raise ProductLimitError("对话历史无效，请清空对话后重试。")
        try:
            conversation = [
                AnalysisAgentResult.model_validate(item.model_dump(mode="json") if isinstance(item, AnalysisAgentResult) else item)
                for item in history[-MAX_HISTORY_TURNS:]
            ]
            prepare_agent_history(conversation, report_fingerprint(report))
        except (ValueError, TypeError):
            raise ProductLimitError("对话不属于当前报告或已失效，请清空对话后重试。") from None
    # No valid rows is a local fallback and must not reserve quota or create a
    # client. The UI normally disables this case; the service remains fail-safe.
    if not report["metrics"].get("success_rows"):
        return run_analysis_agent(report, question, client=client, history=conversation,
                                  progress_callback=progress_callback)
    needed = estimate_agent_requests()
    if control is not None:
        if not isinstance(control, ProductRunControl):
            raise ProductLimitError("经营建议请求控制器无效。")
        if control.attempts != 0 or control.max_attempts != needed or control.stopped or control.usage:
            raise ProductLimitError("经营建议请求控制器必须是未使用的本轮请求控制器。")
        if budget is not None and control.budget is not budget:
            raise ProductLimitError("经营建议请求控制器与额度账本不一致。")
        budget = control.budget
    budget = budget or ProductRequestBudget()
    if not _AGENT_RUN_SLOT.acquire(timeout=max(1, Config.REQUEST_TIMEOUT_SECONDS * needed)):
        raise ProductLimitError("经营建议服务正忙，请稍后再试。")
    try:
        # Serialize a complete turn so competing sessions cannot both start on
        # the same final units and strand each other midway through a tool loop.
        available = budget.remaining()
        if available < needed:
            raise ProductLimitError(f"经营建议最多需要 {needed} 次请求，今日剩余 {available} 次，请稍后再试或联系管理员。")
        control = control or ProductRunControl(budget, DEFAULT_MAX_MODEL_CALLS)
        result = run_analysis_agent(
            report, question, client=client, control=control,
            history=conversation,
            progress_callback=progress_callback,
            max_model_calls=DEFAULT_MAX_MODEL_CALLS, max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
        )
        # The local result and transport ledger must agree. A mismatch could hide a
        # charged request, so return the deterministic fallback rather than expose
        # misleading operational metadata.
        if result.model_calls != control.attempts:
            return result.model_copy(update={
                "status": "fallback", "fallback_reason": "RequestCountMismatch",
                "model_calls": control.attempts,
            })
        return result
    finally:
        _AGENT_RUN_SLOT.release()


def analyze_customer_rows(rows, progress_callback=None, *, budget=None, max_attempts=None,
                          client=None, control=None, result_callback=None,
                          batch_size=None, max_characters=None, run_state=None):
    """Group independent reviews using v3 rules, with one budget unit per request.

    The original one-review analyzer and frozen experiments remain unchanged.
    A group never retries or falls back to additional single-review requests.
    """
    if requires_access_password() and not service_access_password():
        raise ProductLimitError("服务尚未开放，请联系管理员配置访问码。")
    size, characters = _grouping_settings(batch_size, max_characters)
    pending = sum(row.processing_status == ProcessingStatus.PENDING for row in rows)
    needed = estimate_customer_requests(rows, batch_size=size, max_characters=characters)
    budget = budget or ProductRequestBudget()
    available = budget.remaining()
    if needed > available:
        raise ProductLimitError(f"本次 {pending} 条有效评价预计合并为 {needed} 次请求，今日剩余 {available} 次，请拆分表格或联系管理员。")
    if max_attempts is not None and (type(max_attempts) is not int or max_attempts < 0):
        raise ProductLimitError("真实请求上限必须为非负整数。")
    if max_attempts is not None and needed > max_attempts:
        raise ProductLimitError("预计合并请求次数超过本次授权的真实请求上限。")
    control = control or ProductRunControl(budget, max_attempts if max_attempts is not None else max(needed, 1))
    if run_state is not None and not isinstance(run_state, dict):
        raise ProductLimitError("本次运行状态必须为独立的状态对象。")
    meta = {"mode": "grouped", "prompt_version": "v3_grouped_aspects_v2",
            "planned_requests": needed, "batch_size": size, "max_characters": characters,
            "unique_pending_rows": pending, "run_status": "running"}
    if run_state is not None:
        run_state["analysis_meta"] = meta
    started = time.perf_counter()
    finished = False
    try:
        results = analyze_grouped_batch(rows, client=client, max_workers=2, progress_callback=progress_callback,
                                        prompt_version="v3", request_control_factory=control.trace,
                                        result_callback=result_callback, batch_size=size, max_characters=characters)
        finished = True
    finally:
        usage_entries = [entry for entries in control.usage.values() for entry in entries
                         if isinstance(entry, dict) and type(entry.get("total_tokens")) is int]
        meta.update(actual_requests=control.attempts, elapsed_seconds=round(time.perf_counter() - started, 3),
                    total_tokens=sum(entry["total_tokens"] for entry in usage_entries) if usage_entries else None,
                    usage_reported_requests=len(usage_entries), run_status="finished" if finished else "interrupted")
    return CustomerAnalysisResults(results, meta)
