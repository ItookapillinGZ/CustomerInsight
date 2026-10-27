"""Authenticated HTTP isolation checks; temporary workspaces, no model traffic.

TEST_PG_URL optionally selects an isolated PostgreSQL test server. Each test
creates a private UUID schema and never drops or modifies the public schema.
"""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, text

from backend.accounts import AccountService
from backend.app import create_app
from backend.database import Base, get_engine
import backend.pg_history  # register all tenant relations before test DDL
import backend.pg_jobs
from core.product_service import ProductRequestBudget
from tests.helpers import offline_temp_root
from tests.test_backend_api import CSV_BYTES, offline_analysis, offline_agent
from tests.test_history_store import sample_report


PASSWORD = "IsolatedFixturePassword_2026"
LEGACY_TOKEN = "legacy-deployment-token-must-not-authenticate"


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environ = patch.dict(os.environ, {"APP_ANALYSIS_MODE": "direct"})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        test_url = os.getenv("TEST_PG_URL")
        self.engine = get_engine(test_url or f"sqlite:///{self.root / 'account-api.sqlite3'}")
        self.addCleanup(self.engine.dispose)
        if test_url:
            if self.engine.dialect.name != "postgresql":
                raise RuntimeError("TEST_PG_URL must target PostgreSQL")
            self.schema = "account_api_test_" + uuid4().hex
            with self.engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            self.engine = self.engine.execution_options(schema_translate_map={None: self.schema})
            @event.listens_for(self.engine, "begin")
            def select_test_schema(connection):
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{self.schema}"')
            self.addCleanup(self._drop_schema)
        Base.metadata.create_all(self.engine)
        self.now = 1000.0
        self.accounts = AccountService(self.engine, clock=lambda: self.now)
        self.alice = self.accounts.create_user("alice.fixture", PASSWORD)
        self.bob = self.accounts.create_user("bob.fixture", PASSWORD)
        self.analysis = Mock(side_effect=offline_analysis)
        self.agent = Mock(side_effect=offline_agent)
        self.budget = Mock(spec=ProductRequestBudget)
        self.budget.remaining.return_value = 100
        self.app = create_app(data_dir=self.root / "workspace", api_token=LEGACY_TOKEN,
            account_service=self.accounts, budget=self.budget, analysis_runner=self.analysis,
            agent_runner=self.agent, job_dispatcher=lambda _store: None)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.alice_token = self.login(self.alice["username"])
        self.bob_token = self.login(self.bob["username"])

    def _drop_schema(self):
        with self.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))

    def login(self, username, password=PASSWORD):
        response = self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (self.alice_token if token is None else token)}

    def request(self, method, path, *, token=None, **kwargs):
        return self.client.request(method, path, headers=self.headers(token), **kwargs)

    def error(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        document = response.json()
        self.assertTrue(document["request_id"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return document["error"]

    def upload(self, *, token=None, data=CSV_BYTES, form=None):
        response = self.request("POST", "/api/v1/uploads", token=token,
            files={"file": ("offline.csv", io.BytesIO(data))}, data=form or {"purpose": "job"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["upload_id"]

    def job(self, *, token=None, upload_id=None, **extra):
        upload_id = upload_id or self.upload(token=token)
        payload = {"upload_id": upload_id, "text_column": "review", "group_column": "store",
            "date_column": "date", "idempotency_key": uuid4().hex,
            "allow_model_call": True, "max_requests": 5}
        payload.update(extra)
        return self.request("POST", "/api/v1/jobs", token=token, json=payload)

    def save_batch(self, user=None, label="private fixture"):
        user = self.alice if user is None else user
        service, _jobs = self.app.state.tenants.for_workspace(user["workspace_id"])
        original = f"评价,门店\n杯子保温很好 {label},甲店\n".encode("utf-8-sig")
        batch_id = service.history.save_batch(sample_report(f"杯子保温很好 {label}"),
            filename=label + ".csv", file_bytes=original,
            mapping={"text_column": "评价", "group_column": "门店", "sheet_name": None})
        return batch_id, original

    def chat_payload(self, **extra):
        return {"question": "哪些评价需要继续跟进？", "allow_model_call": True,
                "max_requests": 10, **extra}

    def test_login_selects_server_workspace_and_never_returns_password_hash(self):
        first = self.request("GET", "/api/v1/auth/me")
        second = self.request("GET", "/api/v1/auth/me", token=self.bob_token)
        self.assertEqual(first.json(), self.alice)
        self.assertEqual(second.json(), self.bob)
        self.assertNotEqual(self.alice["workspace_id"], self.bob["workspace_id"])
        self.assertNotEqual(self.alice_token, self.bob_token)
        self.assertNotIn("password", first.text)
        self.assertNotIn(LEGACY_TOKEN, first.text)

    def test_legacy_deployment_token_is_rejected_in_account_mode(self):
        for method, route in (("GET", "/api/v1/auth/me"), ("GET", "/api/v1/batches"),
                              ("GET", "/api/v1/jobs"), ("POST", "/api/v1/uploads")):
            with self.subTest(route=route):
                self.error(self.request(method, route, token=LEGACY_TOKEN), 401)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_missing_session_is_rejected_before_upload_or_job_processing(self):
        for method, route in (("GET", "/api/v1/batches"), ("POST", "/api/v1/uploads"),
                              ("POST", "/api/v1/jobs")):
            self.error(self.client.request(method, route), 401)
        self.assertFalse((self.root / "workspace" / "workspaces").exists())

    def test_foreign_upload_id_is_not_found_for_preview_analysis_and_jobs(self):
        upload_id = self.upload()
        self.error(self.request("GET", f"/api/v1/uploads/{upload_id}/preview", token=self.bob_token), 404)
        self.error(self.request("POST", "/api/v1/analyses", token=self.bob_token, json={
            "upload_id": upload_id, "text_column": "review", "allow_model_call": True, "max_requests": 5}), 404)
        self.error(self.job(token=self.bob_token, upload_id=upload_id), 404)
        self.assertEqual(self.request("GET", f"/api/v1/uploads/{upload_id}/preview").status_code, 200)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_foreign_jobs_cannot_be_read_cancelled_or_resumed(self):
        response = self.job()
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        for method, suffix, payload in (("GET", "", None), ("POST", "/cancel", None),
                                         ("POST", "/resume", {"allow_model_call": True, "max_requests": 5})):
            route = f"/api/v1/jobs/{job_id}{suffix}"
            with self.subTest(route=route):
                error = self.error(self.request(method, route, token=self.bob_token, json=payload), 404)
                missing = self.error(self.request(method, f"/api/v1/jobs/{'0' * 32}{suffix}", token=self.bob_token, json=payload), 404)
                self.assertEqual(error, missing)
        own = self.request("GET", f"/api/v1/jobs/{job_id}").json()
        self.assertFalse(own["cancel_requested"])
        self.assertEqual(own["request_count"], 0)
        self.analysis.assert_not_called()

    def test_job_list_and_pagination_include_only_authenticated_workspace(self):
        first = self.job().json()["id"]
        second = self.job(force=True).json()["id"]
        foreign = self.job(token=self.bob_token).json()["id"]
        own = self.request("GET", "/api/v1/jobs", params={"limit": 1, "offset": 0}).json()
        next_page = self.request("GET", "/api/v1/jobs", params={"limit": 1, "offset": 1}).json()
        self.assertEqual((own["total"], next_page["total"]), (2, 2))
        self.assertEqual({own["items"][0]["id"], next_page["items"][0]["id"]}, {first, second})
        self.assertNotIn(foreign, str(own) + str(next_page))
        other = self.request("GET", "/api/v1/jobs", token=self.bob_token).json()
        self.assertEqual(other["total"], 1)
        self.assertEqual(other["items"][0]["id"], foreign)

    def test_foreign_batch_rows_and_all_downloads_are_not_found(self):
        batch_id, original = self.save_batch()
        for suffix in ("", "/rows", "/exports/original", "/exports/csv", "/exports/html"):
            with self.subTest(suffix=suffix):
                error = self.error(self.request("GET", f"/api/v1/batches/{batch_id}{suffix}", token=self.bob_token), 404)
                missing = self.error(self.request("GET", f"/api/v1/batches/{'0' * 32}{suffix}", token=self.bob_token), 404)
                self.assertEqual(error, missing)
                self.assertEqual(self.request("GET", f"/api/v1/batches/{batch_id}{suffix}").status_code, 200)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{batch_id}/exports/original").content, original)
        self.agent.assert_not_called()

    def test_batch_list_and_pagination_never_expose_other_account_metadata(self):
        first, _ = self.save_batch(label="alice one")
        second, _ = self.save_batch(label="alice two")
        foreign, _ = self.save_batch(self.bob, "bob secret")
        pages = [self.request("GET", "/api/v1/batches", params={"limit": 1, "offset": offset}).json() for offset in (0, 1)]
        self.assertEqual([page["total"] for page in pages], [2, 2])
        self.assertEqual({page["items"][0]["id"] for page in pages}, {first, second})
        self.assertNotIn(foreign, str(pages))
        self.assertNotIn("bob secret", str(pages))
        other = self.request("GET", "/api/v1/batches", token=self.bob_token).json()
        self.assertEqual(other["total"], 1)
        self.assertEqual(other["items"][0]["id"], foreign)

    def test_foreign_chat_report_and_comparison_ids_are_not_found_before_agent(self):
        first, _ = self.save_batch()
        second, _ = self.save_batch(self.bob, "other account")
        self.error(self.request("GET", f"/api/v1/batches/{first}/chat", token=self.bob_token), 404)
        self.error(self.request("POST", f"/api/v1/batches/{first}/chat", token=self.bob_token,
            json=self.chat_payload()), 404)
        self.error(self.request("GET", f"/api/v1/batches/{first}/chat", params={"comparison_id": second}), 404)
        self.error(self.request("POST", f"/api/v1/batches/{first}/chat",
            json=self.chat_payload(comparison_id=second)), 404)
        self.error(self.request("POST", f"/api/v1/batches/{second}/chat",
            json=self.chat_payload(comparison_id=first)), 404)
        self.agent.assert_not_called()
        self.budget.reserve.assert_not_called()

    def test_valid_chat_is_saved_only_in_its_owner_workspace(self):
        own, _ = self.save_batch()
        other, _ = self.save_batch(self.bob, "other account")
        response = self.request("POST", f"/api/v1/batches/{own}/chat", json=self.chat_payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.agent.assert_called_once()
        self.assertEqual(self.request("GET", f"/api/v1/batches/{own}/chat").json()["total"], 1)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{other}/chat", token=self.bob_token).json()["total"], 0)

    def test_request_body_cannot_select_a_workspace_or_user(self):
        upload_id = self.upload()
        batch_id, _ = self.save_batch()
        for field in ("workspace_id", "user_id", "scope"):
            with self.subTest(field=field):
                self.error(self.job(upload_id=upload_id, **{field: self.bob["workspace_id"]}), 422)
                self.error(self.request("POST", "/api/v1/analyses", json={"upload_id": upload_id,
                    "text_column": "review", "allow_model_call": True, "max_requests": 5,
                    field: self.bob["workspace_id"]}), 422)
                self.error(self.request("POST", f"/api/v1/batches/{batch_id}/chat",
                    json=self.chat_payload(**{field: self.bob["workspace_id"]})), 422)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_query_and_multipart_workspace_fields_cannot_redirect_storage(self):
        upload_id = self.upload(form={"purpose": "job", "workspace_id": self.bob["workspace_id"]})
        self.assertEqual(self.request("GET", f"/api/v1/uploads/{upload_id}/preview",
            params={"workspace_id": self.bob["workspace_id"]}).status_code, 200)
        self.error(self.request("GET", f"/api/v1/uploads/{upload_id}/preview", token=self.bob_token), 404)
        foreign, _ = self.save_batch(self.bob)
        listing = self.request("GET", "/api/v1/batches", params={"workspace_id": self.bob["workspace_id"]})
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 0)
        self.assertNotIn(foreign, listing.text)

    def test_logout_revokes_only_the_submitted_session(self):
        second_session = self.login(self.alice["username"])
        self.assertEqual(self.request("POST", "/api/v1/auth/logout").status_code, 204)
        self.error(self.request("GET", "/api/v1/batches"), 401)
        self.assertEqual(self.request("GET", "/api/v1/auth/me", token=second_session).status_code, 200)
        self.assertEqual(self.request("GET", "/api/v1/auth/me", token=self.bob_token).status_code, 200)

    def test_password_change_revokes_all_own_sessions_and_old_password(self):
        second_session = self.login(self.alice["username"])
        replacement = "ReplacementOfflinePassword_2026"
        response = self.request("POST", "/api/v1/auth/password", json={
            "current_password": PASSWORD, "new_password": replacement})
        self.assertEqual(response.status_code, 204, response.text)
        for token in (self.alice_token, second_session):
            self.error(self.request("GET", "/api/v1/auth/me", token=token), 401)
        old_login = self.client.post("/api/v1/auth/login", json={"username": self.alice["username"], "password": PASSWORD})
        self.error(old_login, 401)
        self.assertTrue(self.login(self.alice["username"], replacement))
        self.assertEqual(self.request("GET", "/api/v1/auth/me", token=self.bob_token).status_code, 200)

    def test_incorrect_current_password_does_not_change_password_or_revoke_session(self):
        response = self.request("POST", "/api/v1/auth/password", json={
            "current_password": "wrong-fixture-password", "new_password": "ReplacementOfflinePassword_2026"})
        self.error(response, 401)
        self.assertEqual(self.request("GET", "/api/v1/auth/me").status_code, 200)
        self.assertTrue(self.login(self.alice["username"]))

    def test_expired_session_cannot_access_saved_reports(self):
        batch_id, _ = self.save_batch()
        self.now += 8 * 60 * 60 + 1
        self.error(self.request("GET", f"/api/v1/batches/{batch_id}"), 401)
        self.error(self.request("GET", "/api/v1/auth/me", token=self.bob_token), 401)
        new_token = self.login(self.alice["username"])
        self.assertEqual(self.request("GET", f"/api/v1/batches/{batch_id}", token=new_token).status_code, 200)


if __name__ == "__main__":
    unittest.main()
