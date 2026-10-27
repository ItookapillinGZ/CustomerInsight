"""Operational checks must be scoped, bounded and incapable of inference."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch

import redis
from sqlalchemy import create_engine

from backend.accounts import AccountService
from backend.database import Base, get_engine
from backend.observability import emit_event, event_payload
from backend.operations import ReadinessProbe, ServiceHeartbeat, operations_snapshot
from backend.pg_jobs import PgJobStore
from backend.pg_history import PgHistoryStore
from core.history_store import HistoryStore
from models.models import FeedbackResult
from tests.helpers import offline_temp_root
from tests.test_history_store import sample_report


class FakeRedis:
    def __init__(self):
        self.now, self.values, self.indices, self.available = 0, {}, {}, True

    def _check(self):
        if not self.available:
            raise redis.ConnectionError("private broker credentials")

    def ping(self):
        self._check()
        return True

    def exists(self, key):
        self._check()
        return key in self.values and self.values[key][1] > self.now

    def eval(self, script, count, *args):
        self._check()
        if script.startswith("-- service-alive"):
            candidates = sorted((expiry, key) for key, expiry in self.indices.get(args[0], {}).items() if expiry > self.now)
            return int(any(self.exists(key) for _, key in candidates[:32]))
        key, index, token = args[:3]
        if script.startswith("-- service-pulse"):
            ttl = int(args[3]) / 1000
            self.values[key] = (token, self.now + ttl)
            self.indices[index] = {k: v for k, v in self.indices.get(index, {}).items() if v > self.now}
            self.indices[index][key] = self.now + ttl
            return 1
        if self.exists(key) and self.values[key][0] == token:
            del self.values[key]
            self.indices.get(index, {}).pop(key, None)
            return 1
        return 0


class OperationsTests(unittest.TestCase):
    def test_readiness_requires_database_broker_and_recent_service_leases(self):
        client = FakeRedis()
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        probe = ReadinessProbe(engine=engine, client=client, queued=True)
        self.assertEqual(probe.check()["status"], "not_ready")
        worker = ServiceHeartbeat("worker", client=client, hostname="worker-a")
        dispatcher = ServiceHeartbeat("dispatcher", client=client, hostname="dispatcher-a")
        worker.pulse()
        self.assertFalse(probe.check()["checks"]["dispatcher"])
        dispatcher.pulse()
        self.assertEqual(probe.check()["status"], "ready")
        self.assertFalse(probe.check()["model_checked"])
        client.now = 21
        self.assertFalse(probe.check()["checks"]["worker"])
        worker.pulse()
        dispatcher.pulse()
        self.assertEqual(probe.check()["status"], "ready")

    def test_database_or_broker_failure_exposes_only_boolean_checks(self):
        engine = Mock()
        engine.connect.side_effect = RuntimeError("private db password")
        client = FakeRedis()
        result = ReadinessProbe(engine=engine, client=client, queued=True).check()
        self.assertFalse(result["checks"]["database"])
        self.assertNotIn("private", json.dumps(result))
        client.available = False
        result = ReadinessProbe(engine=engine, client=client, queued=True).check()
        self.assertFalse(result["checks"]["redis"])

    def test_heartbeat_shutdown_cannot_remove_replacement_owner(self):
        client = FakeRedis()
        old = ServiceHeartbeat("worker", client=client, hostname="same-container")
        new = ServiceHeartbeat("worker", client=client, hostname="same-container")
        old.pulse()
        new.pulse()
        old.close()
        self.assertTrue(client.exists(new.key))
        new.close()
        self.assertFalse(client.exists(new.key))

    def test_heartbeat_outage_is_not_an_upstream_call_or_crash(self):
        client = FakeRedis()
        heartbeat = ServiceHeartbeat("worker", client=client)
        client.available = False
        self.assertFalse(heartbeat.pulse())
        heartbeat.close()

    def test_json_events_drop_arbitrary_text_keys_and_invalid_metadata(self):
        payload = event_payload("http_request", request_id="request_123", job_id="a" * 32,
            route="/api/v1/jobs/{job_id}", method="POST", status_code=202, elapsed_ms=1.234567,
            question="private customer question", token="private token", filename="private.xlsx",
            status={"private": "secret"}, error_type="unsafe private value", total_tokens=float("nan"))
        self.assertEqual(payload["elapsed_ms"], 1.235)
        self.assertNotIn("private", json.dumps(payload))
        self.assertNotIn("total_tokens", payload)
        self.assertNotIn("status", payload)
        with self.assertRaises(ValueError):
            event_payload("customer original text")

    def test_json_handler_follows_current_capture_stream(self):
        for _ in range(2):
            output = io.StringIO()
            with redirect_stderr(output):
                emit_event("job_started", job_id="b" * 32, original="private feedback")
            row = json.loads(output.getvalue())
            self.assertEqual(row["event"], "job_started")
            self.assertNotIn("original", row)
            output.close()

    def test_diagnostics_fail_open_for_broken_stream_and_huge_numbers(self):
        self.assertNotIn("total_tokens", event_payload("job_started", total_tokens=10**1000))
        stream = Mock()
        stream.write.side_effect = RuntimeError("private logging failure")
        with redirect_stderr(stream):
            emit_event("job_started", total_tokens=10**1000)
        with patch("backend.observability._LOGGER.info", side_effect=RuntimeError("handler failure")):
            emit_event("job_started")
        emit_event("invalid event")

    def test_celery_failure_redacts_provider_traceback(self):
        from backend.celery_app import process_job, TaskExecutionError
        worker = Mock()
        worker.run.side_effect = RuntimeError("private provider body and secret-key")
        with patch("backend.job_worker.default_worker", return_value=worker):
            try:
                process_job.run("a" * 32)
            except TaskExecutionError as error:
                rendered = "".join(traceback.format_exception(error))
            else:
                self.fail("Task did not preserve failure")
        self.assertNotIn("private provider", rendered)
        self.assertNotIn("secret-key", rendered)


class ScopedOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.root = Path(self.temp.name)
        self.engine = get_engine("sqlite:///" + (self.root / "operations.sqlite3").as_posix())
        Base.metadata.create_all(self.engine)
        self.accounts = AccountService(self.engine)
        self.a = self.accounts.create_user("fixture-a", "PrivateFixturePassword_2026")
        self.b = self.accounts.create_user("fixture-b", "PrivateFixturePassword_2026")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.engine.dispose)

    def store(self, user):
        return PgJobStore(self.engine, workspace_id=user["workspace_id"], data_dir=self.root)

    def make(self, store):
        row = FeedbackResult(feedback_id="row-2", source_row=2, original_text="private customer text")
        return store.create_job(original=b"review\nprivate customer text\n", filename="private-file.csv",
            request={"text_column": "review"}, config={}, groups=[[row.model_dump(mode="json")]],
            initial_results=[row.model_dump(mode="json")], max_requests=1, scope=store.scope)[0]

    def test_metrics_only_count_current_workspace_and_do_not_reserve_requests(self):
        a, b = self.store(self.a), self.store(self.b)
        a.clock = lambda: 100
        job_a, job_b = self.make(a), self.make(b)
        group = a.claim_group(job_a["id"], "fixture", 30)
        a.mark_sent(job_a["id"], group["id"], group["lease_token"])
        a.checkpoint_usage(job_a["id"], group["id"], group["lease_token"], usage={"total_tokens": 140})
        a.clock = lambda: 102
        a.record_failure(job_a["id"], group["id"], group["lease_token"], uncertain=False)
        budget = Mock(daily_limit=50)
        budget.remaining.return_value = 49
        result = operations_snapshot(a, budget=budget, now=103)
        self.assertEqual(result["reserved_requests"], 1)
        self.assertEqual(result["attempt_window"]["tokens"]["total_tokens"], 140)
        self.assertEqual(result["attempt_window"]["latency_ms"]["p95"], 2000)
        self.assertNotIn(job_b["id"], json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(operations_snapshot(b, budget=budget)["reserved_requests"], 0)
        budget.reserve.assert_not_called()
        with self.assertRaises(ValueError):
            operations_snapshot(PgJobStore(self.engine, data_dir=self.root), budget=budget)

    def test_clear_chat_keeps_other_comparison_scope_and_rejects_foreign_reference(self):
        store = PgHistoryStore(self.engine, workspace_id=self.a["workspace_id"], data_dir=self.root)
        current = store.save_batch(sample_report(), filename="fixture.csv", file_bytes=b"current", mapping={"text_column": "评价"})
        previous = store.save_batch(sample_report("old"), filename="fixture.csv", file_bytes=b"previous", mapping={"text_column": "评价"})
        store.save_chat(current, [{"question": "current", "comparison_batch_id": None},
                                  {"question": "comparison", "comparison_batch_id": previous}])
        store.clear_chat(current)
        self.assertEqual([turn["question"] for turn in store.load_chat(current)], ["comparison"])
        foreign = PgHistoryStore(self.engine, workspace_id=self.b["workspace_id"], data_dir=self.root)
        foreign_id = foreign.save_batch(sample_report(), filename="fixture.csv", file_bytes=b"foreign", mapping={"text_column": "评价"})
        from core.history_store import HistoryNotFoundError
        with self.assertRaises(HistoryNotFoundError):
            store.clear_chat(current, foreign_id)
        self.assertEqual(len(store.load_chat(current)), 1)

    def test_legacy_clear_chat_is_scoped_and_preserves_other_turns(self):
        store = HistoryStore(self.root / "legacy")
        current = store.save_batch(sample_report(), filename="fixture.csv", file_bytes=b"current", mapping={})
        previous = store.save_batch(sample_report("old"), filename="fixture.csv", file_bytes=b"previous", mapping={})
        store.save_chat(current, [{"question": "current"}, {"question": "comparison", "comparison_batch_id": previous}])
        store.clear_chat(current)
        self.assertEqual([turn["question"] for turn in store.load_chat(current)], ["comparison"])
