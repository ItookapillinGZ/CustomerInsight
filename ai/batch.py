"""A small thread pool around the existing synchronous analyzer."""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import time

from openai import (
    APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, OpenAI,
)
from pydantic import ValidationError

from ai.analyzer import StructuredOutputError, analyze_feedback
from ai.client import get_openai_client
from ai.prompts import get_prompt
from core.config import Config, ConfigurationError
from models.enums import ProcessingStatus
from models.models import CustomerFeedbackAnalysis, FeedbackResult

logger = logging.getLogger(__name__)


def describe_error(error: Exception) -> str:
    """Actionable errors without leaking keys, request bodies or raw feedback."""
    if isinstance(error, ValidationError):
        fields = ", ".join(
            ".".join(str(part) for part in item["loc"])
            for item in error.errors(include_input=False, include_url=False)[:5]
        )
        return f"结构化输出校验失败：{fields or '响应格式'}。"
    if isinstance(error, StructuredOutputError):
        return str(error)
    if isinstance(error, AuthenticationError):
        return "API 身份验证失败，请检查 OPENAI_API_KEY。"
    if isinstance(error, APITimeoutError):
        return "LLM 请求超时，未完成。"
    if isinstance(error, APIConnectionError):
        return "无法连接 LLM 服务，请检查网络或代理配置。"
    if isinstance(error, APIStatusError):
        return f"LLM 服务返回 HTTP {error.status_code}，请检查服务权限、模型或配额。"
    if isinstance(error, ConfigurationError):
        return str(error)
    if isinstance(error, ValueError):
        return "输入或输出无效，请检查配置和数据格式。"
    return f"处理失败（{type(error).__name__}），请检查运行日志。"


def _analyze_row(row: FeedbackResult, client: OpenAI, prompt_version: str, request_control=None) -> FeedbackResult:
    started = time.perf_counter()
    payload = row.model_dump(mode="python")
    try:
        options = {"client": client, "prompt_version": prompt_version}
        if request_control is not None:
            options.update(request_control=request_control, max_retries=0)
        analysis = analyze_feedback(row.original_text, **options)
        # Validate the boundary even when another integration injects an analyzer.
        analysis = CustomerFeedbackAnalysis.model_validate(analysis.model_dump())
        payload.update(
            sentiment=analysis.sentiment,
            category=analysis.category,
            priority=analysis.priority,
            summary=analysis.summary,
            processing_status=ProcessingStatus.SUCCESS,
            error_message=None,
        )
    except Exception as error:
        payload.update(
            processing_status=(
                ProcessingStatus.VALIDATION_ERROR
                if isinstance(error, (ValidationError, StructuredOutputError))
                else ProcessingStatus.FAILED
            ),
            error_message=describe_error(error),
        )
        logger.warning(
            "feedback_id=%s status=%s error_type=%s",
            row.feedback_id, payload["processing_status"].value, type(error).__name__,
        )
    payload["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result = FeedbackResult.model_validate(payload)
    logger.info(
        "feedback_id=%s status=%s latency_ms=%.2f",
        result.feedback_id, result.processing_status.value, result.latency_ms,
    )
    return result


def analyze_batch(
    rows: Sequence[FeedbackResult],
    *,
    client: OpenAI | None = None,
    max_workers: int = Config.DEFAULT_WORKERS,
    progress_callback: Callable[[int, int], None] | None = None,
    prompt_version: str = "v1",
    request_control_factory: Callable[[str], object] | None = None,
    result_callback: Callable[[FeedbackResult], None] | None = None,
) -> list[FeedbackResult]:
    """Isolate row errors, preserve order, and call progress on the main thread."""
    get_prompt(prompt_version)
    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        raise ValueError("并发数必须是整数。")
    if not 1 <= max_workers <= Config.MAX_WORKERS:
        raise ValueError(f"并发数必须在 1 到 {Config.MAX_WORKERS} 之间。")
    if len(rows) > Config.MAX_BATCH_ROWS:
        raise ValueError(f"每批最多处理 {Config.MAX_BATCH_ROWS} 行。")
    results = [FeedbackResult.model_validate(row.model_dump()) for row in rows]
    if len({row.feedback_id for row in results}) != len(results):
        raise ValueError("feedback_id 必须唯一。")
    pending_indices = [
        index for index, row in enumerate(results)
        if row.processing_status == ProcessingStatus.PENDING
    ]
    total = len(results)
    completed = total - len(pending_indices)
    if progress_callback:
        progress_callback(completed, total)
    if not pending_indices:
        return results

    owns_client = client is None
    active_client = get_openai_client() if owns_client else client
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_analyze_row, results[index], active_client, prompt_version,
                                request_control_factory(results[index].feedback_id) if request_control_factory else None): index
                for index in pending_indices
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
                if result_callback:
                    result_callback(results[futures[future]])
                completed += 1
                if progress_callback:
                    progress_callback(completed, total)
    finally:
        if owns_client:
            active_client.close()
    return results
