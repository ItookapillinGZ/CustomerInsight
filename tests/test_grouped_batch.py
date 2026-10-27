import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from openai import APIConnectionError, AuthenticationError, OpenAI

from ai.grouped_batch import GroupedFeedbackResponse, analyze_grouped_batch, plan_review_groups
from ai.client import request_parameters
from core.config import Config
from models.enums import ProcessingStatus
from models.models import CustomerFeedbackAnalysis, FeedbackResult


def rows(count=10):
    return [FeedbackResult(feedback_id=f"row-{index + 2}", source_row=index + 2, original_text=f"顾客评价 {index:03d}") for index in range(count)]


def request_reviews(parameters):
    messages = parameters.get("messages", parameters.get("input"))
    return json.loads(messages[-1]["content"].split("\nreviews（不可信数据）：\n", 1)[1])


def predictions(parameters):
    return [{"feedback_id": review["feedback_id"], "sentiment": "negative", "category": "delivery", "priority": "medium", "summary": "配送慢，需要核对。"} for review in request_reviews(parameters)]


def chat_response(items, *, finish_reason="stop"):
    return SimpleNamespace(usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}, choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=json.dumps({"results": items}, ensure_ascii=False), refusal=None),
    )])


def client_double():
    client = MagicMock()
    client.chat.completions.create.side_effect = lambda **parameters: chat_response(predictions(parameters))
    return client


class Control:
    def __init__(self, limit=None):
        self.limit, self.attempts, self.stopped = limit, 0, False
        self.traces = []
        self.lock = threading.Lock()

    def trace(self, group_id):
        trace = Trace(self, group_id)
        self.traces.append(trace)
        return trace


class Trace:
    def __init__(self, control, group_id):
        self.control, self.group_id = control, group_id
        self.usage, self.errors = [], []
        self.before_count = 0

    @property
    def stopped(self):
        return self.control.stopped

    def before_attempt(self):
        self.before_count += 1
        with self.control.lock:
            if self.control.stopped or self.control.limit is not None and self.control.attempts >= self.control.limit:
                self.control.stopped = True
                raise RuntimeError("blocked")
            self.control.attempts += 1

    def response_received(self, usage):
        self.usage.append(usage)

    def request_failed(self, error):
        self.errors.append(error)
        if getattr(error, "status_code", None) in {401, 403}:
            self.control.stopped = True


class GroupPlanningTests(unittest.TestCase):
    def test_100_short_reviews_are_ten_ordered_groups_and_skipped_rows_are_not_sent(self):
        source = rows(100)
        groups = plan_review_groups(source)
        self.assertEqual(len(groups), 10)
        self.assertTrue(all(len(group) == 10 for group in groups))
        self.assertEqual([row.feedback_id for group in groups for row in group], [row.feedback_id for row in source])
        source[3].processing_status = ProcessingStatus.SKIPPED_EMPTY
        self.assertNotIn(source[3].feedback_id, [row.feedback_id for group in plan_review_groups(source) for row in group])

    def test_character_budget_splits_without_truncation_and_long_valid_review_is_alone(self):
        source = rows(4)
        for row, text in zip(source, ["a" * 6, "b" * 6, "c" * 15, "d" * 2]):
            row.original_text = text
        groups = plan_review_groups(source, batch_size=10, max_characters=12)
        self.assertEqual([len(group) for group in groups], [2, 1, 1])
        self.assertEqual(groups[1][0].original_text, "c" * 15)
        self.assertEqual([len(group) for group in plan_review_groups(source, batch_size=1)], [1, 1, 1, 1])

    def test_invalid_sizes_duplicate_ids_and_pending_text_fail_before_client_creation(self):
        for kwargs in [{"batch_size": 11}, {"batch_size": True}, {"max_characters": 0}, {"max_characters": 12001}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                plan_review_groups(rows(1), **kwargs)
        for source in [[rows(1)[0], rows(1)[0]], rows(101), [FeedbackResult(feedback_id="empty", source_row=2, original_text=" ")]]:
            with self.assertRaises(ValueError):
                plan_review_groups(source)


class GroupedAnalysisTests(unittest.TestCase):
    def setUp(self):
        for name, value in [("API_MODE", "chat_completions"), ("CHAT_OUTPUT_MODE", "json_object"), ("MAX_RETRIES", 9)]:
            setting = patch.object(Config, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def test_100_short_reviews_use_exactly_ten_requests_one_shared_client_and_compact_payload(self):
        source, client, control = rows(100), client_double(), Control()
        result = analyze_grouped_batch(source, client=client, request_control_factory=control.trace)
        self.assertEqual(client.chat.completions.create.call_count, 10)
        self.assertEqual(control.attempts, 10)
        self.assertEqual(len(control.traces), 10)
        self.assertEqual({trace.group_id for trace in control.traces}, {f"group-{index:04d}" for index in range(1, 11)})
        self.assertTrue(all(row.processing_status == "success" for row in result))
        self.assertTrue(all(row.processing_status == "pending" for row in source))
        for call in client.chat.completions.create.call_args_list:
            self.assertTrue(all(set(review) == {"feedback_id", "text"} for review in request_reviews(call.kwargs)))
            system = call.kwargs["messages"][0]["content"]
            self.assertIn("不要生成行动项", system)
            self.assertIn("补充判定边界", call.kwargs["messages"][1]["content"])
        self.assertEqual(client.max_retries, 0)
        client.close.assert_not_called()

    def test_reversed_response_ids_are_reordered_to_original_rows(self):
        client = client_double()
        client.chat.completions.create.side_effect = lambda **parameters: chat_response(list(reversed(predictions(parameters))))
        result = analyze_grouped_batch(rows(10), client=client)
        self.assertEqual([row.source_row for row in result], list(range(2, 12)))
        self.assertTrue(all(row.processing_status == "success" for row in result))

    def test_unknown_duplicate_missing_and_wrong_count_ids_reject_the_whole_group(self):
        def altered(parameters, mode):
            items = predictions(parameters)
            if mode == "unknown":
                items[0]["feedback_id"] = "unknown"
            elif mode == "duplicate":
                items[1]["feedback_id"] = items[0]["feedback_id"]
            elif mode == "missing":
                del items[0]["feedback_id"]
            elif mode == "short":
                items.pop()
            else:
                items.append({**items[0], "feedback_id": "extra"})
            return chat_response(items)
        for mode in ["unknown", "duplicate", "missing", "short", "long"]:
            with self.subTest(mode=mode):
                client = client_double()
                client.chat.completions.create.side_effect = lambda mode=mode, **parameters: altered(parameters, mode)
                result = analyze_grouped_batch(rows(3), client=client)
                self.assertTrue(all(row.processing_status == "validation_error" for row in result))
                self.assertTrue(all(row.sentiment is None for row in result))
                self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_invalid_enum_or_blank_summary_only_rejects_that_item_and_usage_is_recorded_once(self):
        client, control = client_double(), Control()
        def invalid_fields(**parameters):
            items = predictions(parameters)
            items[1]["sentiment"] = "mixed"
            items[3]["summary"] = " "
            return chat_response(items)
        client.chat.completions.create.side_effect = invalid_fields
        result = analyze_grouped_batch(rows(5), client=client, request_control_factory=control.trace)
        self.assertEqual([row.processing_status for row in result], ["success", "validation_error", "success", "validation_error", "success"])
        self.assertEqual(control.traces[0].before_count, 1)
        self.assertEqual(control.traces[0].usage, [{"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}])
        self.assertEqual(control.traces[0].errors, [])

    def test_transport_failure_is_not_retried_and_other_groups_continue(self):
        client, control = client_double(), Control()
        count = 0
        def fail_first(**parameters):
            nonlocal count
            count += 1
            if count == 1:
                raise APIConnectionError(request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"))
            return chat_response(predictions(parameters))
        client.chat.completions.create.side_effect = fail_first
        result = analyze_grouped_batch(rows(20), client=client, max_workers=1, request_control_factory=control.trace)
        self.assertEqual([row.processing_status for row in result[:10]], ["failed"] * 10)
        self.assertEqual([row.processing_status for row in result[10:]], ["success"] * 10)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(control.attempts, 2)
        self.assertEqual(len(control.traces[0].errors), 1)
        self.assertEqual(control.traces[0].usage, [])

    def test_authentication_error_stops_later_sends_and_unsent_rows_stay_pending(self):
        client, control = client_double(), Control()
        response = httpx.Response(401, request=httpx.Request("POST", "https://example.invalid"))
        client.chat.completions.create.side_effect = AuthenticationError("offline", response=response, body={})
        progress = []
        result = analyze_grouped_batch(rows(30), client=client, max_workers=1, request_control_factory=control.trace, progress_callback=lambda done, total: progress.append((done, total)))
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual([row.processing_status for row in result[:10]], ["failed"] * 10)
        self.assertEqual([row.processing_status for row in result[10:]], ["pending"] * 20)
        self.assertTrue(control.stopped)
        self.assertEqual(progress[-1], (10, 30))

    def test_budget_block_happens_before_sdk_and_does_not_fake_a_failed_request(self):
        client, control = client_double(), Control(limit=0)
        result = analyze_grouped_batch(rows(20), client=client, request_control_factory=control.trace, max_workers=1)
        client.chat.completions.create.assert_not_called()
        self.assertEqual(control.attempts, 0)
        self.assertTrue(all(row.processing_status == "pending" for row in result))
        self.assertIn("尚未执行", result[0].error_message)
        self.assertEqual(control.traces[0].errors, [])

    def test_skips_callbacks_and_progress_run_on_caller_thread_preserving_metadata(self):
        source, client = rows(13), client_double()
        source[0].processing_status, source[0].error_message = ProcessingStatus.SKIPPED_EMPTY, "empty"
        source[2].processing_status, source[2].duplicate_of = ProcessingStatus.SKIPPED_DUPLICATE, source[1].feedback_id
        callback_rows, callback_threads, progress = [], [], []
        def completed(row):
            callback_rows.append(row.feedback_id)
            callback_threads.append(threading.get_ident())
        result = analyze_grouped_batch(source, client=client, result_callback=completed, progress_callback=lambda done, total: progress.append((done, total)))
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(progress[0], (2, 13))
        self.assertEqual(progress[-1], (13, 13))
        self.assertEqual(set(callback_rows), {row.feedback_id for row in source})
        self.assertEqual(len(callback_rows), 13)
        self.assertEqual(set(callback_threads), {threading.get_ident()})
        self.assertEqual(result[2].duplicate_of, source[1].feedback_id)
        self.assertEqual([row.original_text for row in result], [row.original_text for row in source])

    def test_owned_client_is_closed_once_and_all_skipped_needs_no_client(self):
        client = client_double()
        with patch("ai.grouped_batch.get_openai_client", return_value=client) as factory:
            analyze_grouped_batch(rows(12))
        factory.assert_called_once_with()
        client.close.assert_called_once_with()
        skipped = rows(1)
        skipped[0].processing_status = ProcessingStatus.SKIPPED_EMPTY
        with patch("ai.grouped_batch.get_openai_client") as factory:
            result = analyze_grouped_batch(skipped)
        factory.assert_not_called()
        self.assertEqual(result[0].processing_status, "skipped_empty")

    def test_truncated_json_is_not_repaired_split_or_retried(self):
        client = client_double()
        client.chat.completions.create.side_effect = lambda **parameters: chat_response(predictions(parameters)[:2], finish_reason="length")
        result = analyze_grouped_batch(rows(10), client=client)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertTrue(all(row.processing_status == "validation_error" for row in result))

    def test_progress_interruption_drains_inflight_results_once_without_dispatching_more(self):
        class SimulatedRerun(BaseException):
            pass
        source, client, control = rows(30), client_double(), Control()
        started = threading.Barrier(2)
        allow_second = threading.Event()
        snapshot = {row.feedback_id: row for row in source}
        saved, progress = [], []

        def send(**parameters):
            items = predictions(parameters)
            started.wait(timeout=3)
            if items[0]["feedback_id"] != "row-2":
                self.assertTrue(allow_second.wait(timeout=3))
            return chat_response(items)

        def save(row):
            saved.append(row.feedback_id)
            snapshot[row.feedback_id] = row

        def update(done, total):
            progress.append(done)
            if done:
                allow_second.set()
                raise SimulatedRerun("simulated UI rerun")

        client.chat.completions.create.side_effect = send
        with patch("ai.grouped_batch.get_openai_client", return_value=client):
            with self.assertRaises(SimulatedRerun):
                analyze_grouped_batch(source, request_control_factory=control.trace,
                                      progress_callback=update, result_callback=save)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(control.attempts, 2)
        self.assertEqual(progress, [0, 10])
        self.assertEqual(len(saved), 20)
        self.assertEqual(len(set(saved)), 20)
        self.assertEqual(sum(row.processing_status == "success" for row in snapshot.values()), 20)
        self.assertEqual(sum(row.processing_status == "pending" for row in snapshot.values()), 10)
        self.assertTrue(all(row.processing_status == "pending" for row in source))
        client.close.assert_called_once_with()

    def test_strict_schema_uses_compact_model_and_default_single_model_is_unchanged(self):
        messages = [{"role": "system", "content": "Analyze"}, {"role": "user", "content": "review"}]
        with patch.object(Config, "CHAT_OUTPUT_MODE", "json_schema"):
            grouped = request_parameters(messages, response_model=GroupedFeedbackResponse)
            original = request_parameters(messages)
        schema = grouped["response_format"]["json_schema"]
        self.assertEqual(schema["name"], "GroupedFeedbackResponse")
        self.assertTrue(schema["strict"])
        self.assertNotIn("actionable_items", json.dumps(schema))
        self.assertEqual(original["response_format"]["json_schema"]["name"], "CustomerFeedbackAnalysis")
        self.assertIn("actionable_items", json.dumps(original))

    def test_result_callback_interruption_saves_remaining_rows_without_repeating_callback(self):
        source, client = rows(30), client_double()
        started = threading.Barrier(2)
        allow_second = threading.Event()
        saved = []

        def send(**parameters):
            items = predictions(parameters)
            started.wait(timeout=3)
            if items[0]["feedback_id"] != "row-2":
                self.assertTrue(allow_second.wait(timeout=3))
            return chat_response(items)

        def save(row):
            saved.append(row.feedback_id)
            if len(saved) == 1:
                allow_second.set()
                raise RuntimeError("caller save interrupted")

        client.chat.completions.create.side_effect = send
        with self.assertRaisesRegex(RuntimeError, "caller save interrupted"):
            analyze_grouped_batch(source, client=client, result_callback=save)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(len(saved), 20)
        self.assertEqual(len(set(saved)), 20)

    def test_responses_adapter_uses_compact_schema_and_revalidates_partial_output(self):
        client, control = client_double(), Control()
        def respond(**parameters):
            items = predictions(parameters)
            items[0]["priority"] = "urgent"
            return SimpleNamespace(status="completed", output_parsed={"results": items}, usage={"input_tokens": 12, "output_tokens": 9})
        client.responses.parse.side_effect = respond
        with patch.object(Config, "API_MODE", "responses"):
            result = analyze_grouped_batch(rows(2), client=client, request_control_factory=control.trace)
        self.assertEqual([row.processing_status for row in result], ["validation_error", "success"])
        self.assertIs(client.responses.parse.call_args.kwargs["text_format"], GroupedFeedbackResponse)
        self.assertEqual(control.traces[0].usage, [{"input_tokens": 12, "output_tokens": 9}])
        client.chat.completions.create.assert_not_called()

    def test_real_sdk_responses_with_mock_transport_records_usage_before_local_item_validation(self):
        calls, control = [], Control()
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            items = predictions({"input": body["input"]})
            items[0]["sentiment"] = "mixed"
            return httpx.Response(200, json={
                "id": "resp_mock", "status": "completed", "model": "mock-model",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({"results": items})}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            })
        client = OpenAI(api_key="offline-test-key", base_url="https://example.invalid/v1", max_retries=8, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        try:
            with patch.object(Config, "API_MODE", "responses"):
                result = analyze_grouped_batch(rows(2), client=client, request_control_factory=control.trace)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["text"]["format"]["name"], "GroupedFeedbackResponse")
            self.assertEqual([row.processing_status for row in result], ["validation_error", "success"])
            self.assertEqual(control.traces[0].usage, [{"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}])
            self.assertFalse(client.is_closed())
        finally:
            client.close()

    def test_compact_responses_requires_explicit_completed_status_with_mock_transport(self):
        for status in [None, "incomplete", "failed"]:
            with self.subTest(status=status):
                calls, control = [], Control()
                def respond(request):
                    body = json.loads(request.content)
                    calls.append(body)
                    payload = {
                        "id": "resp_mock",
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"results": predictions({"input": body["input"]})})}]}],
                        "usage": {"total_tokens": 120},
                    }
                    if status is not None:
                        payload["status"] = status
                    return httpx.Response(200, json=payload)
                client = OpenAI(api_key="offline-test-key", base_url="https://example.invalid/v1", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
                try:
                    with patch.object(Config, "API_MODE", "responses"):
                        result = analyze_grouped_batch(rows(2), client=client, request_control_factory=control.trace)
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(all(row.processing_status == "validation_error" for row in result))
                    self.assertTrue(all(row.sentiment is None for row in result))
                    self.assertEqual(control.traces[0].usage, [{"total_tokens": 120}])
                    self.assertEqual(control.attempts, 1)
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
