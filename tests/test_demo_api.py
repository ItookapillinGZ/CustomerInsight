"""Fixed demo imports use normal authenticated history and chat boundaries."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from backend.accounts import AccountService
from backend.app import create_app
import backend.pg_history  # Register tenant tables before the isolated test DDL.
import backend.pg_jobs
from backend.client import JobApiClient, JobClientError
from backend.database import Base, get_engine
from backend.schemas import DemoWorkspaceResponse
from core.history_store import _unpack
from core.product_service import ProductRequestBudget
from tests.helpers import offline_temp_root
from tests.test_backend_api import offline_agent


PASSWORD = "IsolatedDemoPassword_2026"


class DemoWorkspaceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = get_engine("sqlite:///" + (self.root / "accounts.sqlite3").as_posix())
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.accounts = AccountService(self.engine, clock=lambda: 1000.0)
        self.alice = self.accounts.create_user("demo.alice", PASSWORD)
        self.bob = self.accounts.create_user("demo.bob", PASSWORD)
        self.analysis = Mock(side_effect=AssertionError("Demo import must not analyze reviews"))
        self.agent = Mock(side_effect=offline_agent)
        self.budget = Mock(spec=ProductRequestBudget)
        self.budget.remaining.return_value = 100
        self.app = create_app(data_dir=self.root / "workspace", api_token="legacy-token",
                              account_service=self.accounts, budget=self.budget,
                              analysis_runner=self.analysis, agent_runner=self.agent,
                              job_dispatcher=lambda _store: None)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.tokens = {}
        for user in (self.alice, self.bob):
            result = self.client.post("/api/v1/auth/login", json={
                "username": user["username"], "password": PASSWORD})
            self.assertEqual(result.status_code, 200, result.text)
            self.tokens[user["id"]] = result.json()["access_token"]

    def request(self, method, path, *, user=None, **options):
        token = self.tokens[(user or self.alice)["id"]]
        return self.client.request(method, path, headers={"Authorization": "Bearer " + token}, **options)

    def import_demo(self, user=None):
        response = self.request("POST", "/api/v1/demo-workspace", user=user)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_import_requires_a_session_and_rejects_legacy_token_before_store_access(self):
        for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": "Bearer legacy-token"}):
            with self.subTest(headers=headers):
                response = self.client.post("/api/v1/demo-workspace", headers=headers)
                self.assertEqual(response.status_code, 401, response.text)
        self.assertFalse((self.root / "workspace" / "workspaces").exists())
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_no_client_fixture_or_target_workspace_can_be_supplied(self):
        for data in ({"report": {"metrics": {"success_rows": 999}}},
                     {"workspace_id": self.bob["workspace_id"]},
                     {"fixture_path": "private.xlsx"}, {"model_calls": 2}, ["fixture"]):
            with self.subTest(data=data):
                response = self.request("POST", "/api/v1/demo-workspace", json=data)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.request("GET", "/api/v1/batches").json()["total"], 0)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_fixed_import_is_model_free_and_only_uses_the_authenticated_store(self):
        with patch("ai.client.get_openai_client", side_effect=AssertionError("No LLM client may be created")):
            result = self.import_demo()
        self.assertEqual(set(result), {"current_batch_id", "previous_batch_id", "model_calls", "data_origin"})
        self.assertEqual((result["model_calls"], result["data_origin"]), (0, "synthetic"))
        self.assertNotEqual(result["current_batch_id"], result["previous_batch_id"])
        for identifier in (result["current_batch_id"], result["previous_batch_id"]):
            self.assertRegex(identifier, r"^[0-9a-f]{32}$")
            own = self.request("GET", f"/api/v1/batches/{identifier}")
            self.assertEqual(own.status_code, 200, own.text)
            self.assertEqual(own.json()["metadata"]["success_rows"], 30)
        self.assertEqual(self.request("GET", "/api/v1/batches").json()["total"], 2)
        self.assertEqual(self.request("GET", "/api/v1/batches", user=self.bob).json()["total"], 0)
        self.assertIsNone(self.app.state.service._history)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()
        self.budget.reserve.assert_not_called()
        self.budget.remaining.assert_not_called()

    def test_accounts_import_independently_and_cannot_view_or_chat_with_foreign_ids(self):
        first, second = self.import_demo(), self.import_demo(self.bob)
        self.assertTrue(set((first["current_batch_id"], first["previous_batch_id"])).isdisjoint(
            (second["current_batch_id"], second["previous_batch_id"])))
        for identifier in (first["current_batch_id"], first["previous_batch_id"]):
            for suffix in ("", "/rows", "/view", "/chat", "/exports/original"):
                response = self.request("GET", f"/api/v1/batches/{identifier}{suffix}", user=self.bob)
                self.assertEqual(response.status_code, 404, response.text)
        current = first["current_batch_id"]
        response = self.request("GET", f"/api/v1/batches/{current}/view",
                                params={"comparison_id": second["previous_batch_id"]})
        self.assertEqual(response.status_code, 404, response.text)
        response = self.request("POST", f"/api/v1/batches/{current}/chat", user=self.bob, json={
            "question": "查看别人的表格", "allow_model_call": True, "max_requests": 6})
        self.assertEqual(response.status_code, 404, response.text)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_imported_batches_use_normal_comparison_view_and_multiturn_chat_paths(self):
        result = self.import_demo()
        current, previous = result["current_batch_id"], result["previous_batch_id"]
        view = self.request("GET", f"/api/v1/batches/{current}/view", params={"comparison_id": previous})
        self.assertEqual(view.status_code, 200, view.text)
        report = _unpack(view.json()["packed_report"])
        self.assertEqual(report["data_origin"], "synthetic")
        self.assertEqual(report["historical_context"]["batch"]["id"], previous)
        self.assertTrue(report["historical_context"]["comparison"]["topics_comparable"])
        no_consent = self.request("POST", f"/api/v1/batches/{current}/chat", json={"question": "哪个商品需要跟进？"})
        self.assertEqual(no_consent.status_code, 428, no_consent.text)
        self.agent.assert_not_called()
        for question in ("哪个商品需要跟进？", "和上批相比呢？"):
            response = self.request("POST", f"/api/v1/batches/{current}/chat", json={
                "question": question, "comparison_id": previous, "allow_model_call": True, "max_requests": 6})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.agent.call_count, 2)
        self.assertEqual(len(self.agent.call_args.kwargs["history"]), 1)
        self.assertEqual(self.agent.call_args.args[0]["historical_context"]["batch"]["id"], previous)
        history = self.request("GET", f"/api/v1/batches/{current}/chat", params={"comparison_id": previous})
        self.assertEqual(history.json()["total"], 2)
        self.analysis.assert_not_called()

    def test_repeated_import_preserves_ids_reports_originals_and_existing_chat(self):
        first = self.import_demo()
        current, previous = first["current_batch_id"], first["previous_batch_id"]
        response = self.request("POST", f"/api/v1/batches/{current}/chat", json={
            "question": "保留这个问题", "comparison_id": previous, "allow_model_call": True, "max_requests": 6})
        self.assertEqual(response.status_code, 200, response.text)
        before = {identifier: self.request("GET", f"/api/v1/batches/{identifier}").json()
                  for identifier in (current, previous)}
        original = self.request("GET", f"/api/v1/batches/{current}/exports/original").content
        history_before = self.request("GET", f"/api/v1/batches/{current}/chat", params={"comparison_id": previous}).json()
        self.agent.reset_mock()
        self.assertEqual(self.import_demo(), first)
        empty_object = self.request("POST", "/api/v1/demo-workspace", json={})
        self.assertEqual(empty_object.json(), first)
        for identifier in (current, previous):
            self.assertEqual(self.request("GET", f"/api/v1/batches/{identifier}").json(), before[identifier])
        self.assertEqual(self.request("GET", f"/api/v1/batches/{current}/exports/original").content, original)
        self.assertEqual(self.request("GET", f"/api/v1/batches/{current}/chat",
                                     params={"comparison_id": previous}).json(), history_before)
        self.assertEqual(self.request("GET", "/api/v1/batches").json()["total"], 2)
        self.agent.assert_not_called()
        self.analysis.assert_not_called()

    def test_workspace_mutation_lock_blocks_import_and_is_released_after_error(self):
        service, _jobs = self.app.state.tenants.for_workspace(self.alice["workspace_id"])
        service._mutation_lock.acquire()
        try:
            response = self.request("POST", "/api/v1/demo-workspace")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "workspace_busy")
        finally:
            service._mutation_lock.release()
        with patch("core.demo_workspace.save_demo_workspace", side_effect=ValueError("PRIVATE_FIXTURE")):
            response = self.request("POST", "/api/v1/demo-workspace")
            self.assertEqual(response.status_code, 500, response.text)
            self.assertNotIn("PRIVATE_FIXTURE", response.text)
        self.assertFalse(service._mutation_lock.locked())
        self.import_demo()

    def test_openapi_exposes_authenticated_fixed_response_contract(self):
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/api/v1/demo-workspace"]["post"]
        self.assertTrue(operation["security"])
        self.assertEqual(operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                         "#/components/schemas/DemoWorkspaceResponse")
        for value in ({"current_batch_id": "private-path", "previous_batch_id": "0" * 32},
                      {"current_batch_id": "0" * 32, "previous_batch_id": "1" * 32, "model_calls": 1},
                      {"current_batch_id": "0" * 32, "previous_batch_id": "1" * 32, "data_origin": "real"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                DemoWorkspaceResponse.model_validate(value)


class DemoWorkspaceClientTests(unittest.TestCase):
    def test_client_sends_one_authenticated_post_without_fixture_data(self):
        requests = []
        expected = {"current_batch_id": "a" * 32, "previous_batch_id": "b" * 32,
                    "model_calls": 0, "data_origin": "synthetic"}
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=expected)
        client = JobApiClient("http://api:8000", "fixture-token", transport=httpx.MockTransport(respond))
        self.assertEqual(client.import_demo_workspace(), expected)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].url.path, "/api/v1/demo-workspace")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer fixture-token")
        self.assertEqual(requests[0].content, b"")

    def test_client_never_retries_a_lost_import_response(self):
        requests = []
        def respond(request):
            requests.append(request)
            raise httpx.ReadTimeout("PRIVATE_TOKEN", request=request)
        client = JobApiClient("http://api:8000", "fixture-token", transport=httpx.MockTransport(respond))
        with self.assertRaises(JobClientError) as raised:
            client.import_demo_workspace()
        self.assertEqual(len(requests), 1)
        self.assertNotIn("PRIVATE_TOKEN", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
