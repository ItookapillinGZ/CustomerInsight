import threading
import unittest
from unittest.mock import patch

import pandas as pd

from ai.batch import analyze_batch
from core.config import Config
from core.csv_io import prepare_feedback_rows, read_feedback_csv, results_to_dataframe
from models.enums import ProcessingStatus
from tests.helpers import fake_client, fake_response, valid_analysis


class BatchAnalysisTests(unittest.TestCase):
    def setUp(self):
        transport = patch.object(Config, "API_MODE", "responses")
        transport.start()
        self.addCleanup(transport.stop)

    def make_rows(self, texts):
        return prepare_feedback_rows(pd.DataFrame({"review": texts}), "review")

    def test_batch_keeps_order_source_rows_and_input_metadata(self):
        rows = self.make_rows(["First review", "Second review", "Third review"])
        client = fake_client()
        results = analyze_batch(rows, client=client, max_workers=2)
        self.assertEqual([row.feedback_id for row in results], [row.feedback_id for row in rows])
        self.assertEqual([row.source_row for row in results], [2, 3, 4])
        self.assertEqual([row.original_text for row in results], ["First review", "Second review", "Third review"])
        self.assertTrue(all(row.processing_status == ProcessingStatus.SUCCESS for row in results))
        self.assertTrue(all(row.latency_ms >= 0 for row in results))
        self.assertTrue(all(row.sentiment == "negative" for row in results))
        self.assertTrue(all(row.error_message is None for row in results))
        self.assertEqual(client.responses.parse.call_count, 3)
        self.assertTrue(all(row.processing_status == ProcessingStatus.PENDING for row in rows))
        client.close.assert_not_called()

    def test_sdk_failure_and_validation_failure_do_not_interrupt_other_rows(self):
        client = fake_client()

        def reply(**kwargs):
            prompt = kwargs["input"][-1]["content"]
            if "SERVICE_FAILURE" in prompt:
                raise RuntimeError("private-feedback-and-key-must-not-be-shown")
            if "INVALID_LABEL" in prompt:
                return fake_response(valid_analysis(sentiment="invented"))
            return fake_response()

        client.responses.parse.side_effect = reply
        rows = self.make_rows(["Good review", "SERVICE_FAILURE", "INVALID_LABEL", "Another good review"])
        results = analyze_batch(rows, client=client, max_workers=2)
        self.assertEqual(
            [row.processing_status for row in results],
            [ProcessingStatus.SUCCESS, ProcessingStatus.FAILED, ProcessingStatus.VALIDATION_ERROR, ProcessingStatus.SUCCESS],
        )
        self.assertEqual(client.responses.parse.call_count, 4)
        for row in results[1:3]:
            self.assertTrue(row.error_message)
            self.assertIsNone(row.sentiment)
            self.assertIsNone(row.category)
            self.assertIsNone(row.priority)
            self.assertIsNone(row.summary)
            self.assertNotIn("private-feedback-and-key", row.error_message)

    def test_empty_and_duplicate_rows_do_not_use_api_or_change_identity(self):
        client = fake_client()
        rows = self.make_rows(["Checkout fails", "", " Checkout fails ", "Working well"])
        results = analyze_batch(rows, client=client)
        self.assertEqual(client.responses.parse.call_count, 2)
        self.assertEqual(results[1].processing_status, ProcessingStatus.SKIPPED_EMPTY)
        self.assertEqual(results[2].processing_status, ProcessingStatus.SKIPPED_DUPLICATE)
        self.assertEqual(results[2].duplicate_of, results[0].feedback_id)
        self.assertEqual([row.source_row for row in results], [2, 3, 4, 5])

    def test_all_skipped_batch_runs_without_key_or_client(self):
        rows = self.make_rows([None, " ", 123])
        with patch.object(Config, "OPENAI_API_KEY", None), patch("ai.batch.get_openai_client") as factory:
            results = analyze_batch(rows)
        factory.assert_not_called()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(row.processing_status != ProcessingStatus.PENDING for row in results))

    def test_one_owned_client_is_shared_and_closed(self):
        client = fake_client()
        rows = self.make_rows(["Review A", "Review B", "Review C"])
        with patch("ai.batch.get_openai_client", return_value=client) as factory:
            results = analyze_batch(rows, max_workers=2)
        factory.assert_called_once_with()
        client.close.assert_called_once_with()
        self.assertEqual(client.responses.parse.call_count, len(rows))
        self.assertTrue(all(row.processing_status == ProcessingStatus.SUCCESS for row in results))

    def test_limited_workers_bound_simultaneous_sdk_requests(self):
        lock = threading.Lock()
        second_request_started = threading.Event()
        active = 0
        peak = 0
        client = fake_client()

        def reply(**kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    second_request_started.set()
            # Both initial workers must enter before either can finish. The
            # timeout makes a broken implementation fail without hanging tests.
            reached_two = second_request_started.wait(timeout=2)
            with lock:
                active -= 1
            if not reached_two:
                raise RuntimeError("The expected second worker never started")
            return fake_response()

        client.responses.parse.side_effect = reply
        results = analyze_batch(self.make_rows([f"Review {index}" for index in range(8)]), client=client, max_workers=2)
        self.assertEqual(peak, 2)
        self.assertTrue(all(row.processing_status == ProcessingStatus.SUCCESS for row in results))

    def test_progress_finishes_on_calling_thread_even_with_skipped_or_failed_rows(self):
        client = fake_client()
        client.responses.parse.side_effect = [fake_response(), RuntimeError("Unavailable")]
        rows = self.make_rows(["Review A", "", "Review B"])
        progress = []
        caller_thread = threading.get_ident()

        def callback(completed, total):
            progress.append((completed, total, threading.get_ident()))

        analyze_batch(rows, client=client, max_workers=1, progress_callback=callback)
        self.assertTrue(progress)
        self.assertEqual(progress[-1][:2], (3, 3))
        self.assertEqual([item[0] for item in progress], sorted(item[0] for item in progress))
        self.assertTrue(all(item[1] == 3 and item[2] == caller_thread for item in progress))

    def test_invalid_worker_counts_and_oversized_batch_fail_before_sdk_calls(self):
        client = fake_client()
        rows = self.make_rows(["Review A"])
        for workers in [0, -1, Config.MAX_WORKERS + 1, 1.5, True]:
            with self.subTest(workers=workers):
                with self.assertRaises(ValueError):
                    analyze_batch(rows, client=client, max_workers=workers)
        with patch.object(Config, "MAX_BATCH_ROWS", 0):
            with self.assertRaises(ValueError):
                analyze_batch(rows, client=client)
        client.responses.parse.assert_not_called()

    def test_csv_to_batch_to_dataframe_works_end_to_end_without_key(self):
        frame = read_feedback_csv(b"customer,review\nA,Checkout fails\nB,\nC,Checkout fails\nD,Works well\n")
        rows = prepare_feedback_rows(frame, "review")
        client = fake_client()
        with patch.object(Config, "OPENAI_API_KEY", None):
            results = analyze_batch(rows, client=client, max_workers=1)
        output = results_to_dataframe(results)
        self.assertEqual(output["processing_status"].tolist(), ["success", "skipped_empty", "skipped_duplicate", "success"])
        self.assertEqual(output["source_row"].tolist(), [2, 3, 4, 5])
        self.assertEqual(output["priority"].dropna().tolist(), ["high", "high"])
        self.assertEqual(client.responses.parse.call_count, 2)


if __name__ == "__main__":
    unittest.main()
