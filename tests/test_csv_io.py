import unittest
from unittest.mock import patch

import pandas as pd

from core.config import Config
from core.csv_io import (
    CsvInputError,
    export_results_csv,
    prepare_feedback_rows,
    read_feedback_csv,
    results_to_dataframe,
)
from models.enums import ProcessingStatus
from models.models import FeedbackResult


class CsvInputTests(unittest.TestCase):
    def test_utf8_bom_csv_allows_selecting_any_text_column(self):
        frame = read_feedback_csv(
            "customer,comments\nAlice,很好用\nBob,结账失败\n".encode("utf-8-sig"),
            filename="feedback.csv",
        )
        rows = prepare_feedback_rows(frame, "comments")
        self.assertEqual(frame.columns.tolist(), ["customer", "comments"])
        self.assertEqual([row.original_text for row in rows], ["很好用", "结账失败"])
        self.assertEqual([row.source_row for row in rows], [2, 3])
        self.assertEqual(len({row.feedback_id for row in rows}), 2)
        self.assertTrue(all(row.processing_status == ProcessingStatus.PENDING for row in rows))

    def test_quoted_multiline_text_remains_one_traceable_record(self):
        frame = read_feedback_csv(b'review,customer\n"First line\nsecond line",A\nWorks,B\n')
        rows = prepare_feedback_rows(frame, "review")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].original_text, "First line\nsecond line")
        self.assertEqual([row.source_row for row in rows], [2, 3])

    def test_gb18030_and_literal_na_text_are_preserved(self):
        frame = read_feedback_csv(
            "feedback\n结账失败\nNA\nNULL\n".encode("gb18030"),
            filename="feedback.csv", encoding="gb18030",
        )
        rows = prepare_feedback_rows(frame, "feedback")
        self.assertEqual([row.original_text for row in rows], ["结账失败", "NA", "NULL"])
        self.assertTrue(all(row.processing_status == ProcessingStatus.PENDING for row in rows))

    def test_blank_csv_records_are_preserved_as_skipped(self):
        frame = read_feedback_csv(b"review\nWorks\n\nFails\n")
        rows = prepare_feedback_rows(frame, "review")
        self.assertEqual(len(rows), 3)
        self.assertEqual([row.source_row for row in rows], [2, 3, 4])
        self.assertEqual(rows[1].processing_status, ProcessingStatus.SKIPPED_EMPTY)

    def test_duplicate_blank_headers_and_wrong_field_counts_are_rejected(self):
        for data in [
            b"review,review\nA,B\n",
            b"review, review \nA,B\n",
            b"review,\nA,B\n",
            b"review,customer\nA\n",
            b"review,customer\nA,B,C\n",
        ]:
            with self.subTest(data=data):
                with self.assertRaises(CsvInputError):
                    read_feedback_csv(data)

    def test_large_unselected_field_and_large_feedback_do_not_discard_other_rows(self):
        oversized = "x" * 140_000
        frame = read_feedback_csv(f"feedback,metadata\nWorks,{oversized}\n{oversized},A\nFails,B\n".encode())
        rows = prepare_feedback_rows(frame, "feedback")
        self.assertEqual([row.processing_status for row in rows], [ProcessingStatus.PENDING, ProcessingStatus.SKIPPED_INVALID, ProcessingStatus.PENDING])
        self.assertEqual(rows[0].original_text, "Works")
        self.assertEqual(rows[2].original_text, "Fails")

    def test_source_rows_do_not_depend_on_dataframe_index(self):
        frame = pd.DataFrame({"review": ["A", "B", "A"]}, index=[50, 100, 900])
        rows = prepare_feedback_rows(frame, "review")
        self.assertEqual([row.source_row for row in rows], [2, 3, 4])
        self.assertEqual(rows[2].duplicate_of, rows[0].feedback_id)

    def test_empty_duplicate_and_nontext_values_keep_their_rows(self):
        frame = pd.DataFrame(
            {"review": [None, float("nan"), "", " \t ", " Fine ", "Fine", 123]}
        )
        rows = prepare_feedback_rows(frame, "review")
        self.assertEqual(len(rows), len(frame))
        self.assertEqual([row.source_row for row in rows], list(range(2, 9)))
        self.assertEqual(
            [row.processing_status for row in rows],
            [
                ProcessingStatus.SKIPPED_EMPTY,
                ProcessingStatus.SKIPPED_EMPTY,
                ProcessingStatus.SKIPPED_EMPTY,
                ProcessingStatus.SKIPPED_EMPTY,
                ProcessingStatus.PENDING,
                ProcessingStatus.SKIPPED_DUPLICATE,
                ProcessingStatus.SKIPPED_INVALID,
            ],
        )
        self.assertEqual(rows[5].duplicate_of, rows[4].feedback_id)
        self.assertEqual(rows[4].original_text, " Fine ")
        self.assertTrue(rows[6].error_message)

    def test_selected_column_must_exist(self):
        with self.assertRaises(CsvInputError):
            prepare_feedback_rows(pd.DataFrame({"review": ["Works"]}), "missing")

    def test_empty_malformed_wrong_extension_and_bad_encoding_are_rejected(self):
        bad_inputs = [
            (b"", "feedback.csv"),
            (b"review\n", "feedback.csv"),
            (b'review\n"unterminated', "feedback.csv"),
            (b"review\nWorks\n", "feedback.xlsx"),
            (b"review\n\xff\xfe\n", "feedback.csv"),
        ]
        for data, filename in bad_inputs:
            with self.subTest(filename=filename, data=data):
                with self.assertRaises(CsvInputError) as raised:
                    read_feedback_csv(data, filename=filename)
                self.assertTrue(str(raised.exception))

    def test_upload_size_and_row_limits_prevent_unbounded_api_work(self):
        with patch.object(Config, "MAX_CSV_BYTES", 10):
            with self.assertRaises(CsvInputError):
                read_feedback_csv(b"review\nThis file is too large\n")
        with patch.object(Config, "MAX_BATCH_ROWS", 2):
            with self.assertRaises(CsvInputError):
                read_feedback_csv(b"review\nOne\nTwo\nThree\n")
            with self.assertRaises(CsvInputError):
                prepare_feedback_rows(pd.DataFrame({"review": ["One", "Two", "Three"]}), "review")

    def test_oversized_text_is_explicitly_skipped(self):
        with patch.object(Config, "MAX_TEXT_LENGTH", 10):
            rows = prepare_feedback_rows(pd.DataFrame({"review": ["x" * 11, "Works"]}), "review")
        self.assertEqual(rows[0].processing_status, ProcessingStatus.SKIPPED_INVALID)
        self.assertTrue(rows[0].error_message)
        self.assertEqual(rows[1].processing_status, ProcessingStatus.PENDING)

    def test_result_fields_and_statuses_export_consistently(self):
        rows = [
            FeedbackResult(
                feedback_id="row-2",
                source_row=2,
                original_text="Checkout fails",
                sentiment="negative",
                category="bugs",
                priority="high",
                summary="Checkout is broken.",
                processing_status="success",
                latency_ms=12.5,
            ),
            FeedbackResult(
                feedback_id="row-3",
                source_row=3,
                original_text="Another review",
                processing_status="failed",
                error_message="Service unavailable",
            ),
        ]
        frame = results_to_dataframe(rows)
        required = {
            "feedback_id", "source_row", "original_text", "sentiment", "category",
            "priority", "summary", "processing_status", "latency_ms", "error_message",
        }
        self.assertTrue(required.issubset(frame.columns))
        self.assertEqual(frame["processing_status"].tolist(), ["success", "failed"])
        self.assertEqual(frame.loc[0, "sentiment"], "negative")
        self.assertTrue(pd.isna(frame.loc[1, "sentiment"]))
        exported = export_results_csv(frame)
        self.assertIsInstance(exported, bytes)
        reread = read_feedback_csv(exported, filename="results.csv")
        self.assertEqual(reread["feedback_id"].tolist(), ["row-2", "row-3"])
        self.assertEqual(reread["error_message"].iloc[1], "Service unavailable")

    def test_export_escapes_formula_cells_without_mutating_original_text(self):
        texts = ["=1+1", "+SUM(A1)", "-1+1", "@SUM(A1)", " \t=1+1", "Normal text"]
        frame = pd.DataFrame({"original_text": texts})
        exported = export_results_csv(frame)
        self.assertTrue(exported.startswith(b"\xef\xbb\xbf"))
        reread = read_feedback_csv(exported, filename="results.csv")
        self.assertEqual(reread["original_text"].tolist(), ["'" + value for value in texts[:-1]] + [texts[-1]])
        self.assertEqual(frame["original_text"].tolist(), texts)


if __name__ == "__main__":
    unittest.main()
