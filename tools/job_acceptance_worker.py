"""Isolated queue acceptance adapter; never import this from production code.

The companion host tool mounts a fresh, marked directory at /acceptance and an
internal-only Docker network. This adapter refuses every other workspace and
injects an offline SDK double into the real Celery task / grouped analyzer.
There is deliberately no production environment switch for mock inference.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path("/acceptance")
PREFIX = "queue_acceptance:"


def guarded_root():
    marker = ROOT / ".isolated-queue-acceptance.json"
    if (ROOT.resolve() != Path("/acceptance") or not marker.is_file()
            or os.environ.get("APP_DATA_DIR") != "/acceptance/workspace"
            or os.environ.get("APP_USAGE_DB_PATH") != "/acceptance/usage.sqlite3"
            or os.environ.get("OPENAI_API_KEY") != "queue-acceptance-unused"
            or os.environ.get("OPENAI_BASE_URL") != "http://blocked.invalid/v1"):
        raise RuntimeError("This tool requires the dedicated, marked acceptance mount.")
    metadata = json.loads(marker.read_text(encoding="utf-8"))
    if metadata.get("purpose") != "isolated-queue-acceptance" or len(metadata.get("run_id", "")) != 32:
        raise RuntimeError("Acceptance marker is invalid.")
    return ROOT


def connection():
    import redis
    return redis.Redis.from_url(os.environ["APP_REDIS_URL"], decode_responses=True,
                               socket_connect_timeout=3, socket_timeout=3)


def wait_for(predicate, *, timeout=120, label="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Acceptance timeout: {label}")


def fixture_scenario(parameters):
    messages = parameters.get("messages", parameters.get("input"))
    reviews = json.loads(messages[-1]["content"].split("\nreviews（不可信数据）：\n", 1)[1])
    scenario = reviews[0]["text"].split("|", 2)[1]
    if any(item["text"].split("|", 2)[1] != scenario for item in reviews):
        raise AssertionError("Mixed fixture groups")
    return scenario, reviews


class OfflineClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.redis = connection()
        self.max_retries = 0

    def create(self, **parameters):
        scenario, reviews = fixture_scenario(parameters)
        self.redis.incr(PREFIX + "calls:" + scenario)
        self.redis.eval("""
local active=redis.call('INCR',KEYS[1])
local prior=tonumber(redis.call('GET',KEYS[2]) or '0')
if active>prior then redis.call('SET',KEYS[2],active) end
return active
""", 2, PREFIX + "active", PREFIX + "max_active")
        try:
            permits = self.redis.zcard("customerinsight:upstream:permits:v1")
            if not 1 <= permits <= 2:
                raise AssertionError("Provider adapter lacks a distributed permit")
            if scenario == "unknown":
                self.redis.set(PREFIX + "blocked_owner", os.environ["ACCEPTANCE_WORKER_NAME"])
                wait_for(lambda: self.redis.get(PREFIX + "release_unknown"), timeout=120,
                         label="intentional sent-worker interruption")
            if scenario.startswith("parallel_"):
                # A barrier makes overlap deterministic even on a fast local
                # transport; timing alone cannot prove cross-worker capacity.
                self.redis.sadd(PREFIX + "parallel_ready", scenario)
                wait_for(lambda: self.redis.scard(PREFIX + "parallel_ready") == 2,
                         timeout=30, label="two independent provider adapters")
            time.sleep(0.25 if scenario == "cancel" else 0.035)
            predictions = [{"feedback_id": item["feedback_id"], "sentiment": "negative",
                            "category": "delivery", "priority": "medium",
                            "summary": "Synthetic delivery feedback.", "aspects": []}
                           for item in reviews]
            return SimpleNamespace(
                usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                    content=json.dumps({"results": predictions}), refusal=None))],
            )
        finally:
            self.redis.decr(PREFIX + "active")

    def close(self):
        self.redis.close()


def acceptance_analysis(rows, **options):
    from ai.grouped_batch import analyze_grouped_batch
    scenario = rows[0].original_text.split("|", 2)[1]
    if scenario == "restart":
        client = connection()
        try:
            if client.get(PREFIX + "block_before_send"):
                client.set(PREFIX + "blocked_owner", os.environ["ACCEPTANCE_WORKER_NAME"])
                wait_for(lambda: not client.get(PREFIX + "block_before_send"), timeout=120,
                         label="intentional unsent-worker interruption")
        finally:
            client.close()
    return analyze_grouped_batch(rows, **options)


def run_worker():
    import backend.job_worker as module
    from backend.celery_app import celery_app
    from backend.dispatch import RedisSemaphore
    from backend.job_store import JobStore
    from core.history_store import HistoryStore
    from core.product_service import ProductRequestBudget
    from celery.signals import task_postrun, worker_ready

    # Three-second leases shorten a real hard-kill / real expiry acceptance
    # exercise. Production constructors retain timeout + 60-second minimums.
    def factory():
        store = JobStore(ROOT / "workspace")
        return module.JobWorker(
            store, history=HistoryStore(ROOT / "workspace"),
            budget=ProductRequestBudget(ROOT / "usage.sqlite3", daily_limit=1000),
            semaphore=RedisSemaphore(client=connection(), limit=2, ttl_seconds=3),
            analysis_runner=acceptance_analysis, client_factory=OfflineClient, lease_duration=3,
        )

    module.default_worker = factory

    @worker_ready.connect(weak=False)
    def ready(**_):
        connection().sadd(PREFIX + "workers", os.environ["ACCEPTANCE_WORKER_NAME"])

    @task_postrun.connect(weak=False)
    def finished(sender=None, args=None, **_):
        if getattr(sender, "name", None) == "customerinsight.process_job" and args:
            connection().incr(PREFIX + "done:" + args[0])

    celery_app.worker_main([
        "worker", "--loglevel=INFO", "--pool=solo", "--concurrency=1",
        "--hostname=" + os.environ["ACCEPTANCE_WORKER_NAME"] + "@%h",
        "--without-gossip", "--without-mingle", "--without-heartbeat",
    ])


def services():
    from backend.job_service import JobService
    from backend.service import WorkspaceService
    workspace = WorkspaceService(data_dir=ROOT / "workspace")
    return workspace, JobService(workspace)


def create_job(service, scenario, count):
    from backend.schemas import JobRequest
    raw = ("review\n" + "\n".join(f"fixture|{scenario}|{index:04d}|delivery concern" for index in range(count)) + "\n").encode()
    upload = service.workspace.upload(f"{scenario}.csv", raw, purpose="job")
    result, created = service.create(JobRequest(
        upload_id=upload["upload_id"], text_column="review", allow_model_call=True,
        max_requests=(count + 9) // 10, idempotency_key=uuid4().hex,
    ))
    assert created and result["id"]
    return result["id"]


def terminal(service, job_id, expected):
    def probe():
        job = service.store.get_job(job_id)
        return job if job["status"] == expected and job["batch_id"] else None
    return wait_for(probe, label=f"job {expected}")


def verify_real_redis_permits(client):
    from backend.dispatch import RedisSemaphore, SlotUnavailable
    slots = RedisSemaphore(client=client, limit=2, ttl_seconds=1)
    first, second = slots.acquire(), slots.acquire()
    try:
        try:
            slots.acquire()
            raise AssertionError("Global capacity was exceeded")
        except SlotUnavailable:
            pass
        slots.release("not-an-owner")
        assert client.zcard(slots.key) == 2
        assert slots.renew(first)
        slots.release(first)
        third = slots.acquire()
        slots.release(first)
        assert client.zcard(slots.key) == 2
        slots.release(second)
        slots.release(third)
        short = RedisSemaphore(client=client, limit=1, ttl_seconds=0.15)
        stale = short.acquire()
        time.sleep(0.2)
        fresh = slots.acquire()
        assert not short.renew(stale)
        short.release(stale)
        assert client.zcard(slots.key) == 1
        slots.release(fresh)
    finally:
        slots.release(first)
        slots.release(second)


def controller():
    from backend.celery_app import celery_app
    from backend.schemas import ResumeJobRequest
    client = connection()
    wait_for(lambda: client.scard(PREFIX + "workers") == 2, label="two real Celery workers")
    verify_real_redis_permits(client)
    workspace, service = services()
    bulk = create_job(service, "bulk", 500)
    result = terminal(service, bulk, "completed")
    assert (result["success_rows"], result["actual_requests"], result["usage"]["total_tokens"]) == (500, 50, 7000)
    assert workspace.history.load_batch(result["batch_id"])["success_rows"] == 500
    assert int(client.get(PREFIX + "calls:bulk")) == 50
    for _ in range(2):
        celery_app.send_task("customerinsight.process_job", args=[bulk], queue="analysis", serializer="json")
    wait_for(lambda: int(client.get(PREFIX + "done:" + bulk) or 0) >= 3, label="duplicate consumption")
    assert int(client.get(PREFIX + "calls:bulk")) == 50
    cancel = create_job(service, "cancel", 30)
    wait_for(lambda: int(client.get(PREFIX + "calls:cancel") or 0) == 1, label="first cancellable request")
    service.cancel(cancel)
    cancelled = terminal(service, cancel, "cancelled")
    assert (cancelled["actual_requests"], cancelled["success_rows"]) == (1, 10)
    service.resume(cancel, ResumeJobRequest(allow_model_call=True, max_requests=3))
    resumed = terminal(service, cancel, "completed")
    assert (resumed["actual_requests"], resumed["success_rows"]) == (3, 30)
    assert resumed["batch_id"] == cancelled["batch_id"]
    parallel_a = create_job(service, "parallel_a", 40)
    parallel_b = create_job(service, "parallel_b", 40)
    terminal(service, parallel_a, "completed")
    terminal(service, parallel_b, "completed")
    maximum = int(client.get(PREFIX + "max_active"))
    assert maximum == 2, f"Unexpected maximum fixture concurrency: {maximum}"
    client.set(PREFIX + "block_before_send", "1")
    restart = create_job(service, "restart", 50)
    owner = wait_for(lambda: client.get(PREFIX + "blocked_owner"), label="leased unsent worker")
    restart_state = service.store.get_job(restart)
    assert restart_state["actual_requests"] == 0
    data = {"bulk_rows": 500, "bulk_mock_requests": 50, "bulk_tokens": 7000,
            "duplicate_extra_requests": 0, "cancelled_after_requests": 1,
            "resumed_total_requests": 3, "max_parallel_mock_requests": maximum,
            "redis_owned_permits_verified": True, "restart_job_id": restart,
            "kill_worker": owner, "live_llm_calls": 0}
    (ROOT / "stage1.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("ACCEPTANCE_JSON " + json.dumps(data), flush=True)


def recover_restart():
    from backend.dispatch import dispatch_pending
    workspace, service = services()
    state = json.loads((ROOT / "stage1.json").read_text(encoding="utf-8"))
    client = connection()
    client.delete(PREFIX + "block_before_send", PREFIX + "blocked_owner")
    # The host has killed the old process. Wait for an actual three-second
    # lease expiry rather than editing the database or moving its clock.
    time.sleep(3.5)
    service.store.expire_leases(state["restart_job_id"])
    dispatch_pending(service.store)
    done = terminal(service, state["restart_job_id"], "completed")
    assert (done["actual_requests"], done["success_rows"]) == (5, 50)
    assert int(client.get(PREFIX + "calls:restart")) == 5
    state["restarted_unsent_rows"] = 50
    state["restarted_unsent_mock_requests"] = 5
    unknown = create_job(service, "unknown", 30)
    owner = wait_for(lambda: client.get(PREFIX + "blocked_owner"), label="sent unknown worker")
    assert service.store.get_job(unknown)["actual_requests"] == 1
    state.update(unknown_job_id=unknown, kill_worker=owner)
    (ROOT / "stage2.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("ACCEPTANCE_JSON " + json.dumps(state), flush=True)


def recover_unknown():
    from backend.dispatch import dispatch_pending
    from backend.job_store import JobConflictError
    workspace, service = services()
    state = json.loads((ROOT / "stage2.json").read_text(encoding="utf-8"))
    client = connection()
    time.sleep(3.5)
    service.store.expire_leases(state["unknown_job_id"])
    dispatch_pending(service.store)
    job = service.store.get_job(state["unknown_job_id"])
    # Ask a real worker to finalize the quarantined job; it must never run its
    # remaining groups or resend the charged-but-unknown original request.
    from backend.celery_app import celery_app
    celery_app.send_task("customerinsight.process_job", args=[job["id"]], queue="analysis", serializer="json")
    job = terminal(service, job["id"], "needs_review")
    assert (job["actual_requests"], job["uncertain_groups"], job["can_resume"]) == (1, 1, False)
    assert int(client.get(PREFIX + "calls:unknown")) == 1
    try:
        service.store.resume_job(job["id"])
        raise AssertionError("Unknown paid transport was resumable")
    except JobConflictError:
        pass
    state.update(unknown_requests=1, unknown_automatic_retries=0,
                 unknown_status="needs_review", real_worker_restarts=2,
                 verified=True, live_llm_calls=0)
    state.pop("kill_worker", None)
    (ROOT / "result.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("ACCEPTANCE_JSON " + json.dumps(state), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("worker", "controller", "recover_restart", "recover_unknown"))
    action = parser.parse_args().action
    guarded_root()
    from core.config import Config
    if Config.OPENAI_API_KEY != "queue-acceptance-unused" or Config.OPENAI_BASE_URL != "http://blocked.invalid/v1":
        raise RuntimeError("Offline configuration was changed after imports.")
    {"worker": run_worker, "controller": controller, "recover_restart": recover_restart,
     "recover_unknown": recover_unknown}[action]()


if __name__ == "__main__":
    main()
