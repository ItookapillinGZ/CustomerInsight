"""Explicit transport selection; compatible providers imply no capabilities."""

import hashlib
import json
from types import SimpleNamespace

from core.config import Config, ConfigurationError
from models.models import CustomerFeedbackAnalysis
from openai import OpenAI
from openai import OpenAI as SDKOpenAI


JSON_INSTRUCTION = "Return exactly one JSON object conforming to this schema, with no markdown or additional text: "

USAGE_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
    "input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details",
}
USAGE_DETAIL_FIELDS = {
    "cached_tokens", "cache_creation_tokens", "cache_read_tokens", "audio_tokens", "reasoning_tokens",
    "accepted_prediction_tokens", "rejected_prediction_tokens", "text_tokens", "image_tokens",
}


def disable_sdk_retries(client):
    """Apply zero SDK retries to injected clients as well as owned clients.

    An OpenAI client copy shares its transport and belongs to the original
    caller. Test adapters use the same object so their call records remain usable.
    """
    if isinstance(client, SDKOpenAI):
        return client.with_options(max_retries=0)
    client.max_retries = 0
    return client


def token_usage(response) -> dict | str:
    """Save actual public numeric usage fields; never guess absent usage or cost."""
    usage = getattr(response, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump(mode="json", exclude_none=True)
    if not isinstance(usage, dict):
        return "不可用"
    safe = {}
    for key, value in usage.items():
        if key not in USAGE_FIELDS:
            continue
        if type(value) is int and value >= 0:
            safe[key] = value
        elif isinstance(value, dict) and key.endswith("_details"):
            details = {name: count for name, count in value.items()
                       if name in USAGE_DETAIL_FIELDS and type(count) is int and count >= 0}
            if details:
                safe[key] = details
    return safe or "不可用"


def parse_responses_with_usage(client, parameters: dict, request_control=None, *, response_model=None):
    """One Responses call; preserve usage before the SDK's Pydantic parser.

    SDK parsing can raise without returning its response object. Its raw wrapper
    uses the same endpoint and request, and defers only local parsing. Adapter
    doubles retain their established parse interface.
    """
    if isinstance(client, SDKOpenAI):
        raw_response = client.responses.with_raw_response.parse(**parameters)
        usage = "不可用"
        payload = None
        try:
            payload = raw_response.http_response.json()
            if isinstance(payload, dict):
                usage = token_usage(SimpleNamespace(usage=payload.get("usage")))
        except (ValueError, TypeError):
            pass
        if request_control is not None:
            request_control.response_received(usage)
        if response_model is not None:
            # Keep the schema sent to the provider unchanged while optionally
            # parsing into a local envelope. Grouped analysis validates each
            # item separately so one bad label does not discard its neighbours.
            if not isinstance(payload, dict):
                raise ValueError("Responses 未返回可读取的 JSON 响应。")
            status = payload.get("status")
            if status != "completed":
                return SimpleNamespace(status=status, output_parsed=None, usage=payload.get("usage"))
            texts = []
            refused = False
            for item in payload.get("output", []):
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for content in item.get("content", []):
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") == "refusal":
                        refused = True
                    elif content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        texts.append(content["text"])
            parsed = response_model.model_validate_json("".join(texts)) if texts and not refused else None
            return SimpleNamespace(status=status, output_parsed=parsed, usage=payload.get("usage"))
        return raw_response.parse()
    response = client.responses.parse(**parameters)
    if request_control is not None:
        request_control.response_received(token_usage(response))
    return response


def get_openai_client() -> OpenAI:
    if not Config.OPENAI_API_KEY:
        raise ConfigurationError("OPENAI_API_KEY 未配置，请在 .env 或环境变量中设置。")
    return OpenAI(
        api_key=Config.OPENAI_API_KEY,
        base_url=Config.OPENAI_BASE_URL,
        timeout=Config.REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )


def request_settings() -> dict:
    """An allowlist for reports: no key, headers, or raw endpoint URL."""
    endpoint = Config.OPENAI_BASE_URL or "https://api.openai.com/v1"
    schema = json.dumps(CustomerFeedbackAnalysis.model_json_schema(), sort_keys=True, ensure_ascii=False)
    return {
        "provider": Config.LLM_PROVIDER,
        "model": Config.OPENAI_MODEL,
        "api_mode": Config.API_MODE,
        "endpoint_sha256": hashlib.sha256(endpoint.rstrip('/').encode()).hexdigest(),
        "timeout_seconds": Config.REQUEST_TIMEOUT_SECONDS,
        "max_output_tokens": Config.MAX_OUTPUT_TOKENS,
        "token_parameter": "max_output_tokens" if Config.API_MODE == "responses" else Config.CHAT_TOKEN_PARAMETER,
        "reasoning_effort": Config.OPENAI_REASONING_EFFORT,
        "output_mode": "json_schema" if Config.API_MODE == "responses" else Config.CHAT_OUTPUT_MODE,
        "output_schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
        "chat_json_instruction_sha256": hashlib.sha256(JSON_INSTRUCTION.encode()).hexdigest() if Config.API_MODE == "chat_completions" else None,
        "max_retries": Config.MAX_RETRIES,
        "retry_backoff_seconds": Config.RETRY_BACKOFF_SECONDS,
        "sdk_max_retries": 0,
        "store": False,
    }


def request_parameters(messages: list[dict], *, response_model=CustomerFeedbackAnalysis) -> dict:
    common = {"model": Config.OPENAI_MODEL, "timeout": Config.REQUEST_TIMEOUT_SECONDS, "store": False}
    if Config.API_MODE == "responses":
        common.update(input=messages, text_format=response_model, max_output_tokens=Config.MAX_OUTPUT_TOKENS)
        if Config.OPENAI_REASONING_EFFORT:
            common["reasoning"] = {"effort": Config.OPENAI_REASONING_EFFORT}
        return common
    if Config.API_MODE != "chat_completions":
        raise ConfigurationError("API_MODE 必须为 responses 或 chat_completions。")
    schema = response_model.model_json_schema()
    chat_messages = [dict(message) for message in messages]
    chat_messages[0]["content"] += "\n" + JSON_INSTRUCTION + json.dumps(schema, ensure_ascii=False)
    common.update(messages=chat_messages)
    common[Config.CHAT_TOKEN_PARAMETER] = Config.MAX_OUTPUT_TOKENS
    if Config.OPENAI_REASONING_EFFORT:
        common["reasoning_effort"] = Config.OPENAI_REASONING_EFFORT
    if Config.CHAT_OUTPUT_MODE == "json_schema":
        common["response_format"] = {"type": "json_schema", "json_schema": {"name": response_model.__name__, "strict": True, "schema": schema}}
    elif Config.CHAT_OUTPUT_MODE == "json_object":
        common["response_format"] = {"type": "json_object"}
    elif Config.CHAT_OUTPUT_MODE != "text":
        raise ConfigurationError("CHAT_OUTPUT_MODE 必须为 json_schema / json_object / text。")
    return common
