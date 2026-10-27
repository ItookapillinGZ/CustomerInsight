"""Validated environment settings; errors never echo configured values."""

import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# A filled project .env is the user's explicit local configuration. It must not
# silently inherit a different OPENAI_API_KEY from the desktop host process.
# When no .env exists, normal process environment settings still apply.
def load_project_env(path: str | Path = Path(__file__).resolve().parents[1] / ".env") -> bool:
    return load_dotenv(path, override=True)


load_project_env()


class ConfigurationError(ValueError):
    """A safe, actionable configuration error."""


def load_settings(env=None) -> dict:
    env = os.environ if env is None else env

    def choice(name, default, allowed):
        value = env.get(name, default).strip().lower()
        if value not in allowed:
            raise ConfigurationError(f"{name} 必须为 {' / '.join(sorted(allowed))}。")
        return value

    def number(name, default, *, integer=False, minimum=0):
        try:
            raw = env.get(name, str(default))
            value = int(raw) if integer else float(raw)
            if not math.isfinite(value) or value < minimum:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            kind = "整数" if integer else "有限数值"
            raise ConfigurationError(f"{name} 必须为不小于 {minimum} 的{kind}。") from None
        return value

    base_url = env.get("OPENAI_BASE_URL", "").strip() or None
    if base_url:
        try:
            url = urlsplit(base_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError()
            _ = url.port
        except ValueError:
            raise ConfigurationError("OPENAI_BASE_URL 必须是有效的 HTTP(S) 地址，不得含凭据、查询或片段。") from None
    model = env.get("OPENAI_MODEL", "gpt-4o-2024-08-06").strip()
    if not model:
        raise ConfigurationError("OPENAI_MODEL 不能为空。")
    reasoning = env.get("OPENAI_REASONING_EFFORT", "").strip().lower() or None
    if reasoning not in {None, "none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ConfigurationError("OPENAI_REASONING_EFFORT 必须为空或 none/minimal/low/medium/high/xhigh；实际支持由服务决定。")
    return {
        "LLM_PROVIDER": choice("LLM_PROVIDER", "openai", {"openai", "openai_compatible", "openai-compatible"}),
        "OPENAI_API_KEY": env.get("OPENAI_API_KEY") or None,
        "OPENAI_MODEL": model,
        "OPENAI_BASE_URL": base_url,
        "API_MODE": choice("API_MODE", "responses", {"responses", "chat_completions"}),
        "CHAT_OUTPUT_MODE": choice("CHAT_OUTPUT_MODE", "json_schema", {"json_schema", "json_object", "text"}),
        "CHAT_TOKEN_PARAMETER": choice("CHAT_TOKEN_PARAMETER", "max_completion_tokens", {"max_completion_tokens", "max_tokens"}),
        "OPENAI_REASONING_EFFORT": reasoning,
        "REQUEST_TIMEOUT_SECONDS": number("OPENAI_TIMEOUT_SECONDS", 30, minimum=0.001),
        "MAX_OUTPUT_TOKENS": number("OPENAI_MAX_OUTPUT_TOKENS", 1600, integer=True, minimum=1),
        "MAX_RETRIES": number("LLM_MAX_RETRIES", 2, integer=True, minimum=0),
        "RETRY_BACKOFF_SECONDS": number("LLM_RETRY_BACKOFF_SECONDS", 1, minimum=0),
    }


class Config:
    # Existing attribute names remain compatible with phase-one callers/tests.
    locals().update(load_settings())
    MAX_TEXT_LENGTH = 8000
    MAX_CSV_BYTES = 5 * 1024 * 1024
    MAX_BATCH_ROWS = 100
    DEFAULT_WORKERS = 3
    MAX_WORKERS = 5
