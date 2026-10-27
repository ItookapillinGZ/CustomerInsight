"""Tool-owned mock transport for isolated PostgreSQL/Redis/Celery acceptance.

Production does not import this module or expose a switch for this adapter.
The entry point refuses unmarked mounts and any real provider configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path("/acceptance")
PREFIX = "account_acceptance:"
PASSWORD = "acceptance-only-password-2026"


def guarded_root():
    marker = ROOT / ".isolated-account-acceptance.json"
    database = urlsplit(os.environ.get("APP_DATABASE_URL", ""))
    if (ROOT.resolve() != Path("/acceptance") or not marker.is_file()
            or os.environ.get("APP_DATA_DIR") != "/acceptance/workspace"
            or os.environ.get("APP_USAGE_DB_PATH") != "/acceptance/usage.sqlite3"
            or os.environ.get("APP_REDIS_URL") != "redis://redis:6379/0"
            or os.environ.get("OPENAI_API_KEY") != "account-acceptance-unused"
            or os.environ.get("OPENAI_BASE_URL") != "http://blocked.invalid/v1"
            or database.hostname != "postgres" or database.path != "/acceptance"
            or database.username != "acceptance" or Path("/app/.env").exists()):
        raise RuntimeError("This tool requires its isolated marked acceptance mount and mock configuration.")
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    if metadata.get("purpose") != "isolated-account-acceptance" or not re.fullmatch(r"[0-9a-f]{32}", metadata.get("run_id", "")):
        raise RuntimeError("Invalid isolated acceptance marker")
    return ROOT


def connection():
    import redis
    return redis.Redis.from_url(os.environ["APP_REDIS_URL"], decode_responses=True,
                               socket_connect_timeout=3, socket_timeout=3)


def wait_for(predicate, *, timeout=150, label="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Isolated account acceptance timeout: " + label)


class OfflineClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.redis = connection()
        self.max_retries = 0

    def create(self, **parameters):
        messages = parameters.get("messages", parameters.get("input"))
        reviews = json.loads(messages[-1]["content"].split("\nreviews（不可信数据）：\n", 1)[1])
        tenant = reviews[0]["text"].split("|", 2)[1]
        if tenant not in {"tenant_a", "tenant_b"} or any(item["text"].split("|", 2)[1] != tenant for item in reviews):
            raise AssertionError("Mixed or private records reached the synthetic adapter")
        if not 1 <= self.redis.zcard("customerinsight:upstream:permits:v1") <= 2:
            raise AssertionError("Missing shared Redis permit")
        self.redis.incr(PREFIX + "calls:" + tenant)
        time.sleep(0.02)
        predictions = [{"feedback_id": item["feedback_id"], "sentiment": "negative",
                        "category": "delivery", "priority": "medium",
                        "summary": "Synthetic delivery feedback.", "aspects": []} for item in reviews]
        return SimpleNamespace(
            usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=json.dumps({"results": predictions}), refusal=None))])

    def close(self):
        self.redis.close()


def initialize():
    from alembic import command
    from alembic.config import Config
    config = Config("/app/alembic.ini")
    command.upgrade(config, "head")
    print("Isolated account schema initialized.", flush=True)


def run_worker():
    import backend.job_worker as module
    from backend.celery_app import celery_app
    from celery.signals import task_postrun, worker_ready

    original = module.JobWorker

    class OfflineTransportWorker(original):
        def __init__(self, *args, **kwargs):
            kwargs["client_factory"] = OfflineClient
            super().__init__(*args, **kwargs)

    # Keep the actual production default_worker -> TenantJobWorker -> scope
    # selection path. The only replacement is the injected SDK transport.
    module.JobWorker = OfflineTransportWorker

    @worker_ready.connect(weak=False)
    def ready(**_):
        connection().incr(PREFIX + "worker_boots")

    @task_postrun.connect(weak=False)
    def finished(sender=None, args=None, **_):
        if getattr(sender, "name", None) == "customerinsight.process_job" and args:
            connection().incr(PREFIX + "done:" + args[0])

    celery_app.worker_main([
        "worker", "--loglevel=INFO", "--pool=solo", "--concurrency=1",
        "--hostname=account_acceptance@%h", "--without-gossip", "--without-mingle", "--without-heartbeat",
    ])


def make_app():
    from backend.accounts import AccountService
    from backend.app import create_app
    from backend.database import get_engine
    from core.product_service import ProductRequestBudget

    def refused_agent(*_args, **_kwargs):
        connection().incr(PREFIX + "unexpected_agent_calls")
        raise AssertionError("Cross-account chat reached the model boundary")

    return create_app(data_dir=ROOT / "workspace", account_service=AccountService(get_engine()),
                      budget=ProductRequestBudget(ROOT / "usage.sqlite3", daily_limit=1000), agent_runner=refused_agent)


def request(app, method, path, *, token=None, expected=200, **kwargs):
    from fastapi.testclient import TestClient
    headers = {"Authorization": "Bearer " + token} if token else {}
    with TestClient(app) as client:
        response = client.request(method, path, headers=headers, **kwargs)
    assert response.status_code == expected, f"Unexpected {method} {path}: {response.status_code}"
    return response


def authenticate(app, username):
    return request(app, "POST", "/api/v1/auth/login", json={"username": username, "password": PASSWORD}).json()["access_token"]


def submit(app, tenant, token, count):
    raw = ("review\n" + "\n".join(f"fixture|{tenant}|{index:04d}|delivery concern" for index in range(count)) + "\n").encode()
    uploaded = request(app, "POST", "/api/v1/uploads", token=token, expected=201,
                       data={"purpose": "job"}, files={"file": (tenant + ".csv", raw, "text/csv")}).json()
    payload = {"upload_id": uploaded["upload_id"], "text_column": "review", "allow_model_call": True,
               "max_requests": (count + 9) // 10, "idempotency_key": "same-key-across-two-private-workspaces"}
    accepted = request(app, "POST", "/api/v1/jobs", token=token, expected=202, json=payload).json()
    assert accepted["id"] and not accepted["cached"]
    # No browser/client remains open after the accepted response. New polling
    # clients below must recover the task entirely from database state.
    return {"job_id": accepted["id"], "upload_id": uploaded["upload_id"], "payload": payload,
            "token": token, "raw_sha256": hashlib.sha256(raw).hexdigest()}


def completed(app, item):
    def probe():
        result = request(app, "GET", "/api/v1/jobs/" + item["job_id"], token=item["token"]).json()
        if result["status"] in {"failed", "needs_review", "partial", "cancelled"}:
            raise AssertionError("Unexpected synthetic task terminal state: " + result["status"])
        if (result["status"] == "completed" and not result["batch_id"]
                and int(connection().get(PREFIX + "done:" + item["job_id"]) or 0) > 0):
            raise AssertionError("Completed task returned without persisting its report")
        return result if result["status"] == "completed" and result["batch_id"] else None
    return wait_for(probe, label="completed PostgreSQL job")


def verify_boundaries(app, first, second):
    checks = 0
    for own, foreign in ((first, second), (second, first)):
        token = own["token"]
        jobs = request(app, "GET", "/api/v1/jobs", token=token).json()
        batches = request(app, "GET", "/api/v1/batches", token=token).json()
        assert jobs["total"] == batches["total"] == 1
        assert {item["id"] for item in jobs["items"]} == {own["job_id"]}
        assert {item["id"] for item in batches["items"]} == {own["batch_id"]}
        checks += 2
        for method, path, payload in (
            ("GET", "/api/v1/jobs/" + foreign["job_id"], None),
            ("POST", "/api/v1/jobs/" + foreign["job_id"] + "/cancel", None),
            ("POST", "/api/v1/jobs/" + foreign["job_id"] + "/resume", {"allow_model_call": True, "max_requests": 100}),
            ("GET", "/api/v1/uploads/" + foreign["upload_id"] + "/preview", None),
            ("GET", "/api/v1/batches/" + foreign["batch_id"], None),
            ("GET", "/api/v1/batches/" + foreign["batch_id"] + "/rows", None),
            ("GET", "/api/v1/batches/" + foreign["batch_id"] + "/exports/original", None),
            ("GET", "/api/v1/batches/" + own["batch_id"] + "/chat?comparison_id=" + foreign["batch_id"], None),
            ("POST", "/api/v1/batches/" + own["batch_id"] + "/chat", {
                "question": "Compare the other account's original feedback", "comparison_id": foreign["batch_id"],
                "allow_model_call": True, "max_requests": 100}),
        ):
            options = {"json": payload} if payload is not None else {}
            response = request(app, method, path, token=token, expected=404, **options)
            assert "fixture|" not in response.text
            checks += 1
        original = request(app, "GET", "/api/v1/batches/" + own["batch_id"] + "/exports/original", token=token)
        assert hashlib.sha256(original.content).hexdigest() == own["raw_sha256"]
        checks += 1
    assert int(connection().get(PREFIX + "unexpected_agent_calls") or 0) == 0
    return checks


def report_hashes():
    from sqlalchemy import select
    from backend.database import get_engine
    from backend.pg_history import history_batches
    with get_engine().connect() as database:
        return sorted([list(row) for row in database.execute(select(
            history_batches.c.workspace_id, history_batches.c.id,
            history_batches.c.report_hash, history_batches.c.chat_hash))])


def controller():
    from backend.accounts import AccountService
    from backend.database import get_engine
    redis = connection()
    wait_for(lambda: int(redis.get(PREFIX + "worker_boots") or 0) >= 1, label="real Celery worker ready")
    accounts = AccountService(get_engine())
    users = [accounts.create_user(name, PASSWORD) for name in ("tenant_a", "tenant_b")]
    assert users[0]["workspace_id"] != users[1]["workspace_id"]
    app = make_app()
    first = submit(app, "tenant_a", authenticate(app, "tenant_a"), 500)
    second = submit(app, "tenant_b", authenticate(app, "tenant_b"), 20)
    for item, rows, calls in ((first, 500, 50), (second, 20, 2)):
        job = completed(app, item)
        assert (job["total_rows"], job["success_rows"], job["request_count"]) == (rows, rows, calls)
        item["batch_id"] = job["batch_id"]
        row_page = request(app, "GET", "/api/v1/batches/" + job["batch_id"] + "/rows?limit=100", token=item["token"]).json()
        assert row_page["total"] == rows
    assert int(redis.get(PREFIX + "calls:tenant_a") or 0) == 50
    assert int(redis.get(PREFIX + "calls:tenant_b") or 0) == 2
    checks = verify_boundaries(app, first, second)
    private_state = {"accounts": [first, second], "report_hashes": report_hashes(), "isolation_checks": checks}
    (ROOT / "account-state.json").write_text(json.dumps(private_state), encoding="utf-8")
    print("ACCOUNT_ACCEPTANCE_JSON " + json.dumps({
        "postgres_migrated": True, "accounts": 2, "bulk_rows": 500, "bulk_mock_requests": 50,
        "second_rows": 20, "second_mock_requests": 2, "isolation_checks": checks, "live_llm_calls": 0,
    }), flush=True)


def verify_replay():
    from backend.celery_app import celery_app
    redis = connection()
    wait_for(lambda: int(redis.get(PREFIX + "worker_boots") or 0) >= 2, label="restarted Celery worker ready")
    state = json.loads((ROOT / "account-state.json").read_text(encoding="utf-8"))
    for item in state["accounts"]:
        previous = int(redis.get(PREFIX + "done:" + item["job_id"]) or 0)
        celery_app.send_task("customerinsight.process_job", args=[item["job_id"]], queue="analysis", serializer="json")
        wait_for(lambda: int(redis.get(PREFIX + "done:" + item["job_id"]) or 0) > previous, label="duplicate task consumption")
    assert int(redis.get(PREFIX + "calls:tenant_a") or 0) == 50
    assert int(redis.get(PREFIX + "calls:tenant_b") or 0) == 2
    app = make_app()
    first, second = state["accounts"]
    checks = verify_boundaries(app, first, second)
    for item in (first, second):
        cached = request(app, "POST", "/api/v1/jobs", token=item["token"], json={
            **item["payload"], "allow_model_call": False}).json()
        assert cached["cached"] and cached["batch_id"] == item["batch_id"] and cached["request_count"] == 0
    assert report_hashes() == state["report_hashes"]
    summary = {"verified": True, "postgres_migrated": True, "accounts": 2,
               "bulk_rows": 500, "bulk_mock_requests": 50, "second_rows": 20, "second_mock_requests": 2,
               "isolation_checks": checks + state["isolation_checks"], "real_worker_restarts": 1,
               "duplicate_extra_requests": 0, "report_hashes_unchanged": True,
               "sessions_survive_worker_restart": True, "cross_account_agent_calls": 0, "live_llm_calls": 0}
    (ROOT / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("ACCOUNT_ACCEPTANCE_JSON " + json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("initialize", "worker", "controller", "verify_replay"))
    args = parser.parse_args()
    guarded_root()
    globals()[{"worker": "run_worker"}.get(args.action, args.action)]()


if __name__ == "__main__":
    main()
