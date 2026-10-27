"""Offline request-budget verification using SDK transports and durable journals."""

from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError, AuthenticationError, BadRequestError, InternalServerError, OpenAI, PermissionDeniedError

from ai.analyzer import analyze_feedback
from core.config import Config
from evaluation.control import PersistentRequestBudget, RequestControlStopped, RunControl, RunJournal
from evaluation.reports import load_report, save_report
from evaluation.runner import compare_reports, run_evaluation, select_samples
from tests.helpers import fake_client, fake_response, valid_analysis
from tests.test_evaluation_runner import samples


def status_error(kind, code):
    response = httpx.Response(code, request=httpx.Request("POST", "https://offline.invalid/v1/responses"))
    return kind("private body must remain hidden", response=response, body={})


class EvaluationControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.budget = PersistentRequestBudget(self.path / "budget.json")
        self.control = RunControl(180, budget=self.budget)
        transport = patch.object(Config, "API_MODE", "responses")
        transport.start()
        self.addCleanup(transport.stop)

    def evaluate(self, client, **kwargs):
        return run_evaluation(samples(), mode="live", client=client, control=self.control,
                              journal_dir=self.path / "journals", max_workers=1, **kwargs)

    def test_stable_selection_and_invalid_selection_never_request(self):
        data = samples()
        reversed_rows = list(reversed(data))
        self.assertEqual([row.sample_id for row in select_samples(data, "dev", limit=2)],
                         [row.sample_id for row in select_samples(reversed_rows, "dev", limit=2)])
        selected = select_samples(data, "dev", sample_ids=[data[2].sample_id, data[0].sample_id])
        self.assertEqual([row.sample_id for row in selected], [data[0].sample_id, data[2].sample_id])
        client = fake_client()
        for selection in ({"sample_ids": ["missing"]}, {"sample_ids": [data[0].sample_id] * 2},
                          {"sample_ids": [data[0].sample_id], "limit": 1}, {"limit": True}, {"limit": 0}):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.evaluate(client, **selection)
        client.responses.parse.assert_not_called()

    def test_retry_override_does_not_change_config_and_attempts_include_retries(self):
        client = fake_client()
        error = APIConnectionError(request=httpx.Request("POST", "https://offline.invalid"))
        client.responses.parse.side_effect = [error, fake_response()]
        with patch.object(Config, "MAX_RETRIES", 8), patch("ai.analyzer.time.sleep"):
            report = self.evaluate(client, limit=1, max_retries=1)
            self.assertEqual(Config.MAX_RETRIES, 8)
        row = report["predictions"][0]
        self.assertEqual(row["request_attempts"], 2)
        self.assertEqual(row["token_usage"], ["不可用", "不可用"])
        self.assertEqual(report["metadata"]["request_attempts_actual"], 2)
        self.assertEqual(self.budget.snapshot()["attempts_used"], 2)

    def test_hard_limit_blocks_retry_before_transport_and_retains_failure(self):
        self.control = RunControl(1, budget=self.budget)
        client = fake_client()
        client.responses.parse.side_effect = APIConnectionError(request=httpx.Request("POST", "https://offline.invalid"))
        with patch("ai.analyzer.time.sleep"):
            report = self.evaluate(client, max_retries=5)
        self.assertEqual(client.responses.parse.call_count, 1)
        self.assertEqual([row["status"] for row in report["predictions"]], ["failed", "not_executed", "not_executed"])
        self.assertEqual(report["metadata"]["stop_reason"], "request_limit")
        self.assertEqual(report["metrics"]["failed_count"], 1)
        self.assertEqual(report["metrics"]["sample_count"], 1)
        self.assertEqual(self.control.consecutive_service_errors, 1)

    def test_shared_invocation_limit_applies_to_both_versions(self):
        self.control = RunControl(4, budget=self.budget)
        client = fake_client()
        a = self.evaluate(client, prompt_version="v1")
        b = self.evaluate(client, prompt_version="v2")
        self.assertEqual(client.responses.parse.call_count, 4)
        self.assertEqual(a["metadata"]["request_attempts_actual"], 3)
        self.assertEqual(b["metadata"]["request_attempts_actual"], 1)
        self.assertEqual(b["metadata"]["cumulative_request_attempts"], 4)
        with self.assertRaises(ValueError):
            compare_reports([a, b])

    def test_180_cumulative_limit_survives_new_control_and_cannot_be_enlarged(self):
        for number in range(179):
            self.assertTrue(self.budget.reserve(run_id="setup", sample_id=str(number), prompt_version="v1", sample_attempt=1))
        self.control = RunControl(180, budget=PersistentRequestBudget(self.budget.path))
        client = fake_client()
        report = self.evaluate(client)
        self.assertEqual(client.responses.parse.call_count, 1)
        self.assertEqual(report["metadata"]["stop_reason"], "cumulative_budget")
        self.assertEqual(report["metadata"]["cumulative_request_attempts"], 180)
        another = RunControl(180, budget=PersistentRequestBudget(self.budget.path))
        with self.assertRaises(RequestControlStopped):
            another.before_attempt("later", "sample", "v1", 1)
        self.assertEqual(another.snapshot("later")["cumulative_request_attempts"], 180)
        with self.assertRaises(ValueError):
            RunControl(181, budget=self.budget)

    def test_concurrent_reservations_cannot_exceed_hard_limit(self):
        control = RunControl(5, budget=self.budget)

        def reserve(number):
            try:
                control.before_attempt("same", str(number), "v1", 1)
                return True
            except RequestControlStopped:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(reserve, range(30))), 5)
        self.assertEqual(self.budget.snapshot()["attempts_used"], 5)

    def test_authentication_and_permission_stop_new_submissions(self):
        for error in (status_error(AuthenticationError, 401), status_error(PermissionDeniedError, 403)):
            with self.subTest(code=error.status_code):
                self.control = RunControl(180, budget=self.budget)
                client = fake_client()
                client.responses.parse.side_effect = error
                report = self.evaluate(client)
                self.assertEqual(client.responses.parse.call_count, 1)
                self.assertEqual(report["metadata"]["run_status"], "incomplete")
                self.assertEqual(report["metadata"]["stop_reason"], "authentication_error")
                self.assertEqual(report["predictions"][0]["failure_type"], "authentication")
                self.assertEqual(report["metadata"]["not_executed_count"], 2)
                self.assertNotIn("private body", json.dumps(report))

    def test_three_service_errors_stop_application_retries(self):
        client = fake_client()
        client.responses.parse.side_effect = status_error(InternalServerError, 503)
        with patch("ai.analyzer.time.sleep"):
            report = self.evaluate(client, max_retries=20)
        self.assertEqual(client.responses.parse.call_count, 3)
        self.assertEqual(report["metadata"]["stop_reason"], "consecutive_service_errors")
        self.assertEqual(report["predictions"][0]["request_attempts"], 3)
        self.assertEqual(report["metadata"]["not_executed_count"], 2)

    def test_success_and_permanent_request_error_break_service_error_streak(self):
        control = self.control
        error = status_error(InternalServerError, 503)
        control.request_failed(error)
        control.request_failed(error)
        control.request_failed(status_error(BadRequestError, 400))
        self.assertEqual(control.consecutive_service_errors, 0)
        control.request_failed(error)
        control.response_received()
        control.request_failed(error)
        self.assertFalse(control.stopped)

    def test_journal_and_export_preserve_partial_evidence_without_mock_fill(self):
        self.control = RunControl(1, budget=self.budget)
        report = self.evaluate(fake_client())
        journal = Path(report["metadata"]["journal_path"])
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["event"], "started")
        self.assertEqual([item["prediction"]["status"] for item in events if item["event"] == "prediction"],
                         ["success", "not_executed", "not_executed"])
        self.assertEqual(events[-1]["metadata"]["run_status"], "incomplete")
        paths = save_report(report, self.path / "reports")
        self.assertEqual(load_report(paths["report"]), report)
        self.assertTrue(all(row["prediction_source"] == "live" for row in report["predictions"]))
        self.assertEqual(report["metrics"]["not_executed_count"], 2)

    def test_token_usage_is_actual_or_unavailable_and_output_is_complete(self):
        client = fake_client()
        client.responses.parse.return_value = fake_response(usage={
            "input_tokens": 18, "output_tokens": 35, "total_tokens": 53,
            "output_tokens_details": {"reasoning_tokens": 7}, "api_key": "secret-provider-key"})
        report = self.evaluate(client, limit=1)
        row = report["predictions"][0]
        self.assertEqual(row["token_usage"], [{"input_tokens": 18, "output_tokens": 35,
                         "total_tokens": 53, "output_tokens_details": {"reasoning_tokens": 7}}])
        self.assertEqual(row["actionable_items"], valid_analysis()["actionable_items"])
        self.assertNotIn("secret-provider-key", json.dumps(report))
        self.assertEqual(row["request_attempts"], 1)
        self.assertEqual(client.max_retries, 0)

    def test_injected_real_sdk_retries_are_disabled_and_count_matches_transport(self):
        captured = []

        def transport(request):
            captured.append(request.url.path)
            return httpx.Response(503, json={"error": {"message": "offline-error"}})

        http_client = httpx.Client(transport=httpx.MockTransport(transport))
        client = OpenAI(api_key="offline-only", base_url="https://offline.invalid/v1", max_retries=7, http_client=http_client)
        self.addCleanup(client.close)
        report = self.evaluate(client, max_retries=0)
        self.assertEqual(len(captured), 3)
        self.assertEqual(report["metadata"]["request_attempts_actual"], 3)
        self.assertEqual(report["metadata"]["stop_reason"], "consecutive_service_errors")
        self.assertEqual(client.max_retries, 7)

    def test_corrupt_budget_fails_closed_before_client_and_transport(self):
        self.budget.path.write_text('{"request_limit": 9999}', encoding="utf-8")
        client = fake_client()
        with self.assertRaises(ValueError):
            self.evaluate(client)
        client.responses.parse.assert_not_called()

    def test_budget_is_persisted_before_each_actual_request(self):
        client = fake_client()
        observed = []

        def response(**kwargs):
            observed.append(json.loads(self.budget.path.read_text(encoding="utf-8"))["attempts_used"])
            return fake_response()

        client.responses.parse.side_effect = response
        report = self.evaluate(client)
        self.assertEqual(observed, [1, 2, 3])
        self.assertEqual(report["metadata"]["request_attempts_actual"], 3)

    def test_temporary_replace_denial_preserves_atomicity_and_counts_one_request(self):
        self.budget.reserve(run_id="setup", sample_id="previous", prompt_version="v1", sample_attempt=1)
        original = self.budget.path.read_bytes()
        original_replace = Path.replace
        replacement_attempts = []
        client = fake_client()

        def briefly_denied(source, target):
            self.assertEqual(Path(target), self.budget.path)
            replacement_attempts.append(source)
            self.assertEqual(self.budget.path.read_bytes(), original)
            self.assertEqual(json.loads(source.read_text(encoding="utf-8"))["attempts_used"], 2)
            self.assertTrue(self.budget.path.with_suffix(".json.lock").exists())
            client.responses.parse.assert_not_called()
            if len(replacement_attempts) <= 2:
                raise PermissionError("simulated short Windows file-sharing denial")
            return original_replace(source, target)

        with patch.object(Path, "replace", briefly_denied), patch("evaluation.control.time.sleep") as pause:
            report = self.evaluate(client, limit=1)
        self.assertEqual(len(replacement_attempts), 3)
        self.assertEqual(len(set(replacement_attempts)), 1)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [0.01, 0.02])
        client.responses.parse.assert_called_once()
        self.assertEqual(report["predictions"][0]["request_attempts"], 1)
        self.assertEqual(report["metadata"]["request_attempts_actual"], 1)
        self.assertEqual(self.budget.snapshot()["attempts_used"], 2)

    def test_permanent_replace_denial_keeps_prior_ledger_and_blocks_transport(self):
        self.budget.reserve(run_id="setup", sample_id="previous", prompt_version="v1", sample_attempt=1)
        original = self.budget.path.read_bytes()
        client = fake_client()
        trace = self.control.trace("blocked-run", "sample", "v1")
        with patch.object(Path, "replace", side_effect=PermissionError("simulated permanent denial")) as replace, patch("evaluation.control.time.sleep") as pause:
            with self.assertRaises(PermissionError):
                analyze_feedback("Offline feedback", client=client, max_retries=0, request_control=trace)
        self.assertEqual(replace.call_count, 5)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [0.01, 0.02, 0.04, 0.08])
        client.responses.parse.assert_not_called()
        self.assertEqual(self.budget.path.read_bytes(), original)
        self.assertEqual(self.budget.snapshot()["attempts_used"], 1)
        self.assertEqual(self.control.attempts_used, 0)
        self.assertEqual(trace.attempts, 0)
        self.assertFalse(self.budget.path.with_suffix(".json.lock").exists())

    def test_other_atomic_replace_errors_are_not_retried(self):
        client = fake_client()
        with patch.object(Path, "replace", side_effect=OSError("simulated non-permission filesystem failure")) as replace, patch("evaluation.control.time.sleep") as pause:
            with self.assertRaises(OSError):
                analyze_feedback("Offline feedback", client=client, max_retries=0,
                                 request_control=self.control.trace("blocked-run", "sample", "v1"))
        replace.assert_called_once()
        pause.assert_not_called()
        client.responses.parse.assert_not_called()
        self.assertEqual(self.budget.snapshot()["attempts_used"], 0)

    def test_responses_sdk_validation_failure_retains_usage_from_same_request(self):
        calls = []

        def transport(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={
                "id": "resp-offline", "object": "response", "created_at": 1,
                "model": "offline-model", "status": "completed", "output": [{
                    "id": "msg-offline", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": '{"sentiment":"neutral"}', "annotations": []}]}],
                "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 3}},
            })

        client = OpenAI(api_key="offline-only", base_url="https://offline.invalid/v1", max_retries=7,
                        http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(client.close)
        report = self.evaluate(client, limit=1)
        self.assertEqual(calls, ["/v1/responses"])
        row = report["predictions"][0]
        self.assertEqual(row["status"], "validation_error")
        self.assertEqual(row["request_attempts"], 1)
        self.assertEqual(row["token_usage"], [{"input_tokens": 12, "output_tokens": 7, "total_tokens": 19,
                         "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 3}}])
        self.assertEqual(load_report(save_report(report, self.path / "reports")["report"]), report)

    def test_interrupt_after_submit_keeps_the_unreturned_future_evidence(self):
        original_submit = ThreadPoolExecutor.submit
        interrupts = [True]

        def submit(executor, *args, **kwargs):
            future = original_submit(executor, *args, **kwargs)
            if interrupts:
                interrupts.pop()
                raise KeyboardInterrupt
            return future

        with patch.object(ThreadPoolExecutor, "submit", submit):
            report = self.evaluate(fake_client())
        self.assertEqual(report["metadata"]["run_status"], "incomplete")
        self.assertEqual(report["metadata"]["stop_reason"], "interrupted")
        self.assertEqual(report["metadata"]["request_attempts_actual"], sum(row["request_attempts"] for row in report["predictions"]))
        self.assertEqual(len(report["predictions"]), 3)
        self.assertEqual(load_report(save_report(report, self.path / "reports")["report"]), report)

    def test_interrupt_during_result_collection_stops_and_keeps_finished_rows(self):
        original_result = Future.result
        interrupts = [True]

        def result(future, *args, **kwargs):
            value = original_result(future, *args, **kwargs)
            if interrupts:
                interrupts.pop()
                raise KeyboardInterrupt
            return value

        with patch.object(Future, "result", result):
            report = self.evaluate(fake_client())
        self.assertEqual(report["metadata"]["stop_reason"], "interrupted")
        self.assertEqual([row["status"] for row in report["predictions"]], ["success", "not_executed", "not_executed"])
        self.assertEqual(report["metadata"]["request_attempts_actual"], 1)

    def test_interrupt_after_journal_write_does_not_duplicate_row_and_finishes(self):
        original_append = RunJournal.append
        interrupts = [True]

        def append(journal, row):
            original_append(journal, row)
            if interrupts:
                interrupts.pop()
                raise KeyboardInterrupt

        with patch.object(RunJournal, "append", append):
            report = self.evaluate(fake_client())
        events = [json.loads(line) for line in Path(report["metadata"]["journal_path"]).read_text(encoding="utf-8").splitlines()]
        ids = [event["prediction"]["sample_id"] for event in events if event["event"] == "prediction"]
        self.assertEqual(ids, report["metadata"]["sample_ids"])
        self.assertEqual(events[-1]["event"], "finished")
        self.assertEqual(events[-1]["metadata"]["stop_reason"], "interrupted")


if __name__ == "__main__":
    unittest.main()
