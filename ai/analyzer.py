"""Reusable synchronous analysis with bounded retries and output validation."""

import logging
import time

from openai import APIConnectionError, APIStatusError, OpenAI

from ai.client import disable_sdk_retries, get_openai_client, parse_responses_with_usage, request_parameters, token_usage
from ai.prompts import SYSTEM_INSTRUCTION, get_prompt
from core.config import Config
from models.models import CustomerFeedbackAnalysis

logger = logging.getLogger(__name__)


class StructuredOutputError(ValueError):
    """The provider did not return a complete, usable structured response."""


def _is_retryable(error: Exception) -> bool:
    return isinstance(error, APIConnectionError) or (
        isinstance(error, APIStatusError)
        and (error.status_code in {408, 409, 429} or error.status_code >= 500)
    )


def analyze_feedback(
    feedback_text: str, *, client: OpenAI | None = None,
    prompt_version: str = "v1", max_retries: int | None = None, request_control=None,
) -> CustomerFeedbackAnalysis:
    """Preserve single-argument usage; batch callers inject one shared client.

    Validation errors, refusals and permanent API errors are not retried.
    A supplied client belongs to the caller and is never closed here.
    """
    prompt = get_prompt(prompt_version)  # Reject unknown versions before constructing a client.
    retries = Config.MAX_RETRIES if max_retries is None else max_retries
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("重试次数必须是非负整数。")
    if not isinstance(feedback_text, str) or not feedback_text.strip():
        raise ValueError("反馈文本不能为空，且必须是字符串。")
    if len(feedback_text) > Config.MAX_TEXT_LENGTH:
        raise ValueError(f"单条反馈不能超过 {Config.MAX_TEXT_LENGTH} 个字符。")

    owns_client = client is None
    original_client = get_openai_client() if owns_client else client
    active_client = disable_sdk_retries(original_client)
    last_error = None
    try:
        for attempt in range(retries + 1):
            attempt_sent = False
            try:
                parameters = request_parameters([
                        {
                            "role": "system",
                            "content": SYSTEM_INSTRUCTION,
                        },
                        {
                            "role": "user",
                            "content": prompt.format(
                                feedback_text=feedback_text.strip()
                            ),
                        },
                    ])
                # Reserve immediately before entering the SDK; even failed
                # transports and application retries consume one attempt.
                if request_control is not None:
                    try:
                        request_control.before_attempt()
                    except Exception:
                        if last_error is not None:
                            raise last_error from None
                        raise
                attempt_sent = True
                if Config.API_MODE == "responses":
                    response = parse_responses_with_usage(active_client, parameters, request_control)
                    if getattr(response, "status", "completed") != "completed":
                        raise StructuredOutputError("模型响应未完成，可能达到输出长度限制。")
                    parsed = getattr(response, "output_parsed", None)
                    if parsed is None:
                        raise StructuredOutputError("模型未返回结构化结果，可能拒绝了请求。")
                else:
                    response = active_client.chat.completions.create(**parameters)
                    if request_control is not None:
                        request_control.response_received(token_usage(response))
                    choices = getattr(response, "choices", [])
                    if not choices or getattr(choices[0], "finish_reason", None) != "stop":
                        raise StructuredOutputError("Chat 响应未正常完成，可能被截断、过滤或没有结果。")
                    message = choices[0].message
                    if getattr(message, "refusal", None):
                        raise StructuredOutputError("模型拒绝了请求，未返回有效结构化结果。")
                    content = getattr(message, "content", None)
                    if not isinstance(content, str) or not content.strip():
                        raise StructuredOutputError("Chat 响应没有完整的 JSON 文本。")
                    # No repair, guessing, markdown stripping, or default labels.
                    return CustomerFeedbackAnalysis.model_validate_json(content)
                # Revalidate SDK objects as well as raw dictionaries. This also
                # detects objects created via Pydantic model_construct().
                if isinstance(parsed, CustomerFeedbackAnalysis):
                    parsed = parsed.model_dump(mode="python", warnings=False)
                return CustomerFeedbackAnalysis.model_validate(parsed)
            except Exception as error:
                if request_control is not None and attempt_sent:
                    request_control.request_failed(error)
                last_error = error
                if (attempt >= retries or not _is_retryable(error)
                        or (request_control is not None and request_control.stopped)):
                    raise
                delay = Config.RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Transient LLM error type=%s retry=%d delay_seconds=%.1f",
                    type(error).__name__, attempt + 1, delay,
                )
                time.sleep(delay)
        raise RuntimeError("Unreachable retry state")
    finally:
        if owns_client:
            original_client.close()
