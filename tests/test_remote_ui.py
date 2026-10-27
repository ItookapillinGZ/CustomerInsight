"""API-only UI boundaries, typed report transport and tenant access checks."""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import zipfile

import httpx
import pandas as pd
from streamlit.testing.v1 import AppTest

from backend.client import JobApiClient, JobClientError, api_ui_enabled, queued_analysis_enabled
from backend.ui_history import RemoteHistory, decode_view
from core.analysis_agent import report_fingerprint
from core.history_store import _pack, _unpack
from tests import test_account_api as api_fixtures
from tests import test_account_ui as ui_fixtures
from tests.test_customer_app import APP_PATH


class RemoteWorkspaceApiTests(unittest.TestCase):
    setUp = api_fixtures.AccountApiTests.setUp
    _drop_schema = api_fixtures.AccountApiTests._drop_schema
    login = api_fixtures.AccountApiTests.login
    headers = api_fixtures.AccountApiTests.headers
    request = api_fixtures.AccountApiTests.request
    error = api_fixtures.AccountApiTests.error
    upload = api_fixtures.AccountApiTests.upload
    job = api_fixtures.AccountApiTests.job
    save_batch = api_fixtures.AccountApiTests.save_batch
    chat_payload = api_fixtures.AccountApiTests.chat_payload

    def test_readiness_uses_explicit_factory_engine_and_workspace_directory(self):
        with patch("backend.operations.ReadinessProbe") as probe:
            probe.return_value.check.return_value = {"status": "ready", "checks": {"database": True}}
            response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        probe.assert_called_once_with(engine=self.engine, data_dir=self.app.state.service.root_dir)

    def test_remote_report_preserves_typed_frames_and_agent_fingerprint(self):
        batch_id, _ = self.save_batch()
        response = self.request("GET", f"/api/v1/batches/{batch_id}/view")
        self.assertEqual(response.status_code, 200, response.text)
        decoded = decode_view(response.json())
        self.assertIsInstance(decoded["report"]["detail"], pd.DataFrame)
        service, _ = self.app.state.tenants.for_workspace(self.alice["workspace_id"])
        expected = service._chat_context(batch_id, None)
        self.assertEqual(report_fingerprint(decoded["report"]), report_fingerprint(expected))
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_new_report_deletion_and_chat_endpoints_reject_foreign_batch(self):
        batch_id, _ = self.save_batch()
        for method, suffix in (("GET", "/view"), ("GET", "/deletion"), ("DELETE", ""), ("DELETE", "/chat")):
            with self.subTest(method=method, suffix=suffix):
                self.error(self.request(method, f"/api/v1/batches/{batch_id}{suffix}", token=self.bob_token), 404)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{batch_id}/view").status_code, 200)

    def test_comparison_scope_is_checked_for_view_and_chat_clear(self):
        own, _ = self.save_batch()
        foreign, _ = self.save_batch(self.bob, "foreign only")
        for method, suffix in (("GET", "/view"), ("DELETE", "/chat")):
            self.error(self.request(method, f"/api/v1/batches/{own}{suffix}",
                params={"comparison_id": foreign}), 404)
        self.agent.assert_not_called()

    def test_inspection_is_remote_no_model_and_foreign_upload_cannot_be_used(self):
        upload_id = self.upload()
        payload = {"text_column": "review", "group_column": "store", "date_column": "date"}
        response = self.request("POST", f"/api/v1/uploads/{upload_id}/inspect", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"], {"pending": 2})
        self.assertEqual(response.json()["expected_requests"], 1)
        self.error(self.request("POST", f"/api/v1/uploads/{upload_id}/inspect", json=payload, token=self.bob_token), 404)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_inspection_rejects_missing_or_overlapping_fields(self):
        upload_id = self.upload()
        for payload in ({"text_column": "missing"}, {"text_column": "review", "group_column": "review"}):
            self.error(self.request("POST", f"/api/v1/uploads/{upload_id}/inspect", json=payload), 422)

    def test_workspace_backup_contains_only_the_authenticated_users_data(self):
        alice, _ = self.save_batch(label="ALICE_PRIVATE_MARKER")
        bob, _ = self.save_batch(self.bob, "BOB_PRIVATE_MARKER")
        response = self.request("GET", "/api/v1/backup")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            payload = b"\n".join(archive.read(name) for name in archive.namelist())
        self.assertIn(alice.encode(), payload)
        self.assertNotIn(bob.encode(), payload)
        self.assertNotIn(b"BOB_PRIVATE_MARKER", payload)
        self.analysis.assert_not_called()

    def test_unreferenced_history_can_be_deleted_without_affecting_other_account(self):
        own, _ = self.save_batch()
        other, _ = self.save_batch(self.bob, "other account")
        self.assertEqual(self.request("GET", f"/api/v1/batches/{own}/deletion").json(), {"allowed": True})
        self.assertEqual(self.request("DELETE", f"/api/v1/batches/{own}").status_code, 204)
        self.error(self.request("GET", f"/api/v1/batches/{own}/view"), 404)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{other}/view", token=self.bob_token).status_code, 200)

    def test_durable_job_original_reference_blocks_history_deletion(self):
        from tests.test_backend_api import CSV_BYTES
        from tests.test_history_store import sample_report
        response = self.job()
        self.assertEqual(response.status_code, 202, response.text)
        service, _ = self.app.state.tenants.for_workspace(self.alice["workspace_id"])
        batch_id = service.history.save_batch(sample_report(), filename="job-original.csv",
            file_bytes=CSV_BYTES, mapping={"text_column": "评价"})
        check = self.request("GET", f"/api/v1/batches/{batch_id}/deletion")
        self.assertEqual(check.json(), {"allowed": False})
        self.error(self.request("DELETE", f"/api/v1/batches/{batch_id}"), 409)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{batch_id}/view").status_code, 200)

    def test_new_read_and_delete_entries_require_session(self):
        batch_id, _ = self.save_batch()
        for method, path in (("GET", "/status"), ("GET", "/backup"),
                             ("GET", f"/batches/{batch_id}/view"), ("DELETE", f"/batches/{batch_id}"),
                             ("DELETE", f"/batches/{batch_id}/chat")):
            self.error(self.client.request(method, "/api/v1" + path), 401)

    def test_clear_chat_removes_only_the_explicit_comparison_scope(self):
        own, _ = self.save_batch()
        previous, _ = self.save_batch(label="previous fixture")
        for comparison in (None, previous):
            response = self.request("POST", f"/api/v1/batches/{own}/chat", json=self.chat_payload(comparison_id=comparison))
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.request("DELETE", f"/api/v1/batches/{own}/chat").status_code, 204)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{own}/chat").json()["total"], 0)
        preserved = self.request("GET", f"/api/v1/batches/{own}/chat", params={"comparison_id": previous})
        self.assertEqual(preserved.json()["total"], 1)


class RemoteClientTests(unittest.TestCase):
    def test_ui_mode_has_no_database_dependency_and_implies_queued(self):
        with patch.dict(os.environ, {"APP_UI_MODE": "api", "APP_DATABASE_URL": "", "APP_ANALYSIS_MODE": "direct"}):
            self.assertTrue(api_ui_enabled())
            self.assertTrue(queued_analysis_enabled())

    def test_legacy_account_database_marker_selects_api_without_connecting_database(self):
        with patch.dict(os.environ, {"APP_UI_MODE": "", "APP_DATABASE_URL": "postgresql://unused-marker",
                                     "APP_ANALYSIS_MODE": "direct"}):
            with patch("backend.database.get_engine", side_effect=AssertionError("UI must not connect")):
                self.assertTrue(api_ui_enabled())
                self.assertTrue(queued_analysis_enabled())

    def test_download_is_authenticated_and_transport_errors_are_redacted(self):
        seen = []
        def response(request):
            seen.append(request)
            return httpx.Response(200, content=b"PK_fixture")
        client = JobApiClient("http://api:8000", "PRIVATE_SESSION", transport=httpx.MockTransport(response))
        self.assertEqual(client.backup(), b"PK_fixture")
        self.assertEqual(seen[0].headers["authorization"], "Bearer PRIVATE_SESSION")
        self.assertEqual(str(seen[0].url), "http://api:8000/api/v1/backup")
        def failed(request):
            raise httpx.ConnectError("PRIVATE_PROVIDER_OR_TOKEN", request=request)
        client._transport = httpx.MockTransport(failed)
        with self.assertRaises(JobClientError) as raised:
            client.me()
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_mutations_are_not_retried_and_server_error_body_is_never_exposed(self):
        attempts = []
        def response(request):
            attempts.append(request)
            return httpx.Response(502, text="PRIVATE_UPSTREAM_EXCEPTION")
        client = JobApiClient("http://api:8000", "session", transport=httpx.MockTransport(response))
        with self.assertRaises(JobClientError) as raised:
            client.chat("a" * 32, "question", max_requests=6)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_typed_report_rejects_unrecognized_envelope(self):
        with self.assertRaises(JobClientError):
            decode_view({"metadata": {}, "packed_report": '{"pickle":"not permitted"}'})


class RemoteUiPresentationTests(unittest.TestCase):
    setUp = ui_fixtures.AccountUiTests.setUp
    app = ui_fixtures.AccountUiTests.app

    def test_old_account_deployment_requires_login_and_does_not_open_legacy_history(self):
        with patch.dict(os.environ, {"APP_UI_MODE": "", "APP_DATABASE_URL": "postgresql://unused-account-marker"}):
            app = self.app(stale=True)
        self.assertTrue(any(button.label == "登录" for button in app.button))
        self.accounts.authenticate.assert_not_called()
        self.history_factory.assert_not_called()
        self.client.list_jobs.assert_not_called()
        self.client.list_batches.assert_not_called()
        self.upload.assert_not_called()
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_api_unavailable_does_not_fallback_to_local_accounts_or_storage(self):
        self.client.me.side_effect = JobClientError("服务暂不可用。", status_code=503)
        app = self.app(authenticated=True)
        self.accounts.authenticate.assert_not_called()
        self.history_factory.assert_not_called()
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()
        self.upload.assert_not_called()
        self.assertIn("ci_account_token", app.session_state)
        self.assertTrue(any("服务暂不可用" in message.value for message in app.error))

    def test_logged_in_ui_fetches_status_and_history_without_local_business_services(self):
        self.app(authenticated=True)
        self.client.status.assert_called()
        self.client.list_batches.assert_called()
        self.accounts.authenticate.assert_not_called()
        self.history_factory.assert_not_called()
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()

    def test_upload_and_submission_are_planned_by_api_and_submit_only_once(self):
        from tests.test_customer_app import uploaded_csv
        self.upload.return_value = uploaded_csv()
        self.client.upload.return_value = {"upload_id": "c" * 32, "sheet_names": []}
        self.client.preview.return_value = {"columns": ["产品", "评价"], "total_rows": 2,
            "rows": [{"产品": "杯子", "评价": "杯口有裂纹"}],
            "suggested_mapping": {"text_column": "评价", "group_column": "产品", "date_column": None}}
        self.client.inspect_upload.return_value = {"counts": {"pending": 2}, "total_rows": 2,
            "expected_requests": 1, "cached_batch_id": None}
        job = {"id": "d" * 32, "status": "queued", "filename": "评价.csv", "total_rows": 2,
               "completed_rows": 0, "request_count": 0, "max_requests": 1, "batch_id": None}
        self.client.create_job.return_value = job
        self.client.get_job.return_value = job
        self.client.list_jobs.return_value = {"items": [job]}
        app = self.app(authenticated=True)
        app.checkbox(key="customer_consent").check().run()
        app.button(key="customer_analyze").click().run()
        self.assertFalse(app.exception)
        self.client.upload.assert_called_once()
        self.client.inspect_upload.assert_called()
        self.client.create_job.assert_called_once()
        self.assertEqual(self.client.create_job.call_args.args[0]["max_requests"], 1)
        self.analyzer.assert_not_called()
        self.agent.assert_not_called()
        self.history_factory.assert_not_called()

    def test_agent_followup_uses_saved_report_id_and_server_history(self):
        from tests.test_history_store import sample_report
        from tests.test_backend_api import offline_agent
        report = sample_report()
        self.client.chat_history.return_value = {"items": [], "total": 0}
        self.client.export.return_value = b"offline export"
        self.client.status.return_value["agent_requests_per_run"] = 6
        question = "请展开说明刚才的质量问题。"
        self.client.chat.return_value = {"result": offline_agent(report, question).model_dump(mode="json")}
        app = AppTest.from_file(str(APP_PATH), default_timeout=25)
        app.session_state["ci_account_token"] = ui_fixtures.SESSION_TOKEN
        app.session_state["ci_account_user"] = ui_fixtures.PRINCIPAL.copy()
        app.session_state["customer_report"] = report
        app.session_state["customer_active_batch_id"] = "e" * 32
        app.session_state["customer_history_view"] = True
        app.run()
        self.assertFalse(app.exception)
        app.text_area(key="customer_agent_question").set_value(question)
        app.checkbox(key="customer_agent_consent").check().run()
        app.button(key="customer_agent_generate").click().run()
        self.assertFalse(app.exception)
        self.client.chat.assert_called_once_with("e" * 32, question, comparison_id=None, max_requests=6)
        self.agent.assert_not_called()
        self.analyzer.assert_not_called()
        self.history_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
