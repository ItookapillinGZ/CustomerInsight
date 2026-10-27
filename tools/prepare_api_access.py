"""Configure an independent API token without changing model credentials.

The token is written only to the local ignored .env, never to stdout. Existing
nonempty API tokens and every other parsed setting are preserved.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import secrets

from dotenv import dotenv_values


def prepare_api_access(project_dir: Path) -> bool:
    env_path = project_dir / ".env"
    if not env_path.is_file() or env_path.is_symlink():
        raise ValueError("An existing regular project .env is required.")
    raw = env_path.read_bytes()
    text = raw.decode("utf-8-sig")
    before = dotenv_values(stream=io.StringIO(text))
    if (before.get("APP_API_TOKEN") or "").strip():
        return False
    token = secrets.token_urlsafe(32)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, found = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"^[ \t]*(?:export[ \t]+)?APP_API_TOKEN[ \t]*=", line):
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append("APP_API_TOKEN=" + token + ending)
            found = True
        else:
            lines.append(line)
    updated = "".join(lines)
    if not found:
        if updated and not updated.endswith(("\r", "\n")):
            updated += newline
        updated += "APP_API_TOKEN=" + token + newline
    after = dotenv_values(stream=io.StringIO(updated))
    if any(after.get(key) != value for key, value in before.items() if key != "APP_API_TOKEN"):
        raise ValueError("Unrelated environment settings would change.")
    runtime = project_dir / ".runtime"
    if runtime.is_symlink():
        raise ValueError("Runtime directory must not be a symlink.")
    runtime.mkdir(exist_ok=True)
    backup = runtime / "env_before_backend_api.env"
    if not backup.exists():
        with backup.open("xb") as destination:
            destination.write(raw)
    temporary = env_path.with_name(".env.api-token.tmp")
    payload = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf-8")
    with temporary.open("xb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, env_path)
    return True


if __name__ == "__main__":
    created = prepare_api_access(Path(__file__).resolve().parents[1])
    print(json.dumps({"api_token_configured": True, "created": created,
                      "existing_model_settings_preserved": True}))
