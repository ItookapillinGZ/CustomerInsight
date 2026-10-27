"""Offline coverage of queued UI submission, reopening, and safe HTTP calls."""

from contextlib import ExitStack, nullcontext
from io import BytesIO
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from streamlit.testing.v1 import AppTest

from backend.client import JobApiClient, JobClientError
from backend.job_store import JobStore
from core.csv_io import prepare_feedback_rows, read_feedback_csv, results_to_dataframe
from core.customer_report import build_customer_report
from core.history_store import HistoryStore
from tests.test_customer_app import APP_PATH, successful_rows, uploaded_csv


JOB_ID = "a" * 32


def job(status="queued", **updates):
    return {"id": JOB_ID, "status": status, "filename": "评价.csv", "total_rows": 2,
            "completed_rows": 0, "success_rows": 0, "batch_id": None,
            "request_count": 0, "max_requests": 1, "can_resume": False, "cached": False,
            **updates}


class JobApiClientTests(unittest.TestCase):
    def test_server_token_upload_purpose_and_submission_key(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(202, json=job())

        client = JobApiClient("http://api:8000", "private-token", transport=httpx.MockTransport(respond))
        client.upload("评价.csv", b"review\ngood\n")
        client.create_job({"allow_model_call": True}, idempotency_key="b" * 32)
        self.assertEqual(requests[0].headers["authorization"], "Bearer private-token")
        self.assertIn(b'name="purpose"', requests[0].content)
        self.assertIn(b"job", requests[0].content)
        self.assertIn(b'name="encoding"', requests[0].content)
        self.assertIn(b"utf-8-sig", requests[0].content)
        self.assertEqual(requests[1].url.path, "/api/v1/jobs")
        self.assertIn(b'"idempotency_key":"' + b"b" * 32, requests[1].content)

    def test_gb18030_upload_preserves_encoding_contract_and_original_bytes(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(201, json={"upload_id": "b" * 32})

        raw = "评价\n包装很好\n".encode("gb18030")
        client = JobApiClient("http://api:8000", "token", transport=httpx.MockTransport(respond))
        client.upload("评价.csv", raw, encoding="gb18030")
        self.assertIn(b'name="encoding"', requests[0].content)
        self.assertIn(b"gb18030", requests[0].content)
        self.assertIn(raw, requests[0].content)

    def test_lost_mutation_response_is_never_retried_or_leaked(self):
        calls = []

        def fail(request):
            calls.append(request)
            raise httpx.ReadTimeout("private-token private-table", request=request)

        client = JobApiClient("http://api:8000", "private-token", transport=httpx.MockTransport(fail))
        with self.assertRaises(JobClientError) as raised:
            client.create_job({}, idempotency_key="b" * 32)
        self.assertEqual(len(calls), 1)
        self.assertIn("不会自动重复提交", str(raised.exception))
        self.assertNotIn("private-token", str(raised.exception))
        self.assertNotIn("private-table", str(raised.exception))

    def test_error_body_and_redirect_never_expose_or_forward_credentials(self):
        requests = []

        def redirect(request):
            requests.append(request)
            return httpx.Response(307, headers={"Location": "http://elsewhere.invalid"},
                                  json={"error": "private-token secret review"})

        client = JobApiClient("http://api:8000", "private-token", transport=httpx.MockTransport(redirect))
        with self.assertRaises(JobClientError) as raised:
            client.list_jobs()
        self.assertEqual(len(requests), 1)
        self.assertNotIn("private-token", str(raised.exception))
        self.assertNotIn("secret review", str(raised.exception))

    def test_resume_requires_explicit_total_request_cap(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(202, json=job())

        client = JobApiClient("http://api:8000", "token", transport=httpx.MockTransport(respond))
        client.resume_job(JOB_ID, max_requests=3)
        self.assertEqual(requests[0].url.path, f"/api/v1/jobs/{JOB_ID}/resume")
        self.assertEqual(requests[0].content, b'{"allow_model_call":true,"max_requests":3}')

    def test_invalid_configuration_and_identifiers_do_not_send_requests(self):
        for address in ("file:///tmp/db", "http://user:secret@api:8000", "http://api:8000?secret=1"):
            with self.subTest(address=address), self.assertRaises(JobClientError):
                JobApiClient(address, "token")
        with self.assertRaises(JobClientError):
            JobApiClient("http://api:8000", "")
        client = JobApiClient("http://api:8000", "token")
        with self.assertRaises(JobClientError):
            client.get_job("../../private")


class QueuedJobUiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict("os.environ", {
            "APP_DATA_DIR": directory, "APP_ANALYSIS_MODE": "queued",
            "APP_API_URL": "http://api:8000", "APP_API_TOKEN": "test-token",
        }))
        self.stack.enter_context(patch("streamlit.delta_generator._maybe_print_use_warning"))
        self.stack.enter_context(patch("core.product_service.requires_access_password", return_value=False))
        self.stack.enter_context(patch("core.product_service.get_service_status", return_value={
            "configured": True, "remaining_today": 50, "daily_limit": 50,
            "request_batch_size": 10, "max_batch_characters": 12000,
        }))
        self.stack.enter_context(patch("core.product_service.estimate_customer_requests", return_value=1))
        self.analyzer = self.stack.enter_context(patch("core.product_service.analyze_customer_rows"))
        self.client = Mock(spec=JobApiClient)
        self.client.list_jobs.return_value = {"items": [], "total": 0}
        self.client.upload.return_value = {"upload_id": "b" * 32}
        self.client.create_job.return_value = job()
        self.client.get_job.return_value = job("running")
        self.stack.enter_context(patch("backend.client.JobApiClient.from_environment", return_value=self.client))
        self.upload = uploaded_csv()
        self.stack.enter_context(patch("streamlit.file_uploader", side_effect=lambda *args, **kwargs: self.upload))

    def app(self):
        result = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        self.assertEqual(len(result.exception), 0)
        return result

    def analyze(self, app):
        app.checkbox(key="customer_consent").check().run()
        app.button(key="customer_analyze").click().run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_explicit_submission_once_then_progress_reads_only(self):
        app = self.app()
        self.client.upload.assert_not_called()
        self.client.create_job.assert_not_called()
        self.analyze(app)
        self.assertEqual(app.session_state["customer_active_job_id"], JOB_ID)
        self.assertTrue(app.button(key="customer_analyze").disabled)
        self.client.upload.assert_called_once()
        self.client.create_job.assert_called_once()
        payload = self.client.create_job.call_args.args[0]
        self.assertTrue(payload["allow_model_call"])
        self.assertEqual(payload["max_requests"], 1)
        self.assertEqual(payload["text_column"], "评价")
        app.run()
        app.button(key="customer_job_refresh").click().run()
        self.assertEqual(len(app.exception), 0)
        self.client.create_job.assert_called_once()
        self.analyzer.assert_not_called()

    def test_uncertain_submission_reuses_key_on_explicit_retry(self):
        self.client.create_job.side_effect = [JobClientError("未能确认提交，不会自动重复提交。"), job()]
        app = self.analyze(self.app())
        first_key = self.client.create_job.call_args.kwargs["idempotency_key"]
        app.run()
        self.assertEqual(self.client.create_job.call_count, 1)
        app.button(key="customer_analyze").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.client.create_job.call_count, 2)
        self.assertEqual(self.client.create_job.call_args.kwargs["idempotency_key"], first_key)
        self.client.upload.assert_called_once()
        self.analyzer.assert_not_called()

    def test_reopen_existing_job_without_upload_or_model_call(self):
        self.upload = None
        self.client.list_jobs.return_value = {"items": [job()], "total": 1}
        app = self.app()
        self.client.get_job.assert_not_called()
        app.button(key="customer_job_open").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["customer_active_job_id"], JOB_ID)
        self.client.get_job.assert_called_once_with(JOB_ID)
        self.client.upload.assert_not_called()
        self.client.create_job.assert_not_called()
        self.analyzer.assert_not_called()

    def test_cancel_is_explicit_and_does_not_resubmit(self):
        app = self.analyze(self.app())
        self.client.get_job.side_effect = [job("running"), job("cancelled")]
        app.button(key=f"customer_job_cancel_{JOB_ID}").click().run()
        self.assertEqual(len(app.exception), 0)
        self.client.cancel_job.assert_called_once_with(JOB_ID)
        self.client.create_job.assert_called_once()

    def test_uncertain_paid_call_cannot_resume_automatically(self):
        self.client.get_job.return_value = job("needs_review", request_count=1)
        app = self.analyze(self.app())
        self.assertTrue(any("重复计费" in warning.value for warning in app.warning))
        self.assertFalse(any(button.label == "继续未完成的分析" for button in app.button))
        self.client.resume_job.assert_not_called()
        self.analyzer.assert_not_called()

    def test_resume_requires_second_explicit_consent(self):
        self.client.get_job.return_value = job("partial", can_resume=True, completed_rows=1)
        app = self.analyze(self.app())
        self.assertTrue(app.button(key=f"customer_job_resume_{JOB_ID}").disabled)
        self.client.resume_job.assert_not_called()
        app.checkbox(key=f"customer_job_resume_consent_{JOB_ID}").check().run()
        self.client.get_job.side_effect = [job("partial", can_resume=True, completed_rows=1), job("queued")]
        app.button(key=f"customer_job_resume_{JOB_ID}").click().run()
        self.assertEqual(len(app.exception), 0)
        self.client.resume_job.assert_called_once_with(JOB_ID, max_requests=1)
        self.client.create_job.assert_called_once()

    def test_completed_job_reopens_shared_history_report(self):
        frame = read_feedback_csv(self.upload.getvalue(), filename=self.upload.name)
        rows = successful_rows(prepare_feedback_rows(frame, "评价"))
        report = build_customer_report(frame, results_to_dataframe(rows), "评价")
        batch_id = HistoryStore().save_batch(report, filename=self.upload.name,
            file_bytes=self.upload.getvalue(), mapping={"text_column": "评价"})
        self.upload = None
        self.client.list_jobs.return_value = {"items": [job("completed", batch_id=batch_id)], "total": 1}
        self.client.get_job.return_value = job("completed", batch_id=batch_id, completed_rows=2, success_rows=2)
        app = self.app()
        app.button(key="customer_job_open").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["customer_active_batch_id"], batch_id)
        self.assertEqual(app.session_state["customer_report"]["metrics"]["success_rows"], 2)
        self.assertNotIn("customer_active_job_id", app.session_state)
        self.assertEqual([tab.label for tab in app.tabs], ["分析结果", "问 Agent", "明细与下载"])
        self.client.create_job.assert_not_called()
        self.analyzer.assert_not_called()

    def test_large_queued_table_is_validated_locally_without_a_model_call(self):
        self.upload = uploaded_csv("评价\n" + "\n".join(f"顾客评价{index}" for index in range(500)) + "\n")
        app = self.app()
        self.assertTrue(any("500 条可分析评价" in item.value for item in app.info))
        self.assertFalse(app.error)
        self.client.upload.assert_not_called()
        self.client.create_job.assert_not_called()
        self.analyzer.assert_not_called()

    def test_gb18030_selection_is_sent_to_upload_and_job_creation(self):
        self.upload = BytesIO("产品,评价\n杯子,包装很好\n".encode("gb18030"))
        self.upload.name = "评价.csv"
        app = self.app()
        app.selectbox(key="customer_csv_encoding").select("GB18030 / GBK").run()
        self.analyze(app)
        self.client.upload.assert_called_once_with(self.upload.name, self.upload.getvalue(), encoding="gb18030")
        self.assertEqual(self.client.create_job.call_args.args[0]["encoding"], "gb18030")
        self.analyzer.assert_not_called()

    def _save_report(self):
        frame = read_feedback_csv(self.upload.getvalue(), filename=self.upload.name)
        rows = successful_rows(prepare_feedback_rows(frame, "评价"))
        report = build_customer_report(frame, results_to_dataframe(rows), "评价")
        store = HistoryStore()
        batch_id = store.save_batch(report, filename=self.upload.name,
            file_bytes=self.upload.getvalue(), mapping={"text_column": "评价"})
        self.upload = None
        return store, batch_id

    def test_related_job_protects_history_deletion_even_in_direct_mode(self):
        store, batch_id = self._save_report()
        JobStore(store.root_dir)
        related = self.stack.enter_context(patch("backend.job_store.JobStore.has_related_jobs", return_value=True))
        self.stack.enter_context(patch.dict("os.environ", {"APP_ANALYSIS_MODE": "direct"}))
        app = self.app()
        self.assertTrue(app.checkbox(key=f"customer_delete_confirm_{batch_id}").disabled)
        self.assertTrue(app.button(key="customer_delete_history").disabled)
        self.assertTrue(any("后台任务原件和报告需一起清理" in element.value for element in app.info))
        related.assert_called_with(batch_id, store.list_batches()[0]["content_hash"])
        self.assertEqual(len(store.list_batches()), 1)
        self.client.create_job.assert_not_called()
        self.analyzer.assert_not_called()

    def test_legacy_history_still_deletes_and_backup_scope_is_explicit(self):
        store, batch_id = self._save_report()
        self.stack.enter_context(patch.dict("os.environ", {"APP_ANALYSIS_MODE": "direct"}))
        app = self.app()
        self.assertEqual(app.button(key="customer_prepare_backup").label, "准备历史报告备份")
        self.assertTrue(any("不包括后台任务数据库与额度台账" in element.value for element in app.caption))
        app.checkbox(key=f"customer_delete_confirm_{batch_id}").check().run()
        self.assertFalse(app.button(key="customer_delete_history").disabled)
        app.button(key="customer_delete_history").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(store.list_batches())
        self.assertFalse((store.root_dir / "analysis_jobs.sqlite3").exists())
        self.analyzer.assert_not_called()

    def test_delete_rechecks_under_job_guard_before_removing_history(self):
        store, batch_id = self._save_report()
        content_hash = store.list_batches()[0]["content_hash"]
        guard = self.stack.enter_context(patch("backend.job_store.JobStore.history_delete_guard",
                                              return_value=nullcontext(False)))
        app = self.app()
        app.checkbox(key=f"customer_delete_confirm_{batch_id}").check().run()
        self.assertFalse(app.button(key="customer_delete_history").disabled)
        app.button(key="customer_delete_history").click().run()
        self.assertEqual(len(app.exception), 0)
        guard.assert_called_once_with(batch_id, original_hash=content_hash)
        self.assertEqual(len(store.list_batches()), 1)
        self.assertTrue(any("后台任务原件和报告需一起清理" in element.value for element in app.warning))
        self.client.create_job.assert_not_called()
        self.analyzer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
