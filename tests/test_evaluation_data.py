"""Independent evaluation input validation, without SDK requests or CSV rewrites."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import Config
from evaluation.data import (
    DATASET_FIELDS,
    DEFAULT_DATASET,
    DatasetValidationError,
    EvaluationSample,
    dataset_hash,
    load_dataset,
    select_split,
    validate_samples,
)
from models.enums import ActionItemPriorityType, SentimentType, TopicType
from tests.helpers import offline_temp_root, write_draft_dataset


def sample(**changes):
    record = {
        "sample_id": "original-007",
        "text": "操作顺畅，感谢团队。",
        "gold_sentiment": "positive",
        "gold_category": "user_experience",
        "gold_priority": "low",
        "split": "dev",
        "review_status": "draft",
    }
    record.update(changes)
    return record


class EvaluationDataTests(unittest.TestCase):
    def write_dataset(self, directory, records, header=DATASET_FIELDS):
        path = Path(directory) / "samples.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for record in records:
                writer.writerow([record[field] for field in header])
        return path

    def test_isolated_draft_dataset_split_status_and_full_label_coverage(self):
        with tempfile.TemporaryDirectory(dir=offline_temp_root()) as directory:
            records = load_dataset(write_draft_dataset(Path(directory) / "draft.csv"))
        self.assertEqual(len(records), 60)
        self.assertEqual(len(select_split(records, "dev")), 40)
        self.assertEqual(len(select_split(records, "test")), 20)
        self.assertEqual({record.review_status for record in records}, {"draft"})
        self.assertEqual({record.gold_sentiment for record in records}, set(SentimentType))
        self.assertEqual({record.gold_category for record in records}, set(TopicType))
        self.assertEqual({record.gold_priority for record in records}, set(ActionItemPriorityType))
        self.assertEqual([record.sample_id for record in records[:2]], ["dev-001", "dev-002"])
        self.assertEqual([record.sample_id for record in records[-2:]], ["test-019", "test-020"])

    def test_draft_fixture_preserves_source_content_without_rewriting_source(self):
        original = DEFAULT_DATASET.read_bytes()
        source = load_dataset(DEFAULT_DATASET)
        with tempfile.TemporaryDirectory(dir=offline_temp_root()) as directory:
            fixture = load_dataset(write_draft_dataset(Path(directory) / "draft.csv"))
        self.assertEqual(DEFAULT_DATASET.read_bytes(), original)
        self.assertEqual(
            [record.model_dump(exclude={"review_status"}) for record in fixture],
            [record.model_dump(exclude={"review_status"}) for record in source],
        )
        self.assertEqual({record.review_status for record in fixture}, {"draft"})

    def test_draft_fixture_refuses_to_overwrite_source_dataset(self):
        original = DEFAULT_DATASET.read_bytes()
        with self.assertRaises(ValueError):
            write_draft_dataset(DEFAULT_DATASET)
        self.assertEqual(DEFAULT_DATASET.read_bytes(), original)

    def test_ids_and_exact_text_are_preserved_including_literal_na(self):
        source = [sample(text="  NA\n仍是反馈  "), sample(sample_id="42", text="NULL", split="test")]
        with tempfile.TemporaryDirectory(dir=offline_temp_root()) as directory:
            records = load_dataset(self.write_dataset(directory, source))
        self.assertEqual([record.sample_id for record in records], ["original-007", "42"])
        self.assertEqual([record.text for record in records], ["  NA\n仍是反馈  ", "NULL"])

    def test_empty_overlong_illegal_labels_states_and_extra_fields_are_rejected(self):
        invalid = [
            sample(text=""), sample(text=" \t\n　"), sample(sample_id=""),
            sample(sample_id=" padded "), sample(gold_sentiment="mixed"),
            sample(gold_category="unknown"), sample(gold_priority="urgent"),
            sample(split="train"), sample(review_status="auto_reviewed"),
            sample(secret_note="not an allowed field"),
        ]
        for record in invalid:
            with self.subTest(record=record):
                with self.assertRaises(DatasetValidationError):
                    validate_samples([record])
        with patch.object(Config, "MAX_TEXT_LENGTH", 10):
            with self.assertRaises(DatasetValidationError):
                validate_samples([sample(text="x" * 11)])

    def test_duplicate_id_is_never_rewritten_or_silently_skipped(self):
        with self.assertRaisesRegex(DatasetValidationError, "Duplicate sample_id"):
            validate_samples([sample(), sample(text="不同的反馈")])
        with self.assertRaisesRegex(DatasetValidationError, "conflicting annotation"):
            validate_samples([sample(), sample(text="不同的反馈", gold_sentiment="negative")])

    def test_normalized_text_duplicate_within_or_across_splits_is_rejected(self):
        first = sample(text="Ａ　反馈\n文本")
        for split in ["dev", "test"]:
            with self.subTest(split=split):
                with self.assertRaisesRegex(DatasetValidationError, "duplicate text"):
                    validate_samples([first, sample(sample_id="other-id", text="A 反馈 文本", split=split)])

    def test_conflicting_gold_for_normalized_text_is_rejected(self):
        with self.assertRaisesRegex(DatasetValidationError, "conflicting annotation"):
            validate_samples([
                sample(), sample(sample_id="conflict", text=" 操作顺畅，感谢团队。 ", gold_priority="high"),
            ])

    def test_model_construct_cannot_bypass_validation(self):
        unvalidated = EvaluationSample.model_construct(**sample(gold_sentiment="fabricated"))
        with self.assertRaises(DatasetValidationError):
            validate_samples([unvalidated])
        invalid_text = EvaluationSample.model_construct(**sample(text=" "))
        with self.assertRaises(DatasetValidationError):
            validate_samples([invalid_text])

    def test_hash_is_canonical_and_tracks_annotations_and_review(self):
        records = validate_samples([sample(), sample(sample_id="second", text="其他反馈")])
        baseline = dataset_hash(records)
        self.assertEqual(len(baseline), 64)
        self.assertEqual(baseline, dataset_hash(reversed(records)))
        for field, value in [
            ("sample_id", "new-id"), ("text", "重新编写的反馈"),
            ("gold_sentiment", "neutral"), ("gold_category", "other"),
            ("gold_priority", "medium"), ("split", "test"), ("review_status", "reviewed"),
        ]:
            with self.subTest(field=field):
                changed = [records[0].model_dump(), records[1].model_dump()]
                changed[0][field] = value
                self.assertNotEqual(baseline, dataset_hash(changed))

    def test_select_split_keeps_ids_and_rejects_unknown_split(self):
        records = validate_samples([sample(), sample(sample_id="other", text="另一条", split="test")])
        self.assertEqual([record.sample_id for record in select_split(records, "dev")], ["original-007"])
        self.assertEqual([record.sample_id for record in select_split(records, "test")], ["other"])
        with self.assertRaises(DatasetValidationError):
            select_split(records, "train")

    def test_empty_datasets_and_bad_csv_structure_reject_the_whole_file(self):
        with self.assertRaises(DatasetValidationError):
            validate_samples([])
        inputs = [
            "", ",".join(DATASET_FIELDS) + "\n", "sample_id,text\nid,hello\n",
            ",".join(DATASET_FIELDS) + "\nmissing,too,few\n",
            ",".join(DATASET_FIELDS) + '\n"unterminated',
            "sample_id,text,gold_sentiment,gold_category,gold_priority,split,split\n",
            ",".join(DATASET_FIELDS) + "\n\n",
        ]
        with tempfile.TemporaryDirectory(dir=offline_temp_root()) as directory:
            path = Path(directory) / "invalid.csv"
            for content in inputs:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(DatasetValidationError):
                        load_dataset(path)
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(DatasetValidationError):
                load_dataset(path)
            with self.assertRaises(DatasetValidationError):
                load_dataset(Path(directory) / "absent.csv")

    def test_record_error_message_does_not_echo_feedback(self):
        private_text = "local private text never put in errors"
        with self.assertRaises(DatasetValidationError) as raised:
            validate_samples([sample(text=private_text, gold_priority="invalid")])
        self.assertNotIn(private_text, str(raised.exception))
        self.assertIn("gold_priority", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
