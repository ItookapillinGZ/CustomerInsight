"""Durable HTTP job boundaries with disposable files and no model transport."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.job_store import JobStore
from core.config import Config
from core.csv_io import results_to_dataframe
from core.customer_report import build_customer_report
from core.excel_io import prepare_excel_feedback_rows, read_feedback_xlsx
from core.product_service import ProductLimitError, ProductRequestBudget, ProductRunControl
from tests.helpers import offline_temp_root
from tests.test_backend_api import offline_analysis
from tests.test_excel_io import make_xlsx


TOKEN = "isolated-job-api-token"


def csv_reviews(count, prefix="Synthetic review"):
    return ("review,store\n" + "".join(f"{prefix} {index},North\n" for index in range(count))).encode("utf-8-sig")


class JobApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.budget = ProductRequestBudget(self.root / "usage.sqlite3", daily_limit=100)
        self.dispatcher = Mock()
        self.analysis = Mock(side_effect=AssertionError("HTTP jobs must not execute model analysis"))
        self.agent = Mock(side_effect=AssertionError("Job requests must not execute the agent"))
        self.environment = patch.dict("os.environ", {
            "APP_ANALYSIS_MODE": "direct", "APP_LLM_BATCH_SIZE": "10",
            "APP_LLM_BATCH_MAX_CHARACTERS": "12000",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(
            data_dir=self.root / "workspace", api_token=TOKEN, budget=self.budget,
            analysis_runner=self.analysis, agent_runner=self.agent,
            job_dispatcher=self.dispatcher,
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = f"Bearer {TOKEN}"

    @property
    def store(self):
        return self.app.state.jobs.store

    def upload(self, data=None, filename="reviews.csv", purpose="job"):
        response = self.client.post("/api/v1/uploads", files={
            "file": (filename, io.BytesIO(csv_reviews(20) if data is None else data)),
        }, data={"purpose": purpose})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create(self, upload=None, **changes):
        upload = self.upload() if upload is None else upload
        payload = {
            "upload_id": upload["upload_id"], "text_column": "review",
            "allow_model_call": True, "max_requests": 50,
            "idempotency_key": "isolated-create-key-0001",
        }
        payload.update(changes)
        return self.client.post("/api/v1/jobs", json=payload)

    def assert_error(self, response, status, code=None):
        self.assertEqual(response.status_code, status, response.text)
        result = response.json()
        self.assertTrue(result["request_id"])
        if code is not None:
            self.assertEqual(result["error"]["code"], code, response.text)
        return result

    def assert_no_transport(self):
        self.analysis.assert_not_called()
        self.agent.assert_not_called()
        self.assertEqual(self.budget.remaining(), 100)

    def test_500_rows_create_50_groups_with_durable_snapshot_after_upload_expiry(self):
        data = csv_reviews(500)
        upload = self.upload(data)
        response = self.create(upload)
        self.assertEqual(response.status_code, 202, response.text)
        public = response.json()
        self.assertEqual((public["total_rows"], public["total_groups"]), (500, 50))
        self.assertEqual(public["status"], "queued")
        self.assertEqual(public["request_count"], 0)
        self.assertEqual(response.headers["location"], "/api/v1/jobs/" + public["id"])
        reopened = JobStore(self.root / "workspace")
        self.assertEqual(reopened.original(public["id"]), data)
        snapshot = reopened.get_job(public["id"])
        self.assertEqual(len(snapshot["groups"]), 50)
        self.assertEqual(len(reopened.get_results(public["id"])), 500)
        self.assertIn("frame_snapshot", snapshot["config"])
        for path in (self.root / "workspace" / "api_uploads").glob("*"):
            path.unlink()
        self.assertEqual(self.client.get("/api/v1/jobs/" + public["id"]).status_code, 200)
        self.assertEqual(reopened.original(public["id"]), data)
        self.assert_no_transport()

    def test_default_100_row_limit_unchanged_and_job_upload_bounded_at_1000(self):
        for count, purpose, expected in ((101, "analysis", 422), (500, "job", 201),
                                         (1000, "job", 201), (1001, "job", 422)):
            with self.subTest(count=count, purpose=purpose):
                response = self.client.post("/api/v1/uploads", files={
                    "file": ("reviews.csv", csv_reviews(count)),
                }, data={"purpose": purpose})
                self.assertEqual(response.status_code, expected, response.text)
        self.dispatcher.assert_not_called()
        self.assert_no_transport()

    def test_500_row_excel_preview_and_job_preserve_selected_sheet(self):
        data = make_xlsx([("说明", [["说明"], ["Select Reviews"]]),
                          ("Reviews", [["review", "store"]] + [[f"Item {i}", "North"] for i in range(500)])])
        upload = self.upload(data, "reviews.xlsx")
        preview = self.client.get(f"/api/v1/uploads/{upload['upload_id']}/preview",
                                  params={"sheet_name": "Reviews"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["total_rows"], 500)
        response = self.create(upload, sheet_name="Reviews")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["total_groups"], 50)
        job = self.store.get_job(response.json()["id"])
        self.assertEqual(job["request"]["sheet_name"], "Reviews")
        self.assertIsNone(job["request"]["encoding"])
        self.assert_no_transport()

    def test_unauthenticated_job_routes_cannot_enqueue_or_charge(self):
        upload = self.upload()
        for method, path, payload in (
            ("POST", "/api/v1/jobs", {"upload_id": upload["upload_id"]}),
            ("GET", "/api/v1/jobs", None),
            ("GET", "/api/v1/jobs/" + "0" * 32, None),
            ("POST", "/api/v1/jobs/" + "0" * 32 + "/cancel", None),
            ("POST", "/api/v1/jobs/" + "0" * 32 + "/resume", {"max_requests": 10}),
        ):
            with self.subTest(path=path, method=method):
                self.assert_error(self.client.request(method, path, json=payload,
                                  headers={"Authorization": "Bearer invalid"}), 401)
        self.dispatcher.assert_not_called()
        self.assertEqual(self.store.list_jobs()["total"], 0)
        self.assert_no_transport()

    def test_consent_and_explicit_sufficient_request_budget_precede_enqueue(self):
        upload = self.upload(csv_reviews(500))
        for changes, status, code in (
            ({"allow_model_call": False}, 428, "model_consent_required"),
            ({"max_requests": None}, 428, "request_budget_required"),
            ({"max_requests": 49}, 422, "request_budget_too_small"),
            ({"max_requests": 0}, 422, "invalid_request"),
        ):
            with self.subTest(changes=changes):
                self.assert_error(self.create(upload, **changes), status, code)
        self.dispatcher.assert_not_called()
        self.assertEqual(self.store.list_jobs()["total"], 0)
        self.assert_no_transport()

    def test_strict_inputs_reject_coercion_extra_credentials_and_invalid_key(self):
        upload = self.upload()
        for changes in ({"allow_model_call": "true"}, {"max_requests": True},
                        {"max_requests": "50"}, {"api_key": "do-not-echo-this-secret"},
                        {"idempotency_key": "short"}, {"idempotency_key": "spaces are invalid"}):
            with self.subTest(changes=list(changes)):
                response = self.create(upload, **changes)
                self.assert_error(response, 422, "invalid_request")
                self.assertNotIn("do-not-echo-this-secret", response.text)
        self.dispatcher.assert_not_called()
        self.assert_no_transport()

    def test_reupload_same_bytes_and_key_returns_existing_job_without_duplicate_outbox(self):
        first = self.create().json()
        second = self.create(self.upload(filename="renamed.csv"))
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first["id"], second.json()["id"])
        self.assertEqual(self.store.list_jobs()["total"], 1)
        self.assertEqual(len(self.store.pending_messages()), 1)
        self.assert_no_transport()

    def test_idempotency_key_conflict_on_different_upload_is_409(self):
        self.assertEqual(self.create().status_code, 202)
        response = self.create(self.upload(csv_reviews(20, "Different input")))
        self.assert_error(response, 409, "job_conflict")
        self.assertEqual(self.store.list_jobs()["total"], 1)
        self.assertEqual(len(self.store.pending_messages()), 1)
        self.assert_no_transport()

    def test_broker_failure_returns_202_with_recoverable_durable_outbox(self):
        self.dispatcher.side_effect = ConnectionError("private-redis-password")
        with self.assertLogs("customerinsight.jobs", level="WARNING") as logs:
            response = self.create()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotIn("private-redis-password", response.text + " ".join(logs.output))
        reopened = JobStore(self.root / "workspace")
        self.assertEqual(reopened.get_job(response.json()["id"])["status"], "queued")
        self.assertEqual(len(reopened.pending_messages()), 1)
        self.assert_no_transport()

    def test_public_job_list_and_details_do_not_expose_originals_config_or_credentials(self):
        with patch.object(Config, "OPENAI_API_KEY", "sentinel-secret-key"), \
                patch.object(Config, "OPENAI_BASE_URL", "https://sentinel-private-endpoint.invalid/v1"):
            created = self.create(self.upload(csv_reviews(20, "sentinel-review-text"))).json()
        for path in ("/api/v1/jobs", "/api/v1/jobs/" + created["id"]):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            for forbidden in ("sentinel-secret-key", "sentinel-private-endpoint", "sentinel-review-text",
                              '"config"', '"groups"', '"original"', '"frame_snapshot"', str(self.root)):
                self.assertNotIn(forbidden, response.text)
        self.assert_no_transport()

    def test_cancel_and_resume_require_fresh_explicit_consent(self):
        job = self.create().json()
        path = "/api/v1/jobs/" + job["id"]
        cancelled = self.client.post(path + "/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertTrue(cancelled.json()["can_resume"])
        self.assert_error(self.client.post(path + "/resume", json={"max_requests": 2}),
                          428, "model_consent_required")
        self.assert_error(self.client.post(path + "/resume", json={
            "allow_model_call": True, "max_requests": 1,
        }), 422, "request_budget_too_small")
        self.assertEqual(self.store.get_job(job["id"])["status"], "cancelled")
        resumed = self.client.post(path + "/resume", json={"allow_model_call": True, "max_requests": 2})
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["status"], "queued")
        self.assertFalse(resumed.json()["cancel_requested"])
        self.assertEqual(resumed.json()["max_requests"], 2)
        self.assert_no_transport()

    def test_sent_unknown_outcome_is_visible_and_never_resumed_by_polling(self):
        job = self.create().json()
        group = self.store.claim_group(job["id"], "offline-worker")
        self.store.mark_sent(job["id"], group["id"], group["lease_token"])
        self.store.record_failure(job["id"], group["id"], group["lease_token"], uncertain=True)
        self.dispatcher.reset_mock()
        path = "/api/v1/jobs/" + job["id"]
        current = self.client.get(path)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["status"], "needs_review")
        self.assertEqual(current.json()["request_count"], 1)
        self.assertFalse(current.json()["can_resume"])
        self.assert_error(self.client.post(path + "/resume", json={
            "allow_model_call": True, "max_requests": 50,
        }), 409, "job_not_resumable")
        self.dispatcher.assert_not_called()
        self.assert_no_transport()

    def test_legacy_excel_report_cache_returns_null_job_without_consent_or_enqueue(self):
        data = make_xlsx([("Reviews", [["review"], ["Good battery"], ["Delivery was late"]])])
        frame = read_feedback_xlsx(data, "legacy.xlsx", "Reviews")
        rows = prepare_excel_feedback_rows(frame, "review")
        report = build_customer_report(frame, results_to_dataframe(offline_analysis(rows)), "review")
        report["source_frame"] = frame
        batch_id = self.app.state.service.history.save_batch(
            report, filename="legacy.xlsx", file_bytes=data,
            mapping={"sheet_name": "Reviews", "encoding": None, "text_column": "review",
                     "group_column": None, "date_column": None},
        )
        response = self.create(self.upload(data, "renamed.xlsx"), sheet_name="Reviews",
                               allow_model_call=False, max_requests=None)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["cached"])
        self.assertIsNone(body["id"])
        self.assertEqual(body["batch_id"], batch_id)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["request_count"], 0)
        self.dispatcher.assert_not_called()
        self.assertEqual(self.store.list_jobs()["total"], 0)
        self.assert_no_transport()

    def test_openapi_jobs_have_strict_inputs_typed_public_responses_and_auth(self):
        response = self.client.get("/openapi.json", headers={"Authorization": ""})
        self.assertEqual(response.status_code, 200)
        document = response.json()
        schemas = document["components"]["schemas"]
        for name in ("JobRequest", "ResumeJobRequest", "JobResponse", "JobListResponse"):
            self.assertFalse(schemas[name]["additionalProperties"], name)
        self.assertIn("idempotency_key", schemas["JobRequest"]["required"])
        self.assertEqual(schemas["JobRequest"]["properties"]["allow_model_call"]["default"], False)
        self.assertEqual(schemas["ResumeJobRequest"]["properties"]["allow_model_call"]["default"], False)
        self.assertTrue({"id", "status", "request_count", "total_rows", "total_groups"}.issubset(
            schemas["JobResponse"]["required"]))
        for path, methods in document["paths"].items():
            if path.startswith("/api/v1/jobs"):
                for operation in methods.values():
                    self.assertIn({"HTTPBearer": []}, operation["security"])
                    self.assertEqual(operation["responses"]["401"]["content"]["application/json"]["schema"],
                                     {"$ref": "#/components/schemas/ErrorResponse"})
        self.assert_no_transport()

    def test_queued_deployment_rejects_sync_analysis_without_executing_runner(self):
        upload = self.upload()
        with patch.dict("os.environ", {"APP_ANALYSIS_MODE": "queued"}):
            response = self.client.post("/api/v1/analyses", json={
                "upload_id": upload["upload_id"], "text_column": "review",
                "allow_model_call": True, "max_requests": 50,
            })
        self.assert_error(response, 409, "background_jobs_required")
        self.assert_no_transport()

    def test_direct_mode_sync_analysis_remains_available(self):
        self.analysis.side_effect = offline_analysis
        upload = self.upload()
        response = self.client.post("/api/v1/analyses", json={
            "upload_id": upload["upload_id"], "text_column": "review",
            "allow_model_call": True, "max_requests": 50,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed")
        self.analysis.assert_called_once()
        self.dispatcher.assert_not_called()
        self.assertEqual(self.budget.remaining(), 100)

    def test_missing_malformed_identifiers_and_invalid_pagination_are_local_errors(self):
        for path, status in (("/api/v1/jobs/" + "0" * 32, 404),
                             ("/api/v1/jobs/not-an-id", 422),
                             ("/api/v1/jobs?limit=101", 422),
                             ("/api/v1/jobs?offset=-1", 422)):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), status)
        self.dispatcher.assert_not_called()
        self.assert_no_transport()


class QueuedProductTraceTests(unittest.TestCase):
    """Agent/direct transport and worker transports share upstream capacity."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.budget = ProductRequestBudget(Path(self.temp.name) / "usage.sqlite3", daily_limit=3)
        self.gate = Mock()
        self.gate.acquire.return_value = "shared-request-token"
        self.gates = patch("core.product_service._shared_request_gate", return_value=self.gate)
        self.gates.start()
        self.addCleanup(self.gates.stop)

    def test_gate_release_on_response_preserves_atomic_quota_and_usage(self):
        control = ProductRunControl(self.budget, 1)
        trace = control.trace("agent-turn")
        trace.before_attempt()
        self.assertEqual(self.budget.remaining(), 2)
        self.assertEqual(control.attempts, 1)
        self.gate.acquire.assert_called_once_with()
        trace.response_received({"total_tokens": 17})
        self.gate.release.assert_called_once_with("shared-request-token")
        self.assertEqual(control.usage["agent-turn"], [{"total_tokens": 17}])

    def test_gate_release_on_provider_failure_and_stop_on_auth_error(self):
        control = ProductRunControl(self.budget, 1)
        trace = control.trace("agent-turn")
        trace.before_attempt()
        error = RuntimeError("provider returned 401")
        error.status_code = 401
        trace.request_failed(error)
        self.gate.release.assert_called_once_with("shared-request-token")
        self.assertTrue(control.stopped)
        self.assertEqual(self.budget.remaining(), 2)

    def test_gate_release_when_daily_quota_rejects_transport(self):
        for _ in range(3):
            self.budget.reserve("already-consumed")
        control = ProductRunControl(self.budget, 1)
        with self.assertRaises(ProductLimitError):
            control.trace("agent-turn").before_attempt()
        self.gate.release.assert_called_once_with("shared-request-token")
        self.assertEqual(control.attempts, 0)
        self.assertEqual(self.budget.remaining(), 0)

    def test_unavailable_gate_does_not_reserve_quota_or_count_paid_request(self):
        self.gate.acquire.side_effect = ProductLimitError("Shared service is unavailable")
        control = ProductRunControl(self.budget, 1)
        with self.assertRaises(ProductLimitError):
            control.trace("agent-turn").before_attempt()
        self.assertEqual(control.attempts, 0)
        self.assertEqual(self.budget.remaining(), 3)
        self.gate.release.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
