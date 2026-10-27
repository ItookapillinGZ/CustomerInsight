"""Paid-call safety and durable progress; all provider transports are doubles."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import httpx
from openai import APITimeoutError

from backend.dispatch import SlotUnavailable, dispatch_pending
from backend.job_store import JobStore
from backend.job_worker import JobWorker
from core.config import Config
from core.history_store import HistoryStore, _pack
from core.product_service import ProductRequestBudget
from models.models import FeedbackResult
from tests.helpers import offline_temp_root
from tests.test_grouped_batch import chat_response, client_double, predictions


class OfflineSemaphore:
    def __init__(self):
        self.tokens = set()
        self.serial = 0

    def acquire(self):
        self.serial += 1
        token = str(self.serial)
        self.tokens.add(token)
        return token

    def renew(self, token):
        return token in self.tokens

    def release(self, token):
        self.tokens.discard(token)


class JobWorkerTests(unittest.TestCase):
    def setUp(self):
        for key, value in (("API_MODE", "chat_completions"), ("CHAT_OUTPUT_MODE", "json_object")):
            setting = patch.object(Config, key, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root)
        self.history = HistoryStore(self.root)
        self.budget = ProductRequestBudget(self.root / "usage.sqlite3", daily_limit=1000)
        self.client = client_double()
        self.slots = OfflineSemaphore()

    def make_job(self, count=25, max_requests=None, **config_changes):
        rows = [FeedbackResult(feedback_id=f"row-{number+2}", source_row=number+2,
                               original_text=f"Delivery review {number:04d}") for number in range(count)]
        frame = pd.DataFrame({"review": [row.original_text for row in rows]})
        config = {"model": Config.OPENAI_MODEL, "api_mode": Config.API_MODE,
                  "chat_output_mode": Config.CHAT_OUTPUT_MODE,
                  "endpoint_hash": hashlib.sha256((Config.OPENAI_BASE_URL or "default").encode()).hexdigest(),
                  "prompt_version": "v3_grouped_aspects_v2", "batch_size": 10,
                  "max_characters": 12000, "frame_snapshot": _pack(frame), **config_changes}
        return self.store.create_job(
            original=frame.to_csv(index=False).encode(), filename="reviews.csv",
            request={"sheet_name": None, "encoding": "utf-8-sig", "text_column": "review",
                     "group_column": None, "date_column": None}, config=config,
            groups=[[row.model_dump(mode="json") for row in rows[start:start+10]]
                    for start in range(0, count, 10)],
            initial_results=[row.model_dump(mode="json") for row in rows],
            max_requests=max_requests if max_requests is not None else (count+9)//10,
        )[0]

    def worker(self, **options):
        return JobWorker(self.store, history=self.history, budget=self.budget,
                         semaphore=self.slots, client_factory=lambda: self.client,
                         lease_duration=120, **options)

    def test_500_rows_use_fifty_requests_and_duplicate_consumption_never_rewrites_report(self):
        job = self.make_job(500)
        complete = self.worker().run(job["id"])
        self.assertEqual((complete["status"], complete["success_rows"], complete["actual_requests"]),
                         ("completed", 500, 50))
        self.assertEqual(self.client.chat.completions.create.call_count, 50)
        self.assertEqual(complete["usage"]["total_tokens"], 50 * 140)
        saved = self.history.load_batch(complete["batch_id"])
        self.assertEqual(saved["total_rows"], 500)
        self.worker().run(job["id"])
        self.assertEqual(self.client.chat.completions.create.call_count, 50)
        self.assertEqual(self.history.load_batch(complete["batch_id"])["updated_at"], saved["updated_at"])
        self.assertFalse(self.slots.tokens)

    def test_cancel_during_first_request_preserves_it_and_does_not_start_next_group(self):
        job = self.make_job()

        def response(**params):
            self.store.cancel_job(job["id"])
            return chat_response(predictions(params))

        self.client.chat.completions.create.side_effect = response
        stopped = self.worker().run(job["id"])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual((stopped["success_rows"], stopped["actual_requests"]), (10, 1))
        self.assertTrue(stopped["can_resume"])
        self.assertIsNotNone(stopped["batch_id"])
        self.assertFalse(self.slots.tokens)

    def test_cancel_before_processing_has_no_client_and_still_exports_pending_report(self):
        job = self.make_job()
        self.store.cancel_job(job["id"])
        with patch("backend.job_worker.get_openai_client") as factory:
            result = self.worker().run(job["id"])
            factory.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["actual_requests"], 0)
        self.assertIsNotNone(result["batch_id"])

    def test_hard_request_cap_cannot_be_exceeded_by_many_remaining_groups(self):
        job = self.make_job(max_requests=1)
        result = self.worker().run(job["id"])
        self.assertEqual((result["status"], result["actual_requests"], result["success_rows"]),
                         ("partial", 1, 10))
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertIsNotNone(result["batch_id"])

    def test_runtime_daily_exhaustion_pauses_and_saves_completed_groups(self):
        self.budget = ProductRequestBudget(self.root / "limited.sqlite3", daily_limit=1)
        job = self.make_job()
        result = self.worker().run(job["id"])
        self.assertEqual(result["error_code"], "daily_budget_exhausted")
        self.assertEqual((result["actual_requests"], result["success_rows"]), (1, 10))
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertTrue(result["can_resume"])
        self.assertFalse(self.slots.tokens)

    def test_disconnected_shared_gate_sends_nothing_and_requires_resume(self):
        job = self.make_job()
        self.slots.acquire = Mock(side_effect=SlotUnavailable("offline"))
        result = self.worker().run(job["id"])
        self.assertEqual(result["actual_requests"], 0)
        self.assertEqual(result["error_code"], "execution_paused")
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(self.budget.remaining(), 1000)

    def test_changed_execution_configuration_sends_nothing(self):
        job = self.make_job(model="obsolete-model")
        result = self.worker().run(job["id"])
        self.assertEqual(result["error_code"], "configuration_changed")
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(result["actual_requests"], 0)

    def test_hard_interruption_after_send_quarantines_and_never_automatically_retries(self):
        job = self.make_job()
        calls = 0

        def response(**params):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("isolated worker loss")
            return chat_response(predictions(params))

        self.client.chat.completions.create.side_effect = response
        with self.assertRaises(KeyboardInterrupt):
            self.worker().run(job["id"])
        stopped = self.store.get_job(job["id"])
        self.assertEqual((stopped["status"], stopped["actual_requests"], stopped["success_rows"]),
                         ("needs_review", 2, 10))
        self.assertFalse(stopped["can_resume"])
        self.assertIsNotNone(stopped["batch_id"])
        self.worker().run(job["id"])
        self.assertEqual(calls, 2)
        self.assertFalse(self.slots.tokens)

    def test_invalid_output_still_saves_actual_usage_and_never_retries_failed_group(self):
        job = self.make_job(5)
        self.client.chat.completions.create.return_value = chat_response([])
        self.client.chat.completions.create.side_effect = None
        result = self.worker().run(job["id"])
        self.assertEqual((result["status"], result["actual_requests"]), ("failed", 1))
        self.assertEqual(result["usage"]["total_tokens"], 140)
        self.assertEqual(self.store.attempts(job["id"])[0]["usage"]["total_tokens"], 140)
        self.assertEqual(len(self.store.get_results(job["id"])), 5)
        self.worker().run(job["id"])
        self.assertEqual(self.client.chat.completions.create.call_count, 1)

    def test_sdk_timeout_without_response_is_quarantined_not_treated_as_known_failure(self):
        job = self.make_job()
        self.client.chat.completions.create.side_effect = APITimeoutError(
            request=httpx.Request("POST", "https://offline.invalid/v1/chat/completions"))
        result = self.worker().run(job["id"])
        self.assertEqual((result["status"], result["actual_requests"]), ("needs_review", 1))
        self.assertEqual(result["uncertain_groups"], 1)
        self.assertFalse(result["can_resume"])
        self.assertIsNotNone(result["batch_id"])
        self.worker().run(job["id"])
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertFalse(self.slots.tokens)

    def test_explicit_safe_resume_only_processes_remaining_groups_and_refreshes_same_history(self):
        job = self.make_job(max_requests=1)
        first = self.worker().run(job["id"])
        self.assertEqual(first["success_rows"], 10)
        resumed = self.store.resume_job(job["id"], max_requests=3)
        self.assertIsNone(resumed["batch_id"])
        result = self.worker().run(job["id"])
        self.assertEqual((result["status"], result["success_rows"], result["actual_requests"]),
                         ("completed", 25, 3))
        self.assertEqual(result["batch_id"], first["batch_id"])
        self.assertEqual(self.client.chat.completions.create.call_count, 3)
        self.assertEqual(self.history.load_batch(result["batch_id"])["success_rows"], 25)

    def test_outbox_keeps_failed_publication_and_only_sends_job_identifier(self):
        job = self.make_job()
        broker = Mock()
        broker.send_task.side_effect = ConnectionError("offline")
        first = dispatch_pending(self.store, celery=broker)
        self.assertEqual(first, {"published": 0, "deferred": 1})
        self.assertEqual(len(self.store.pending_messages()), 1)
        broker.send_task.side_effect = None
        second = dispatch_pending(self.store, celery=broker)
        self.assertEqual(second, {"published": 1, "deferred": 0})
        self.assertEqual(broker.send_task.call_args.kwargs["args"], [job["id"]])
        self.assertFalse(self.store.pending_messages())

    def test_celery_task_return_never_exposes_snapshot_in_success_logs(self):
        from backend.celery_app import celery_app, process_job
        private = {"id": "a" * 32, "status": "completed", "actual_requests": 2,
                   "config": {"frame_snapshot": "PRIVATE ORIGINAL"},
                   "groups": [{"rows": ["PRIVATE ORIGINAL"]}]}
        worker = Mock()
        worker.run.return_value = private
        with patch("backend.job_worker.default_worker", return_value=worker):
            public = process_job.run(private["id"])
        self.assertEqual(public, {"job_id": "a" * 32, "status": "completed", "request_count": 2})
        self.assertEqual(celery_app.conf.accept_content, ["json"])
        self.assertTrue(celery_app.conf.task_ignore_result)


if __name__ == "__main__":
    unittest.main()
