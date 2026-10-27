"""The durable job contract on SQLAlchemy plus tenant isolation regressions.

The default test engine is a temporary SQLite database with foreign keys on.
Set TEST_PG_URL to run the same suite against an isolated PostgreSQL database;
its schema is dropped and recreated, so deployment databases are forbidden.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import threading
import unittest
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import backend.accounts  # register account/workspace tables in shared metadata
from backend.database import Base, get_engine
from backend.job_store import JobConflictError, JobNotFoundError, JobStoreError
from backend.pg_jobs import PgJobStore
from tests import test_job_store as legacy


class PgJobContractTests(legacy.JobStoreTests):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        test_url = os.getenv("TEST_PG_URL")
        if test_url and ("phase3" not in test_url and "test" not in test_url):
            raise RuntimeError("TEST_PG_URL must target an isolated test database")
        self.engine = get_engine(test_url or f"sqlite:///{Path(self.temp.name) / 'jobs.sqlite3'}")
        self.addCleanup(self.engine.dispose)
        # Dedicated PG schema avoids destroying any other test subsystem.
        if self.engine.dialect.name == "postgresql":
            self.schema = "jobs_test_" + uuid4().hex
            with self.engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            self.engine = self.engine.execution_options(schema_translate_map={None: self.schema})
            # Raw state-machine SQL is intentionally unqualified; setting the
            # per-connection search path keeps it inside this test schema.
            from sqlalchemy import event
            @event.listens_for(self.engine, "begin")
            def set_search_path(conn):
                conn.exec_driver_sql(f'SET LOCAL search_path TO "{self.schema}"')
            self.addCleanup(self._drop_schema)
        Base.metadata.create_all(self.engine)
        self.now = 1000.0
        self.workspace_a, self.workspace_b = "a" * 32, "b" * 32
        with self.engine.begin() as conn:
            conn.execute(backend.accounts.Workspace.__table__.insert(), [
                {"id": self.workspace_a, "created_at": self.now},
                {"id": self.workspace_b, "created_at": self.now},
            ])
        self.store = self.make_store(self.workspace_a)
        self.other = self.make_store(self.workspace_b)
        self.global_store = self.make_store(None)

    def _drop_schema(self):
        with self.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def make_store(self, workspace_id):
        return PgJobStore(engine=self.engine, workspace_id=workspace_id,
                          data_dir=self.temp.name, clock=lambda: self.now)

    def create(self, **overrides):
        target = self.store
        if overrides.get("scope") == "different-workspace":
            overrides["scope"] = self.workspace_b
            target = self.other
        previous = self.store
        try:
            self.store = target
            return super().create(**overrides)
        finally:
            self.store = previous

    def test_snapshot_and_outbox_survive_repository_restart(self):
        job, created = self.create()
        self.assertTrue(created)
        reopened = self.make_store(self.workspace_a)
        self.assertEqual(reopened.original(job["id"]), b"feedback\nsynthetic")
        self.assertEqual(reopened.get_job(job["id"])["config"]["model"], "offline")
        self.assertEqual(len(reopened.get_results(job["id"])), 2)
        self.assertEqual(reopened.pending_messages()[0]["payload"], {"job_id": job["id"]})

    def test_all_500_rows_checkpoint_and_survive_reopen_without_model_calls(self):
        rows = [legacy.row(index) for index in range(500)]
        job, _ = self.create(count=0, groups=[rows[index:index + 10] for index in range(0, 500, 10)],
                             initial_results=rows, max_requests=50)
        for _ in range(50):
            self.complete(job, self.claim(job), {"total_tokens": 10})
        reopened = self.make_store(self.workspace_a)
        current = reopened.get_job(job["id"])
        self.assertEqual(current["status"], "completed")
        self.assertEqual((current["total_rows"], current["completed_rows"], current["success_rows"]), (500, 500, 500))
        self.assertEqual((current["request_count"], current["usage"]["total_tokens"]), (50, 500))
        self.assertEqual(len(reopened.get_results(job["id"])), 500)

    def test_cross_workspace_job_ids_are_consistently_not_found(self):
        foreign, _ = self.create()
        group = self.claim(foreign)
        operations = [
            lambda: self.other.get_job(foreign["id"]),
            lambda: self.other.original(foreign["id"]),
            lambda: self.other.get_results(foreign["id"]),
            lambda: self.other.attempts(foreign["id"]),
            lambda: self.other.claim_group(foreign["id"], "untrusted"),
            lambda: self.other.cancel_job(foreign["id"]),
            lambda: self.other.resume_job(foreign["id"]),
            lambda: self.other.pause_job(foreign["id"]),
            lambda: self.other.enqueue_job(foreign["id"]),
            lambda: self.other.expire_leases(foreign["id"]),
            lambda: self.other.set_report(foreign["id"], "d" * 32),
            lambda: self.other.renew_lease(foreign["id"], group["id"], group["lease_token"]),
            lambda: self.other.mark_sent(foreign["id"], group["id"], group["lease_token"]),
            lambda: self.other.checkpoint_group(foreign["id"], group["id"], group["lease_token"], results=[]),
            lambda: self.other.record_failure(foreign["id"], group["id"], group["lease_token"]),
        ]
        for operation in operations:
            with self.subTest(operation=operations.index(operation)):
                with self.assertRaisesRegex(JobNotFoundError, "分析任务不存在"):
                    operation()
        with self.assertRaises(JobNotFoundError):
            with self.other.report_guard(foreign["id"]):
                self.fail("foreign guard must never yield")
        self.assertEqual(self.other.list_jobs()["total"], 0)
        self.assertEqual(self.other.pending_messages(), [])
        self.assertEqual(self.store.get_job(foreign["id"])["request_count"], 0)

    def test_same_input_and_idempotency_key_are_isolated_between_workspaces(self):
        first, _ = self.create(idempotency_key="identical-key")
        second, created = self.create(scope="different-workspace", idempotency_key="identical-key")
        self.assertTrue(created)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((self.store.list_jobs()["total"], self.other.list_jobs()["total"]), (1, 1))
        self.assertEqual(self.global_store.list_jobs()["total"], 2)
        self.assertEqual(self.global_store.get_job(second["id"])["scope"], self.workspace_b)

    def test_bound_creation_and_list_cannot_select_another_workspace(self):
        with self.assertRaises(JobNotFoundError):
            self.create(scope=self.workspace_b)
        with self.assertRaises(JobNotFoundError):
            self.store.list_jobs(scope=self.workspace_b)
        with self.assertRaises(JobStoreError):
            self.global_store.create_job(original=b"x", filename="x.csv", request={}, config={}, groups=[], max_requests=1)

    def test_bound_recovery_and_expiration_do_not_mutate_other_workspace(self):
        first, _ = self.create()
        second, _ = self.create(scope="different-workspace")
        first_group = self.store.claim_group(first["id"], "first", lease_seconds=10)
        self.other.claim_group(second["id"], "second", lease_seconds=10)
        self.store.mark_sent(first["id"], first_group["id"], first_group["lease_token"])
        for message in self.store.pending_messages():
            self.store.acknowledge_message(message["id"])
        for message in self.other.pending_messages():
            self.other.acknowledge_message(message["id"])
        self.now += 61
        self.other.recover_dispatches()
        self.assertEqual(self.store.get_job(first["id"])["groups"][0]["status"], "sent")
        self.assertEqual(self.other.get_job(second["id"])["groups"][0]["status"], "pending")
        self.assertEqual(self.store.pending_messages(), [])
        self.assertEqual(len(self.other.pending_messages()), 1)
        self.global_store.expire_leases()
        self.assertEqual(self.store.get_job(first["id"])["status"], "needs_review")

    def test_outbox_mutations_cannot_cross_workspaces(self):
        self.create()
        message = self.store.pending_messages()[0]
        for mutate in (self.other.acknowledge_message, self.other.fail_message):
            with self.assertRaises(JobNotFoundError):
                mutate(message["id"])
        self.assertEqual(self.store.pending_messages()[0]["attempts"], 0)

    def test_history_delete_guard_never_uses_another_workspace_references(self):
        job, _ = self.create()
        self.store.set_report(job["id"], "d" * 32)
        # Legacy batch IDs can coexist in two workspace-scoped history tables.
        # A foreign job must not block deleting an unrelated local record with
        # that same ID; PgHistoryStore validates local ownership separately.
        self.assertFalse(self.other.has_related_jobs("d" * 32))
        with self.other.history_delete_guard("d" * 32) as allowed:
            self.assertTrue(allowed)
        local, _ = self.create(scope="different-workspace")
        self.other.set_report(local["id"], "d" * 32)
        self.assertTrue(self.other.has_related_jobs("d" * 32))
        with self.other.history_delete_guard("d" * 32) as allowed:
            self.assertFalse(allowed)

    def test_relational_scope_foreign_keys_reject_cross_tenant_child_records(self):
        job, _ = self.create()
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as conn:
                conn.execute(text("""INSERT INTO job_outbox(id,job_id,scope,status,created_at)
                    VALUES(:id,:job,:scope,'pending',1000)"""),
                    {"id": "e" * 32, "job": job["id"], "scope": self.workspace_b})
        inspector = inspect(self.engine)
        for table in ("analysis_jobs", "job_idempotency", "analysis_groups", "analysis_results", "analysis_attempts", "job_outbox"):
            self.assertIn("scope", {column["name"] for column in inspector.get_columns(table)})
            self.assertTrue(any(fk["referred_table"] == "app_workspaces" for fk in inspector.get_foreign_keys(table)))

    def test_independent_repository_instances_race_under_postgres_advisory_lock(self):
        if self.engine.dialect.name != "postgresql":
            self.skipTest("independent process locking requires PostgreSQL")
        def create_one(_):
            repository = self.make_store(self.workspace_a)
            rows = [legacy.row(0)]
            return repository.create_job(original=b"feedback\nsynthetic", filename="reviews.csv",
                request={"text_column": "feedback"}, config={"model": "offline"}, groups=[rows],
                initial_results=rows, max_requests=1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            created = list(pool.map(create_one, range(6)))
        self.assertEqual(sum(item[1] for item in created), 1)
        job_id = created[0][0]["id"]
        def claim_one(index):
            return self.make_store(self.workspace_a).claim_group(job_id, f"worker-{index}")
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(claim_one, range(6)))
        self.assertEqual(sum(item is not None for item in claims), 1)


if __name__ == "__main__":
    unittest.main()
