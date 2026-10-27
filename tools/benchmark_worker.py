"""Guarded, tool-only HTTP load runner and injected offline SDK transport.

This module is never imported by the production application. Only the worker's
SDK factory is replaced; routing, auth, parsing, validation, repositories,
budgets, Redis permits, Celery, and final report generation remain real.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.benchmark_metrics import latency_summary

ROOT = Path("/benchmark")
PREFIX = "benchmark:"
PASSWORD = "benchmark-only-password-2026"


def guarded_root():
    marker = ROOT / ".isolated-backend-benchmark.json"
    database = urlsplit(os.environ.get("APP_DATABASE_URL", ""))
    if (ROOT.resolve() != Path("/benchmark") or not marker.is_file()
            or os.environ.get("APP_DATA_DIR") != "/benchmark/workspace"
            or os.environ.get("APP_USAGE_DB_PATH") != "/benchmark/usage.sqlite3"
            or os.environ.get("APP_REDIS_URL") != "redis://redis:6379/0"
            or os.environ.get("OPENAI_API_KEY") != "benchmark-unused"
            or os.environ.get("OPENAI_BASE_URL") != "http://blocked.invalid/v1"
            or database.hostname != "postgres" or database.path != "/benchmark"
            or database.username != "benchmark" or Path("/app/.env").exists()):
        raise RuntimeError("This tool requires its own marked benchmark volume and offline configuration")
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    if (metadata.get("purpose") != "isolated-backend-benchmark"
            or not re.fullmatch(r"[0-9a-f]{32}", metadata.get("run_id", ""))
            or not 1 <= metadata.get("accounts", 0) <= 10
            or not 1 <= metadata.get("rows_per_account", 0) <= 1000
            or not 0 <= metadata.get("mock_delay_ms", -1) <= 5000):
        raise RuntimeError("Invalid isolated benchmark marker")
    return metadata


def connection():
    import redis
    return redis.Redis.from_url(os.environ["APP_REDIS_URL"], decode_responses=True,
                               socket_connect_timeout=3, socket_timeout=3)


class OfflineClient:
    def __init__(self):
        self.configuration = guarded_root()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.redis = connection()
        self.max_retries = 0

    def create(self, **parameters):
        messages = parameters["messages"]
        reviews = json.loads(messages[-1]["content"].split("\nreviews（不可信数据）：\n", 1)[1])
        permitted = {f"bench_{index:02d}" for index in range(self.configuration["accounts"])}
        tenants = set()
        for row in reviews:
            match = re.fullmatch(r"fixture\|(bench_\d{2})\|\d{4}\|delivery concern", row["text"])
            if not match or match[1] not in permitted:
                raise AssertionError("Non-synthetic feedback reached the offline SDK adapter")
            tenants.add(match[1])
        if len(tenants) != 1 or self.redis.zcard("customerinsight:upstream:permits:v1") != 1:
            raise AssertionError("Mixed account data or missing upstream concurrency permit")
        self.redis.incr(PREFIX + "mock_requests")
        self.redis.incrby(PREFIX + "mock_rows", len(reviews))
        time.sleep(self.configuration["mock_delay_ms"] / 1000)
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
    command.upgrade(Config("/app/alembic.ini"), "head")


def api():
    import uvicorn
    uvicorn.run("backend.app:app", host="0.0.0.0", port=8000, workers=1,
                access_log=False, log_level="warning")


def worker():
    import backend.job_worker as module
    from backend.celery_app import celery_app
    from celery.signals import worker_ready
    original = module.JobWorker

    class OfflineTransportWorker(original):
        def __init__(self, *args, **kwargs):
            kwargs["client_factory"] = OfflineClient
            super().__init__(*args, **kwargs)

    module.JobWorker = OfflineTransportWorker

    @worker_ready.connect(weak=False)
    def ready(**_):
        connection().set(PREFIX + "worker_ready", "1")

    celery_app.worker_main([
        "worker", "--loglevel=WARNING", "--pool=prefork", "--concurrency=1",
        "--hostname=benchmark@%h", "--without-gossip", "--without-mingle", "--without-heartbeat",
    ])


def hardware():
    def text(path):
        source = Path(path)
        return source.read_text().strip() if source.is_file() else None
    return {"os": platform.platform(), "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(), "cpu_quota_cgroup_v2": text("/sys/fs/cgroup/cpu.max"),
            "memory_limit_cgroup_v2": text("/sys/fs/cgroup/memory.max"),
            "python": platform.python_version(),
            "versions": {name: importlib.metadata.version(name) for name in
                         ("fastapi", "uvicorn", "sqlalchemy", "psycopg", "celery", "redis", "httpx",
                          "openai", "pydantic", "pandas")}}


def readiness(stage):
    """Diagnostic HTTP checks outside the measured load, on this private stack."""
    import httpx
    configuration = guarded_root()
    healthy = stage in {"all_ready", "recovered_ready"}
    expected = {"database": True, "redis": True, "worker": stage != "missing_services",
                "dispatcher": healthy}
    status = 200 if healthy else 503
    started = time.monotonic()
    if stage == "dispatcher_expired":
        # The real dispatcher uses interval=5 -> TTL=max(20, 5*3+10)=25.
        # Waiting beyond it tests an ungraceful crash, not the close() hook.
        time.sleep(26)
    deadline = time.monotonic() + 30
    with httpx.Client(base_url="http://api:8000", trust_env=False, timeout=5) as client:
        while True:
            try:
                response = client.get("/readyz")
                payload = response.json()
                if response.status_code == status and payload.get("checks") == expected:
                    break
            except (httpx.TransportError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("Unexpected isolated readiness state: " + stage)
            time.sleep(.1)
    if payload.get("model_checked") is not False or payload.get("queue_required") is not True:
        raise AssertionError("Readiness unexpectedly reached a model boundary")
    counter = int(connection().get(PREFIX + "mock_requests") or 0)
    expected_calls = configuration["accounts"] * ((configuration["rows_per_account"] + 9) // 10)
    if counter != (expected_calls if stage in {"dispatcher_expired", "recovered_ready"} else 0):
        raise AssertionError("Readiness diagnostics changed the model request count")
    print("READINESS_JSON " + json.dumps({"stage": stage, "http_status": status,
          "checks": payload["checks"], "elapsed_seconds": round(time.monotonic() - started, 3),
          "mock_requests": counter, "live_llm_calls": 0, "outside_benchmark_timing": True}), flush=True)


def controller():
    import httpx
    from sqlalchemy import text
    from backend.accounts import AccountService
    from backend.database import get_engine

    configuration = guarded_root()
    redis = connection()
    deadline = time.monotonic() + configuration["timeout"]
    while not redis.get(PREFIX + "worker_ready"):
        if time.monotonic() > deadline:
            raise RuntimeError("Benchmark worker did not become ready")
        time.sleep(.1)
    with httpx.Client(base_url="http://api:8000", trust_env=False, timeout=10) as probe:
        while True:
            try:
                if probe.get("/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Benchmark HTTP API did not become ready")
            time.sleep(.1)

    accounts = AccountService(get_engine())
    for index in range(configuration["accounts"]):
        accounts.create_user(f"bench_{index:02d}", PASSWORD)
    metrics = {"http_requests": 0, "http_failures": 0, "submit": [], "read": [], "end_to_end": []}
    lock = threading.Lock()
    barrier = threading.Barrier(configuration["accounts"])

    def request(client, method, path, *, expected=200, sample=None, **kwargs):
        started = time.perf_counter()
        failed = False
        try:
            response = client.request(method, path, **kwargs)
            if response.status_code != expected:
                failed = True
                raise RuntimeError(f"Benchmark HTTP status mismatch: {response.status_code}, expected {expected}")
            return response
        except Exception:
            failed = True
            raise
        finally:
            elapsed = time.perf_counter() - started
            with lock:
                metrics["http_requests"] += 1
                metrics["http_failures"] += int(failed)
                if sample:
                    metrics[sample].append(elapsed)

    def submit(index):
        tenant = f"bench_{index:02d}"
        with httpx.Client(base_url="http://api:8000", trust_env=False, timeout=30) as client:
            token = request(client, "POST", "/api/v1/auth/login", json={
                "username": tenant, "password": PASSWORD}).json()["access_token"]
            headers = {"Authorization": "Bearer " + token}
            raw = ("review\n" + "\n".join(
                f"fixture|{tenant}|{row:04d}|delivery concern" for row in range(configuration["rows_per_account"])) + "\n").encode()
            uploaded = request(client, "POST", "/api/v1/uploads", expected=201, headers=headers,
                               data={"purpose": "job"}, files={"file": (tenant + ".csv", raw, "text/csv")}).json()
            barrier.wait(timeout=30)
            started = time.perf_counter()
            job = request(client, "POST", "/api/v1/jobs", expected=202, sample="submit", headers=headers, json={
                "upload_id": uploaded["upload_id"], "text_column": "review", "allow_model_call": True,
                "max_requests": (configuration["rows_per_account"] + 9) // 10,
                "idempotency_key": "benchmark-only-idempotency-key-" + tenant}).json()
            return {"id": job["id"], "token": token, "started": started}

    workload_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=configuration["accounts"]) as executor:
        jobs = list(executor.map(submit, range(configuration["accounts"])))
    max_queued = max_active = 0
    terminal_errors = 0
    with httpx.Client(base_url="http://api:8000", trust_env=False, timeout=30) as client:
        while not all(job.get("done") for job in jobs):
            queued = active = 0
            for job in jobs:
                if job.get("done"):
                    continue
                headers = {"Authorization": "Bearer " + job["token"]}
                state = request(client, "GET", "/api/v1/jobs/" + job["id"], headers=headers, sample="read").json()
                queued += int(state["status"] == "queued")
                active += int(state["status"] == "running")
                if state["status"] in {"failed", "partial", "cancelled", "needs_review"}:
                    terminal_errors += 1
                    job["done"] = True
                    continue
                if state["status"] == "completed" and state["batch_id"]:
                    if state["success_rows"] != configuration["rows_per_account"]:
                        raise AssertionError("Completed job has unexpected row count")
                    metrics["end_to_end"].append(time.perf_counter() - job["started"])
                    batch_id = state["batch_id"]
                    result = request(client, "GET", f"/api/v1/batches/{batch_id}/rows?limit=100",
                                     headers=headers, sample="read").json()
                    if result["total"] != configuration["rows_per_account"]:
                        raise AssertionError("Persisted report row count differs from submitted source")
                    job["done"] = True
            max_queued = max(max_queued, queued)
            max_active = max(max_active, active)
            if time.monotonic() > deadline:
                raise RuntimeError("Benchmark workload timed out")
            if not all(job.get("done") for job in jobs):
                time.sleep(.1)
    elapsed = time.perf_counter() - workload_started
    rows = configuration["accounts"] * configuration["rows_per_account"]
    expected_calls = configuration["accounts"] * ((configuration["rows_per_account"] + 9) // 10)
    calls = int(redis.get(PREFIX + "mock_requests") or 0)
    mock_rows = int(redis.get(PREFIX + "mock_rows") or 0)
    if terminal_errors or calls != expected_calls or mock_rows != rows:
        raise AssertionError("Synthetic workload did not complete exactly once")
    with get_engine().connect() as database:
        postgres_version = database.execute(text("SHOW server_version")).scalar_one()
    summary = {
        "verified": True, "kind": "offline_application_scheduling_benchmark",
        "accounts": configuration["accounts"], "concurrent_submitters": configuration["accounts"],
        "rows_per_account": configuration["rows_per_account"], "rows": rows, "group_size": 10,
        "max_group_characters": 12000, "warmup_model_requests": 0,
        "celery_workers": 1, "celery_pool": "prefork", "api_workers": 1,
        "mock_delay_ms": configuration["mock_delay_ms"], "mock_requests": calls, "live_llm_calls": 0,
        "wall_seconds": round(elapsed, 3), "rows_per_second": round(rows / elapsed, 3),
        "submit_http": latency_summary(metrics["submit"]), "poll_and_rows_http": latency_summary(metrics["read"]),
        "job_end_to_end": latency_summary(metrics["end_to_end"]),
        "http_requests": metrics["http_requests"], "http_failures": metrics["http_failures"],
        "http_error_rate": metrics["http_failures"] / metrics["http_requests"], "failed_jobs": terminal_errors,
        "max_queued_observed": max_queued, "max_running_observed": max_active,
        "queue_observation": "Sequential authenticated job snapshots, once per ~100 ms sweep; observed lower bound, not a Redis peak.",
        "timing_scope": "Wall throughput includes login/upload/setup; job latency includes submission, queue wait, parsing, persistence and finalization.",
        "hardware": hardware(), "postgres_server": postgres_version, "redis_server": redis.info()["redis_version"],
    }
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("BENCHMARK_JSON " + json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser()
    diagnostics = ("missing_services", "all_ready", "dispatcher_expired", "recovered_ready")
    parser.add_argument("action", choices=("initialize", "api", "worker", "controller", *diagnostics))
    args = parser.parse_args()
    guarded_root()
    if args.action in diagnostics:
        readiness(args.action)
    else:
        globals()[args.action]()


if __name__ == "__main__":
    main()
