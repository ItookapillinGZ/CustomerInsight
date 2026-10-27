"""Verify the running API against existing reports without permitting model calls.

Only an existing original is staged again. Analyses are requested with
allow_model_call=False, so any cache miss must fail closed rather than consume
the provider budget. No report, original, or conversation is rewritten.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
from uuid import uuid4

from dotenv import dotenv_values
import requests


def verify(base_url: str, project_dir: Path, *, queued=False, token=None, baseline_path=None, output_path=None) -> dict:
    if token is None:
        settings = dotenv_values(project_dir / ".env")
        token = (settings.get("APP_API_TOKEN") or "").strip()
    if not token:
        raise ValueError("API token must already be configured.")
    baseline = json.loads((baseline_path or project_dir / ".runtime/backend_history_baseline.json").read_text(encoding="utf-8-sig"))
    expected = {record["id"]: record for record in baseline["reports"]}
    session = requests.Session()
    session.trust_env = False

    def request(method, suffix, *, authorized=True, **kwargs):
        headers = kwargs.pop("headers", {})
        if authorized:
            headers["Authorization"] = "Bearer " + token
        response = session.request(method, base_url + suffix, headers=headers, timeout=30, **kwargs)
        # Report only protocol information on failure; never include payloads,
        # request headers, customer rows, private filenames or credentials.
        if response.status_code >= 400:
            if not authorized and response.status_code == 401:
                return response
            raise AssertionError(f"API verification failed: {method} {suffix.split('?')[0]} status={response.status_code}")
        if not response.headers.get("X-Request-ID"):
            raise AssertionError("Missing request identifier")
        return response

    assert request("GET", "/healthz", authorized=False).json() == {"status": "ok"}
    assert request("GET", "/api/v1/batches", authorized=False).status_code == 401
    schema = request("GET", "/openapi.json", authorized=False).json()
    assert schema["components"]["securitySchemes"]
    listing = request("GET", "/api/v1/batches?limit=100").json()
    assert listing["total"] == len(expected)
    assert {item["id"] for item in listing["items"]} == set(expected)
    total, covered, cache_hits, downloaded, chats = 0, 0, 0, 0, 0
    for metadata in listing["items"]:
        batch_id = metadata["id"]
        previous = expected[batch_id]
        detail = request("GET", f"/api/v1/batches/{batch_id}").json()
        metrics = detail["report"]["metrics"]
        assert metrics["total_rows"] == previous["total_rows"]
        assert metrics["success_rows"] == previous["success_rows"]
        assert "source_frame" not in detail["report"]
        total += metrics["total_rows"]
        covered += metrics["success_rows"]
        rows = request("GET", f"/api/v1/batches/{batch_id}/rows?limit=100").json()
        assert rows["total"] == metrics["total_rows"]
        assert len(rows["items"]) == metrics["total_rows"]
        original = request("GET", f"/api/v1/batches/{batch_id}/exports/original").content
        assert hashlib.sha256(original).hexdigest() == previous["content_hash"]
        for kind in ("csv", "html"):
            exported = request("GET", f"/api/v1/batches/{batch_id}/exports/{kind}")
            assert exported.content
            assert exported.headers.get("X-Content-Type-Options") == "nosniff"
            downloaded += 1
        chat = request("GET", f"/api/v1/batches/{batch_id}/chat").json()
        assert isinstance(chat["items"], list)
        chats += 1
        staged = request("POST", "/api/v1/uploads", files={
            "file": (metadata["filename"], io.BytesIO(original), "application/octet-stream"),
        }, data={"purpose": "job" if queued else "analysis"}).json()
        mapping = metadata["mapping"]
        options = {key: value for key, value in mapping.items() if value is not None}
        request("GET", f"/api/v1/uploads/{staged['upload_id']}/preview", params={
            key: value for key, value in options.items() if key in ("sheet_name", "encoding")
        })
        reuse = request("POST", "/api/v1/jobs" if queued else "/api/v1/analyses", json={
            **options, "upload_id": staged["upload_id"], "allow_model_call": False,
            **({"idempotency_key": uuid4().hex} if queued else {}),
        }).json()
        assert reuse["cached"] is True and reuse["batch_id"] == batch_id
        if queued:
            assert reuse["id"] is None and reuse["request_count"] == 0
        cache_hits += 1
    result = {"history_batches": len(expected), "total_rows": total, "success_rows": covered,
              "cached_existing_analyses": cache_hits, "report_downloads": downloaded,
              "chat_histories_read": chats, "real_model_calls": 0,
              "authorization_checked": True, "original_hashes_unchanged": True}
    destination = ".runtime/phase2/existing_history_acceptance.json" if queued else ".runtime/backend_api_acceptance.json"
    (output_path or project_dir / destination).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[1]
    urls = [value for value in sys.argv[1:] if value != "--jobs"]
    print(json.dumps(verify(urls[0] if urls else "http://127.0.0.1:8000", project, queued="--jobs" in sys.argv)))
