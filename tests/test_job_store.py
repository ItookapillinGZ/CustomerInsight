"""Fault-oriented repository tests; all model results are fixed offline data."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from backend.job_store import JobBudgetError, JobConflictError, JobStore, JobStoreError


def row(index, *, success=False):
    result = {"feedback_id": f"feedback-{index}", "source_row": index + 2,
              "original_text": f"Synthetic offline review {index}",
              "processing_status": "success" if success else "pending"}
    if success:
        result.update(sentiment="positive", category="product_quality", priority="low",
                      summary="Synthetic fixture", aspects=[], latency_ms=0)
    return result


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.store = JobStore(self.temp.name, clock=lambda: self.now)

    def create(self, *, count=2, max_requests=10, **overrides):
        rows = [row(index) for index in range(count)]
        values = {"original": b"feedback\nsynthetic", "filename": "reviews.csv",
                  "request": {"upload_id": "a" * 32, "text_column": "feedback"},
                  "config": {"model": "offline", "prompt_version": "v1"},
                  "groups": [[value] for value in rows], "initial_results": rows,
                  "max_requests": max_requests}
        values.update(overrides)
        return self.store.create_job(**values)

    def claim(self, job):
        claimed = self.store.claim_group(job["id"], "offline-worker", lease_seconds=10)
        self.assertIsNotNone(claimed)
        return claimed

    def complete(self, job, group, usage=None):
        self.store.mark_sent(job["id"], group["id"], group["lease_token"])
        results = [dict(item, processing_status="success", sentiment="positive",
                        category="product_quality", priority="low", summary="Fixture")
                   for item in group["rows"]]
        return self.store.checkpoint_group(job["id"], group["id"], group["lease_token"],
                                           results=results, usage=usage)

    def test_snapshot_and_outbox_survive_repository_restart(self):
        job, created = self.create()
        self.assertTrue(created)
        reopened = JobStore(self.temp.name)
        self.assertEqual(reopened.original(job["id"]), b"feedback\nsynthetic")
        self.assertEqual(reopened.get_job(job["id"])["config"]["model"], "offline")
        self.assertEqual(len(reopened.get_results(job["id"])), 2)
        messages = reopened.pending_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], {"job_id": job["id"]})

    def test_file_mapping_model_and_prompt_scope_deduplication(self):
        job, _ = self.create()
        same, created = self.create(request={"upload_id": "b" * 32, "text_column": "feedback"})
        self.assertFalse(created)
        self.assertEqual(same["id"], job["id"])
        for values in ({"original": b"changed"}, {"request": {"text_column": "other"}},
                       {"config": {"model": "changed", "prompt_version": "v1"}},
                       {"config": {"model": "offline", "prompt_version": "v2"}},
                       {"scope": "different-workspace"}):
            changed, created = self.create(**values)
            self.assertTrue(created)
            self.assertNotEqual(changed["id"], job["id"])

    def test_idempotency_key_cannot_be_reused_for_different_input(self):
        job, _ = self.create(idempotency_key="safe-key")
        repeated, created = self.create(idempotency_key="safe-key", force=True)
        self.assertFalse(created)
        self.assertEqual(job["id"], repeated["id"])
        with self.assertRaises(JobConflictError):
            self.create(idempotency_key="safe-key", original=b"different")

    def test_duplicate_reuses_latest_forced_generation_even_when_clocks_match(self):
        self.create()
        newer, _ = self.create(force=True)
        reused, created = self.create()
        self.assertFalse(created)
        self.assertEqual(reused["id"], newer["id"])

    def test_atomic_concurrent_create_and_single_group_claim(self):
        def create_one(_):
            return self.create(count=1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(create_one, range(6)))
        self.assertEqual(sum(created for _, created in jobs), 1)
        job = jobs[0][0]
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda index: self.store.claim_group(job["id"], f"worker-{index}"), range(6)))
        self.assertEqual(sum(item is not None for item in claims), 1)

    def test_completed_groups_never_claim_again_and_report_link_is_stable(self):
        job, _ = self.create(count=1)
        result = self.complete(job, self.claim(job), {"total_tokens": 17})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["success_rows"], 1)
        self.assertEqual(result["completed_rows"], 1)
        self.assertFalse(result["can_resume"])
        self.assertEqual(result["usage"], {"total_tokens": 17})
        self.assertIsNone(self.store.claim_group(job["id"], "other-worker"))
        self.store.set_report(job["id"], "a" * 32)
        self.store.set_report(job["id"], "a" * 32)
        with self.assertRaises(JobConflictError):
            self.store.set_report(job["id"], "b" * 32)

    def test_expired_unsent_lease_is_safe_but_stale_worker_is_fenced(self):
        job, _ = self.create(count=1)
        old = self.claim(job)
        self.now += 11
        self.assertEqual(self.store.expire_leases(job["id"]), 1)
        new = self.claim(job)
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaises(JobConflictError):
            self.store.mark_sent(job["id"], old["id"], old["lease_token"])
        self.assertEqual(self.store.get_job(job["id"])["request_count"], 0)
        self.complete(job, new)

    def test_expired_sent_lease_is_unknown_and_never_automatically_resent(self):
        job, _ = self.create()
        group = self.claim(job)
        self.store.mark_sent(job["id"], group["id"], group["lease_token"])
        self.now += 11
        self.store.expire_leases(job["id"])
        current = self.store.get_job(job["id"])
        self.assertEqual(current["status"], "needs_review")
        self.assertFalse(current["can_resume"])
        self.assertEqual(self.store.attempts(job["id"])[0]["status"], "uncertain")
        self.assertIsNone(self.store.claim_group(job["id"], "new-worker"))
        with self.assertRaises(JobConflictError):
            self.store.resume_job(job["id"])
        with self.assertRaises(JobConflictError):
            self.store.checkpoint_group(job["id"], group["id"], group["lease_token"], results=[])

    def test_heartbeat_extends_fencing_lease(self):
        job, _ = self.create(count=1)
        group = self.claim(job)
        self.now += 9
        self.store.renew_lease(job["id"], group["id"], group["lease_token"], lease_seconds=10)
        self.now += 5
        self.assertEqual(self.store.expire_leases(job["id"]), 0)
        self.complete(job, group)

    def test_request_budget_is_atomic_even_with_two_claimed_groups(self):
        job, _ = self.create(max_requests=1)
        groups = [self.claim(job), self.claim(job)]
        def reserve(group):
            try:
                self.store.mark_sent(job["id"], group["id"], group["lease_token"])
                return True
            except JobBudgetError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, groups))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(self.store.get_job(job["id"])["request_count"], 1)
        sent = groups[outcomes.index(True)]
        with self.assertRaises(JobConflictError):
            self.store.mark_sent(job["id"], sent["id"], sent["lease_token"])

    def test_budget_exhaustion_keeps_checkpoint_and_only_explicit_raise_resumes(self):
        job, _ = self.create(max_requests=1)
        current = self.complete(job, self.claim(job))
        self.assertEqual(current["status"], "partial")
        self.assertEqual(current["error_code"], "request_budget_exhausted")
        self.assertTrue(current["can_resume"])
        self.assertIsNone(self.store.claim_group(job["id"], "new-worker"))
        self.assertEqual(self.store.resume_job(job["id"])["status"], "partial")
        self.store.resume_job(job["id"], max_requests=2)
        self.assertEqual(self.complete(job, self.claim(job))["status"], "completed")

    def test_cancel_fences_unsent_but_allows_sent_response_to_checkpoint(self):
        job, _ = self.create(count=3)
        sent, unsent = self.claim(job), self.claim(job)
        self.store.mark_sent(job["id"], sent["id"], sent["lease_token"])
        self.store.cancel_job(job["id"])
        with self.assertRaises(JobConflictError):
            self.store.mark_sent(job["id"], unsent["id"], unsent["lease_token"])
        self.assertIsNone(self.store.claim_group(job["id"], "other-worker"))
        response = [dict(sent["rows"][0], processing_status="success")]
        result = self.store.checkpoint_group(job["id"], sent["id"], sent["lease_token"], results=response)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["request_count"], 1)
        self.store.resume_job(job["id"])
        self.complete(job, self.claim(job))
        result = self.complete(job, self.claim(job))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["request_count"], 3)

    def test_reported_usage_survives_json_validation_failure(self):
        job, _ = self.create(count=1)
        group = self.claim(job)
        self.store.mark_sent(job["id"], group["id"], group["lease_token"])
        self.store.checkpoint_usage(job["id"], group["id"], group["lease_token"], {"total_tokens": 83})
        self.store.checkpoint_group(job["id"], group["id"], group["lease_token"], results=[], error="invalid response")
        self.assertEqual(self.store.get_job(job["id"])["usage"], {"total_tokens": 83})
        self.assertEqual(self.store.attempts(job["id"])[0]["usage"], {"total_tokens": 83})
        self.assertIsNone(self.store.claim_group(job["id"], "other"))

    def test_checkpoint_rejects_missing_duplicate_or_foreign_rows(self):
        job, _ = self.create(count=1)
        group = self.claim(job)
        self.store.mark_sent(job["id"], group["id"], group["lease_token"])
        for results in ([], [row(20)], [row(0), row(0)]):
            with self.assertRaises(JobStoreError):
                self.store.checkpoint_group(job["id"], group["id"], group["lease_token"], results=results)
        self.assertEqual(self.store.get_results(job["id"])[0]["processing_status"], "pending")

    def test_dispatch_failure_and_lost_published_notification_are_recoverable(self):
        job, _ = self.create()
        message = self.store.pending_messages()[0]
        self.store.fail_message(message["id"], error="secret must not persist")
        self.assertNotIn("secret", str(self.store.pending_messages()))
        self.store.acknowledge_message(message["id"])
        self.assertEqual(self.store.pending_messages(), [])
        self.now += 59
        self.assertEqual(self.store.recover_dispatches(), 0)
        self.now += 2
        self.assertEqual(self.store.recover_dispatches(), 1)
        self.assertEqual(self.store.pending_messages()[0]["job_id"], job["id"])

    def test_daily_quota_pause_requires_explicit_resume(self):
        job, _ = self.create()
        group = self.claim(job)
        self.store.record_failure(job["id"], group["id"], group["lease_token"], "quota reached")
        self.store.pause_job(job["id"], "daily_budget_exhausted")
        self.assertEqual(self.store.get_job(job["id"])["status"], "failed")
        self.assertIsNone(self.store.claim_group(job["id"], "other"))
        message = self.store.pending_messages()[0]
        self.store.acknowledge_message(message["id"])
        self.store.set_report(job["id"], "d" * 32)
        self.now += 61
        self.assertEqual(self.store.recover_dispatches(), 0)
        self.store.resume_job(job["id"])
        self.assertIsNotNone(self.claim(job))

    def test_finished_job_without_history_link_is_recovered_for_local_finalization(self):
        job, _ = self.create(count=1)
        self.complete(job, self.claim(job))
        self.store.acknowledge_message(self.store.pending_messages()[0]["id"])
        self.now += 61
        self.assertEqual(self.store.recover_dispatches(), 1)
        self.assertIsNone(self.store.claim_group(job["id"], "recovery"))
        self.assertEqual(self.store.get_job(job["id"])["request_count"], 1)
        self.store.set_report(job["id"], "e" * 32)
        self.store.acknowledge_message(self.store.pending_messages()[0]["id"])
        self.now += 61
        self.assertEqual(self.store.recover_dispatches(), 0)

    def test_explicit_resume_clears_stale_report_link_only_when_progress_possible(self):
        job, _ = self.create(max_requests=1)
        self.complete(job, self.claim(job))
        self.store.set_report(job["id"], "f" * 32)
        self.assertEqual(self.store.resume_job(job["id"])["batch_id"], "f" * 32)
        resumed = self.store.resume_job(job["id"], max_requests=2)
        self.assertIsNone(resumed["batch_id"])
        self.assertEqual(resumed["status"], "running")

    def test_old_unknown_job_cannot_export_over_newer_forced_generation(self):
        old, _ = self.create(count=1)
        group = self.claim(old)
        self.store.mark_sent(old["id"], group["id"], group["lease_token"])
        self.store.record_failure(old["id"], group["id"], group["lease_token"], uncertain=True)
        newer, _ = self.create(count=1, force=True)
        self.complete(newer, self.claim(newer))
        with self.store.report_guard(newer["id"]) as allowed:
            self.assertTrue(allowed)
            self.store.set_report(newer["id"], "a" * 32)
        with self.store.report_guard(old["id"]) as allowed:
            self.assertFalse(allowed)
        previous = self.store.get_job(old["id"])
        self.assertEqual(previous["report_superseded_by"], newer["id"])
        self.assertEqual(previous["status"], "needs_review")
        self.assertIsNone(previous["batch_id"])
        for message in self.store.pending_messages():
            self.store.acknowledge_message(message["id"])
        self.now += 61
        self.assertEqual(self.store.recover_dispatches(), 0)

    def test_two_finalizers_export_once_under_the_report_guard(self):
        job, _ = self.create(count=1)
        self.complete(job, self.claim(job))
        def finalize(_):
            with self.store.report_guard(job["id"]) as allowed:
                if allowed:
                    self.store.set_report(job["id"], "b" * 32)
                return allowed
        with ThreadPoolExecutor(max_workers=2) as pool:
            allowed = list(pool.map(finalize, range(2)))
        self.assertEqual(allowed.count(True), 1)
        self.assertEqual(self.store.get_job(job["id"])["batch_id"], "b" * 32)

    def test_forced_creation_waits_until_local_report_export_finishes(self):
        job, _ = self.create(count=1)
        self.complete(job, self.claim(job))
        started = threading.Event()
        def create_forced():
            started.set()
            return self.create(count=1, force=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.report_guard(job["id"]) as allowed:
                self.assertTrue(allowed)
                future = pool.submit(create_forced)
                self.assertTrue(started.wait(timeout=3))
                self.assertFalse(future.done())
                self.store.set_report(job["id"], "c" * 32)
            newer, created = future.result(timeout=5)
        self.assertTrue(created)
        self.assertNotEqual(newer["id"], job["id"])

    def test_older_partial_job_cannot_resume_after_newer_generation_exists(self):
        job, _ = self.create(max_requests=1)
        self.complete(job, self.claim(job))
        self.create(force=True)
        with self.assertRaises(JobConflictError):
            self.store.resume_job(job["id"], max_requests=10)
        self.assertEqual(self.store.get_job(job["id"])["request_count"], 1)

    def test_partial_group_preserves_successful_rows_and_partial_job_status(self):
        rows = [row(0), row(1)]
        job, _ = self.create(groups=[rows])
        group = self.claim(job)
        self.store.mark_sent(job["id"], group["id"], group["lease_token"])
        results = [row(0, success=True), dict(row(1), processing_status="validation_error")]
        current = self.store.checkpoint_group(job["id"], group["id"], group["lease_token"],
                                              results=results, error="partially invalid response")
        self.assertEqual(current["status"], "partial")
        self.assertEqual(current["success_rows"], 1)
        self.assertEqual(current["completed_groups"], 0)
        self.assertEqual(current["completed_rows"], 2)

    def test_history_reference_checks_linked_and_unexported_original_snapshots(self):
        job, _ = self.create()
        original_hash = hashlib.sha256(b"feedback\nsynthetic").hexdigest()
        batch_id, unrelated = "a" * 32, "b" * 32
        self.assertFalse(self.store.has_related_jobs(batch_id))
        self.assertTrue(self.store.has_related_jobs(batch_id, original_hash))
        with self.store.history_delete_guard(batch_id, original_hash) as allowed:
            self.assertFalse(allowed)
        self.store.set_report(job["id"], batch_id)
        self.assertTrue(self.store.has_related_jobs(batch_id))
        self.assertFalse(self.store.has_related_jobs(unrelated))

    def test_history_delete_guard_serializes_new_snapshot_creation(self):
        batch_id = "c" * 32
        original_hash = hashlib.sha256(b"feedback\nsynthetic").hexdigest()
        started = threading.Event()
        def submit():
            started.set()
            return self.create()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store.history_delete_guard(batch_id, original_hash) as allowed:
                self.assertTrue(allowed)
                self.assertFalse(self.store.has_related_jobs(batch_id, original_hash))
                future = pool.submit(submit)
                self.assertTrue(started.wait(timeout=3))
                self.assertFalse(future.done())
                # A local HistoryStore.delete_batch call belongs here. The
                # new task cannot snapshot or export until this section ends.
            _, created = future.result(timeout=5)
        self.assertTrue(created)
        self.assertTrue(self.store.has_related_jobs(batch_id, original_hash))

    def test_all_500_rows_checkpoint_and_survive_reopen_without_model_calls(self):
        rows = [row(index) for index in range(500)]
        job, _ = self.create(count=0, groups=[rows[index:index + 10] for index in range(0, 500, 10)],
                             initial_results=rows, max_requests=50)
        for _ in range(50):
            self.complete(job, self.claim(job), {"total_tokens": 10})
        reopened = JobStore(self.temp.name)
        current = reopened.get_job(job["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual((current["total_rows"], current["completed_rows"], current["success_rows"]), (500, 500, 500))
        self.assertEqual((current["request_count"], current["usage"]["total_tokens"]), (50, 500))
        self.assertEqual(len(reopened.get_results(job["id"])), 500)


if __name__ == "__main__":
    unittest.main()
