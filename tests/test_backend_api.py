"""HTTP boundary tests with disposable workspaces and deterministic model doubles.

These tests never load a real customer batch or send a model request. The API
factory's injected runners exercise orchestration separately from SDK transport.
"""

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app import create_app
from core.analysis_agent import AnalysisAgentResult, report_fingerprint
from core.config import Config
from core.csv_io import results_to_dataframe
from core.customer_report import build_customer_report
from core.excel_io import prepare_excel_feedback_rows, read_feedback_xlsx
from core.product_service import CustomerAnalysisResults, ProductRequestBudget
from models.enums import ProcessingStatus
from models.models import FeedbackResult
from tests.helpers import offline_temp_root
from tests.test_excel_io import make_xlsx
from tests.test_history_store import sample_report


TOKEN = "isolated-backend-test-token"
CSV_BYTES = (
    "review,store,date\n"
    "Battery lasts all day,North,2026-10-01\n"
    "Delivery was late,South,2026-10-02\n"
).encode("utf-8-sig")


def offline_analysis(rows, **options):
    """Honor skipped rows, emit completed rows, and preserve partial failures."""
    results = []
    for row in rows:
        if row.processing_status != ProcessingStatus.PENDING:
            result = row
        elif row.original_text == "FAIL_CASE":
            result = row.model_copy(update={
                "processing_status": ProcessingStatus.FAILED,
                "error_message": "Offline fixture: model unavailable.",
            })
        else:
            result = FeedbackResult.model_validate({
                **row.model_dump(),
                "processing_status": "success",
                "sentiment": "negative" if "late" in row.original_text else "positive",
                "category": "delivery" if "late" in row.original_text else "product_quality",
                "priority": "medium" if "late" in row.original_text else "low",
                "summary": row.original_text,
            })
        results.append(result)
        callback = options.get("result_callback")
        if callback is not None and result.processing_status != ProcessingStatus.PENDING:
            callback(result)
    meta = {
        "mode": "offline-fixture", "prompt_version": "test-only",
        "planned_requests": 1, "actual_requests": 0,
        "total_tokens": None, "run_status": "finished",
    }
    if options.get("run_state") is not None:
        options["run_state"]["analysis_meta"] = meta
    return CustomerAnalysisResults(results, meta)


def offline_agent(report, question, **options):
    return AnalysisAgentResult(
        status="completed", question=question,
        conclusion="The saved report contains delivery feedback.",
        conclusion_metrics=[], findings=[], actions=[],
        scope_note="Only the explicitly selected report is in scope.",
        tool_trace=["get_overview"], model_calls=0, tool_calls=1,
        total_tokens=None, usage_reported_requests=0,
        report_fingerprint=report_fingerprint(report), fallback_reason=None,
        answer_origin="llm",
    )


class BackendApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=offline_temp_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.analysis = Mock(side_effect=offline_analysis)
        self.agent = Mock(side_effect=offline_agent)
        self.budget = ProductRequestBudget(self.root / "usage.sqlite3", daily_limit=100)
        self.app = create_app(
            data_dir=self.root / "workspace", api_token=TOKEN,
            budget=self.budget, analysis_runner=self.analysis,
            agent_runner=self.agent,
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = f"Bearer {TOKEN}"

    def upload(self, data=CSV_BYTES, filename="reviews.csv"):
        result = self.client.post(
            "/api/v1/uploads", files={"file": (filename, io.BytesIO(data))},
        )
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def analyze(self, upload=None, **overrides):
        upload = self.upload() if upload is None else upload
        payload = {
            "upload_id": upload["upload_id"], "text_column": "review",
            "group_column": "store", "date_column": "date",
            "allow_model_call": True, "max_requests": 10,
        }
        payload.update(overrides)
        return self.client.post("/api/v1/analyses", json=payload)

    def saved_batch(self, label="test"):
        report = sample_report(f"杯子保温很好 {label}")
        return self.app.state.service.history.save_batch(
            report, filename=f"{label}.csv",
            file_bytes=f"评价,门店\n杯子保温很好 {label},甲店\n".encode("utf-8-sig"),
            mapping={"text_column": "评价", "group_column": "门店", "sheet_name": None},
        )

    def assert_error(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertIsInstance(body.get("request_id"), str)
        self.assertTrue(body["request_id"])
        self.assertIsInstance(body["error"]["code"], str)
        self.assertIsInstance(body["error"]["message"], str)
        return body

    def test_health_is_public_and_contains_no_workspace_configuration(self):
        response = self.client.get("/healthz", headers={"Authorization": ""})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("OPENAI", response.text)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_openapi_publishes_typed_results_errors_and_protected_data_routes(self):
        response = self.client.get("/openapi.json", headers={"Authorization": ""})
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        schemas = document["components"]["schemas"]
        required = {
            "UploadResponse": {"upload_id", "filename", "size_bytes", "sheet_names", "expires_at"},
            "PreviewResponse": {"columns", "rows", "total_rows", "suggested_mapping"},
            "AnalysisResponse": {"batch_id", "cached", "status", "metadata"},
            "BatchListResponse": {"items", "total", "limit", "offset"},
            "RowsResponse": {"columns", "items", "total", "limit", "offset"},
            "ChatResponse": {"result", "turn_count"},
            "ErrorResponse": {"error", "request_id"},
        }
        for name, fields in required.items():
            with self.subTest(schema=name):
                self.assertTrue(fields.issubset(schemas[name]["required"]))
        self.assertEqual(schemas["AnalysisRequest"]["properties"]["allow_model_call"]["default"], False)
        self.assertFalse(schemas["AnalysisRequest"]["additionalProperties"])
        for path, operations in document["paths"].items():
            if path.startswith("/api/"):
                for method, operation in operations.items():
                    with self.subTest(path=path, method=method):
                        if path == "/api/v1/auth/login":
                            self.assertNotIn("security", operation)
                        else:
                            self.assertIn({"HTTPBearer": []}, operation["security"])
                        self.assertEqual(operation["responses"]["401"]["content"]["application/json"]["schema"],
                                         {"$ref": "#/components/schemas/ErrorResponse"})

    def test_explicit_factory_workspace_also_isolates_its_default_budget_database(self):
        workspace = self.root / "factory-isolation"
        unrelated = self.root / "must-not-be-created.sqlite3"
        with patch.dict("os.environ", {"APP_USAGE_DB_PATH": str(unrelated)}):
            app = create_app(data_dir=workspace, api_token=TOKEN)
            budget = app.state.service.budget
            self.assertEqual(budget.path.resolve(), (workspace / "api_usage.sqlite3").resolve())
            self.assertGreaterEqual(budget.remaining(), 0)
        self.assertFalse(unrelated.exists())

    def test_all_data_routes_require_authentication_before_using_workspace(self):
        paths = [
            ("POST", "/api/v1/uploads"),
            ("GET", "/api/v1/uploads/" + "0" * 32 + "/preview"),
            ("POST", "/api/v1/analyses"),
            ("GET", "/api/v1/batches"),
            ("GET", "/api/v1/batches/" + "0" * 32),
            ("GET", "/api/v1/batches/" + "0" * 32 + "/rows"),
            ("GET", "/api/v1/batches/" + "0" * 32 + "/exports/original"),
            ("GET", "/api/v1/batches/" + "0" * 32 + "/chat"),
            ("POST", "/api/v1/batches/" + "0" * 32 + "/chat"),
        ]
        for method, path in paths:
            with self.subTest(method=method, path=path):
                self.assert_error(
                    self.client.request(method, path, headers={"Authorization": ""}), 401,
                )
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_wrong_or_non_bearer_token_is_rejected(self):
        for authorization in ("Bearer wrong", f"Basic {TOKEN}", "Bearer"):
            with self.subTest(authorization=authorization):
                self.assert_error(self.client.get(
                    "/api/v1/batches", headers={"Authorization": authorization},
                ), 401)

    def test_unconfigured_token_fails_closed_instead_of_creating_public_api(self):
        with patch.dict("os.environ", {"APP_API_TOKEN": "", "APP_ACCESS_PASSWORD": ""}):
            app = create_app(data_dir=self.root / "unconfigured", api_token="")
            with TestClient(app) as client:
                self.assert_error(client.get("/api/v1/batches"), 503)
                self.assertEqual(client.get("/healthz").status_code, 200)

    def test_csv_upload_preview_preserves_fields_and_never_calls_model(self):
        uploaded = self.upload()
        response = self.client.get(f"/api/v1/uploads/{uploaded['upload_id']}/preview")
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["filename"], "reviews.csv")
        self.assertEqual(preview["columns"], ["review", "store", "date"])
        self.assertEqual(preview["total_rows"], 2)
        self.assertEqual(preview["rows"][0]["review"], "Battery lasts all day")
        self.assertEqual(preview["suggested_mapping"]["text_column"], "review")
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_excel_preview_honors_explicit_worksheet_and_physical_rows(self):
        data = make_xlsx([
            ("说明", [["说明"], ["请选择评价工作表"]]),
            ("评价", [["review", "store"], ["good", "North"], [None, None], ["late", "South"]]),
        ])
        uploaded = self.upload(data, "评价.xlsx")
        self.assertEqual(uploaded["sheet_names"], ["说明", "评价"])
        response = self.client.get(
            f"/api/v1/uploads/{uploaded['upload_id']}/preview", params={"sheet_name": "评价"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_rows"], 3)
        self.assertEqual(response.json()["rows"][0]["review"], "good")
        analysis = self.analyze(uploaded, sheet_name="评价", date_column=None)
        self.assertEqual(analysis.status_code, 200, analysis.text)
        supplied_rows = self.analysis.call_args.args[0]
        self.assertEqual([row.source_row for row in supplied_rows], [2, 3, 4])
        self.assertEqual(supplied_rows[1].processing_status, ProcessingStatus.SKIPPED_EMPTY)

    def test_corrupt_excel_unsupported_extensions_and_empty_upload_are_rejected(self):
        for data, filename in ((b"not a workbook", "a.xlsx"), (CSV_BYTES, "a.xls"),
                               (CSV_BYTES, "a.exe"), (b"", "a.csv")):
            with self.subTest(filename=filename, data=data[:8]):
                response = self.client.post("/api/v1/uploads", files={"file": (filename, data)})
                self.assert_error(response, 422)
        self.analysis.assert_not_called()

    def test_file_size_cap_rejects_oversized_upload_before_model_or_history(self):
        response = self.client.post(
            "/api/v1/uploads", files={"file": ("large.csv", b"a" * (Config.MAX_CSV_BYTES + 1))},
        )
        self.assert_error(response, 413)
        self.analysis.assert_not_called()
        self.assertEqual(self.app.state.service.history.list_batches(), [])

    def test_missing_upload_invalid_mapping_and_unknown_sheet_are_local_errors(self):
        self.assert_error(self.analyze({"upload_id": "0" * 32}), 404)
        uploaded = self.upload()
        self.assert_error(self.analyze(uploaded, text_column="missing"), 422)
        excel = self.upload(make_xlsx([("S", [["review"], ["good"]])]), "reviews.xlsx")
        self.assert_error(self.client.get(
            f"/api/v1/uploads/{excel['upload_id']}/preview", params={"sheet_name": "missing"},
        ), 422)
        self.analysis.assert_not_called()

    def test_analysis_requires_explicit_model_consent_and_valid_request_budget(self):
        uploaded = self.upload()
        self.assert_error(self.analyze(uploaded, allow_model_call=False), 428)
        for cap in (0, -1, True, "ten"):
            with self.subTest(cap=cap):
                self.assert_error(self.analyze(uploaded, max_requests=cap), 422)
        self.analysis.assert_not_called()

    def test_clients_cannot_supply_a_server_file_path_or_unknown_analysis_fields(self):
        uploaded = self.upload()
        self.assert_error(self.analyze(uploaded, file_path=".env"), 422)
        self.assert_error(self.analyze(uploaded, history_root=str(self.root)), 422)
        self.analysis.assert_not_called()

    def test_exhausted_request_budget_prevents_analysis_and_agent_execution(self):
        exhausted = ProductRequestBudget(self.root / "exhausted.sqlite3", daily_limit=1)
        exhausted.reserve("already-used-offline")
        self.app.state.service.budget = exhausted
        self.assert_error(self.analyze(), 429)
        batch_id = self.saved_batch()
        self.assert_error(self.client.post(f"/api/v1/batches/{batch_id}/chat", json={
            "question": "What should improve?", "allow_model_call": True, "max_requests": 10,
        }), 429)
        self.analysis.assert_not_called()
        self.agent.assert_not_called()

    def test_overlapping_duplicate_analyses_do_not_start_two_model_runs(self):
        uploaded = self.upload()
        entered, release = threading.Event(), threading.Event()
        outcomes = []

        def slow_analysis(rows, **options):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("offline test timed out")
            return offline_analysis(rows, **options)

        self.analysis.side_effect = slow_analysis
        worker = threading.Thread(target=lambda: outcomes.append(self.analyze(uploaded)))
        worker.start()
        self.addCleanup(worker.join, 6)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        try:
            self.assert_error(self.analyze(uploaded), 409)
        finally:
            release.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0].status_code, 200, outcomes[0].text)
        self.analysis.assert_called_once()

    def test_duplicate_upload_and_mapping_reuse_saved_report_without_new_consent(self):
        first = self.analyze()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["cached"])
        second = self.analyze(self.upload(filename="renamed.csv"), allow_model_call=False)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["cached"])
        self.assertEqual(second.json()["batch_id"], first.json()["batch_id"])
        self.analysis.assert_called_once()
        self.assertEqual(len(self.app.state.service.history.list_batches()), 1)

    def test_changed_mapping_is_distinct_and_force_needs_fresh_consent(self):
        uploaded = self.upload()
        first = self.analyze(uploaded)
        self.assertEqual(first.status_code, 200, first.text)
        self.assert_error(self.analyze(uploaded, force=True, allow_model_call=False), 428)
        changed = self.analyze(uploaded, group_column=None)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertNotEqual(first.json()["batch_id"], changed.json()["batch_id"])
        self.assertEqual(self.analysis.call_count, 2)

    def test_existing_browser_excel_batch_is_reused_without_new_model_consent(self):
        data = make_xlsx([("S", [["review"], ["Good battery"], ["Delivery was late"]])])
        source = read_feedback_xlsx(data, "legacy.xlsx", "S")
        rows = prepare_excel_feedback_rows(source, "review")
        report = build_customer_report(source, results_to_dataframe(offline_analysis(rows)), "review")
        report["source_frame"] = source
        legacy_id = self.app.state.service.history.save_batch(
            report, filename="legacy.xlsx", file_bytes=data,
            mapping={"sheet_name": "S", "encoding": None, "text_column": "review",
                     "group_column": None, "date_column": None},
        )
        response = self.analyze(
            self.upload(data, "same-workbook.xlsx"), sheet_name="S", group_column=None,
            date_column=None, allow_model_call=False, max_requests=None,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["cached"])
        self.assertEqual(response.json()["batch_id"], legacy_id)
        self.analysis.assert_not_called()
        self.assertEqual(len(self.app.state.service.history.list_batches()), 1)

    def test_column_roles_cannot_overlap(self):
        uploaded = self.upload()
        for fields in ({"group_column": "review"}, {"date_column": "review"},
                       {"date_column": "store"}):
            with self.subTest(fields=fields):
                self.assert_error(self.analyze(uploaded, **fields), 422)
        self.analysis.assert_not_called()

    def test_partial_failure_remains_failed_and_survives_service_restart(self):
        data = b"review,store,date\nWorks well,North,2026-10-01\nFAIL_CASE,South,2026-10-02\n"
        response = self.analyze(self.upload(data))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "partial")
        batch_id = response.json()["batch_id"]
        report = self.app.state.service.history.load_batch(batch_id)["report"]
        self.assertEqual(report["metrics"]["success_rows"], 1)
        self.assertEqual(report["metrics"]["failed_rows"], 1)
        self.assertEqual(report["metrics"]["neutral_rows"], 0)
        restarted = create_app(
            data_dir=self.root / "workspace", api_token=TOKEN,
            budget=self.budget, analysis_runner=self.analysis, agent_runner=self.agent,
        )
        with TestClient(restarted) as client:
            restored = client.get(f"/api/v1/batches/{batch_id}", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["report"]["metrics"]["failed_rows"], 1)
        self.analysis.assert_called_once()

    def test_interrupted_analysis_keeps_callback_results_and_pending_rows(self):
        def interrupted(rows, **options):
            completed = offline_analysis(rows[:1])[0]
            options["result_callback"](completed)
            options["run_state"]["analysis_meta"] = {"actual_requests": 1, "run_status": "interrupted"}
            raise RuntimeError("PRIVATE_EXCEPTION_MUST_NOT_APPEAR")

        self.analysis.side_effect = interrupted
        response = self.analyze()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "partial")
        self.assertNotIn("PRIVATE_EXCEPTION_MUST_NOT_APPEAR", response.text)
        report = self.app.state.service.history.load_batch(response.json()["batch_id"])["report"]
        self.assertEqual(report["metrics"]["success_rows"], 1)
        self.assertEqual(report["metrics"]["pending_rows"], 1)

    def test_history_pagination_is_stable_bounded_and_contains_no_original_bytes(self):
        identifiers = {self.saved_batch(str(index)) for index in range(3)}
        first = self.client.get("/api/v1/batches", params={"limit": 2, "offset": 0})
        second = self.client.get("/api/v1/batches", params={"limit": 2, "offset": 2})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["total"], 3)
        first_ids = {item["id"] for item in first.json()["items"]}
        second_ids = {item["id"] for item in second.json()["items"]}
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids, identifiers)
        self.assertNotIn("original_bytes", first.text)
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            self.assert_error(self.client.get("/api/v1/batches", params=params), 422)

    def test_report_json_handles_dates_numpy_nan_and_uses_no_pickle_payload(self):
        batch_id = self.saved_batch("typed-values")
        response = self.client.get(f"/api/v1/batches/{batch_id}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        json.dumps(body, allow_nan=False)
        self.assertEqual(body["report"]["metrics"]["total_rows"], 2)
        self.assertNotIn("original_bytes", response.text)
        self.assertNotIn("__pickle__", response.text)
        rows = self.client.get(f"/api/v1/batches/{batch_id}/rows")
        self.assertEqual(rows.status_code, 200, rows.text)
        json.dumps(rows.json(), allow_nan=False)
        self.assertEqual(rows.json()["items"][0]["日期"], "2026-10-01")
        self.assertIsNone(rows.json()["items"][1]["金额"])

    def test_rows_export_pagination_preserves_total_and_bounds(self):
        batch_id = self.saved_batch()
        response = self.client.get(f"/api/v1/batches/{batch_id}/rows", params={"limit": 1, "offset": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertIn("评价", response.json()["columns"])
        self.assertEqual(response.json()["items"][0]["评价"], "希望减少塑料包装")
        self.assert_error(self.client.get(f"/api/v1/batches/{batch_id}/rows", params={"offset": -1}), 422)

    def test_exports_return_exact_original_and_downloadable_safe_report_formats(self):
        response = self.analyze()
        self.assertEqual(response.status_code, 200, response.text)
        batch_id = response.json()["batch_id"]
        original = self.client.get(f"/api/v1/batches/{batch_id}/exports/original")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.content, CSV_BYTES)
        for kind, marker in (("csv", "Battery lasts all day"), ("html", "<html")):
            with self.subTest(kind=kind):
                exported = self.client.get(f"/api/v1/batches/{batch_id}/exports/{kind}")
                self.assertEqual(exported.status_code, 200, exported.text)
                self.assertIn(marker, exported.content.decode("utf-8-sig"))
                self.assertIn("attachment", exported.headers["content-disposition"])
        self.assert_error(self.client.get(f"/api/v1/batches/{batch_id}/exports/executable"), 422)

    def test_missing_and_malformed_batch_ids_do_not_expose_filesystem_or_sql(self):
        for suffix in ("", "/rows", "/exports/original", "/chat"):
            self.assert_error(self.client.get("/api/v1/batches/" + "0" * 32 + suffix), 404)
        response = self.client.get("/api/v1/batches/invalid-id")
        self.assert_error(response, 422)
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("sqlite", response.text.lower())

    def test_chat_requires_consent_and_persists_history_for_next_turn(self):
        batch_id = self.saved_batch()
        url = f"/api/v1/batches/{batch_id}/chat"
        self.assert_error(self.client.post(url, json={"question": "What should improve?"}), 428)
        self.agent.assert_not_called()
        for index, question in enumerate(("What should improve?", "Which review supports that?"), 1):
            response = self.client.post(url, json={
                "question": question, "allow_model_call": True, "max_requests": 10,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["turn_count"], index)
            self.assertEqual(response.json()["result"]["question"], question)
            self.assertEqual(len(self.agent.call_args.kwargs.get("history", [])), index - 1)
        saved = self.client.get(url)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["total"], 2)
        self.assertEqual(self.agent.call_count, 2)

    def test_chat_comparison_scope_validates_selected_batch_and_keeps_scope_separate(self):
        current, previous = self.saved_batch("current"), self.saved_batch("previous")
        url = f"/api/v1/batches/{current}/chat"
        base = {"question": "Compare these batches", "allow_model_call": True, "max_requests": 10}
        self.assert_error(self.client.post(url, json={**base, "comparison_id": current}), 422)
        self.assert_error(self.client.post(url, json={**base, "comparison_id": "0" * 32}), 404)
        self.agent.assert_not_called()
        response = self.client.post(url, json={**base, "comparison_id": previous})
        self.assertEqual(response.status_code, 200, response.text)
        supplied_report = self.agent.call_args.args[0]
        self.assertEqual(supplied_report["historical_context"]["batch"]["id"], previous)
        without_comparison = self.client.get(url)
        with_comparison = self.client.get(url, params={"comparison_id": previous})
        self.assertEqual(without_comparison.json()["total"], 0)
        self.assertEqual(with_comparison.json()["total"], 1)

    def test_invalid_or_empty_chat_questions_never_call_runner(self):
        batch_id = self.saved_batch()
        url = f"/api/v1/batches/{batch_id}/chat"
        for question in ("", "   ", 1, "x" * 20001):
            with self.subTest(question=str(question)[:20]):
                self.assert_error(self.client.post(url, json={
                    "question": question, "allow_model_call": True,
                }), 422)
        self.agent.assert_not_called()

    def test_unexpected_errors_redact_private_values_in_responses_and_logs(self):
        secret = "PRIVATE_TEST_EXCEPTION_BODY_7e493"
        with TestClient(self.app, raise_server_exceptions=False) as client:
            with patch.object(self.app.state.service, "list_batches", side_effect=RuntimeError(secret)):
                with self.assertLogs("customerinsight.events", level="INFO") as logs:
                    response = client.get("/api/v1/batches", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assert_error(response, 500)
        events = [record.event_payload for record in logs.records]
        error = next(event for event in events if event["event"] == "http_error")
        self.assertEqual(error["request_id"], response.headers["x-request-id"])
        self.assertEqual(error["error_type"], "RuntimeError")
        combined = response.text + json.dumps(events)
        for private in (secret, TOKEN, str(self.root)):
            self.assertNotIn(private, combined)

    def test_request_ids_correlate_responses_and_invalid_ids_are_replaced(self):
        valid = "offline-request-12345"
        response = self.client.get("/healthz", headers={"X-Request-ID": valid})
        self.assertEqual(response.headers["x-request-id"], valid)
        response = self.client.get("/healthz", headers={"X-Request-ID": "x" * 100})
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
