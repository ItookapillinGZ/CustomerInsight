"""Human review state and source history checks; confirmations here are test fixtures."""

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation.data import DATASET_FIELDS, dataset_hash, load_dataset
from evaluation.review import (
    ReviewValidationError, freeze_reviewed_dataset, initialize_review,
    load_review_ledger, new_review_ledger, review_checklist_csv,
    save_review, validate_review_evidence, validate_review_ledger,
)
from evaluation.reports import load_report, save_report
from evaluation.runner import compare_reports, run_evaluation
from tests.helpers import offline_temp_root, write_draft_dataset


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=offline_temp_root(), prefix=".review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "feedback_eval.csv"
        write_draft_dataset(self.source)
        self.target = self.root / "history.json"
        self.original = self.source.read_bytes()
        self.initial_hash = dataset_hash(load_dataset(self.source))
        self.labels = load_review_ledger(self.source, self.target)["records"][0]["current_labels"]

    def save(self, *, updates=None, **kwargs):
        return save_review(self.source, self.target, updates=updates or {"dev-001": self.labels},
                           reviewer="test human fixture", reasons="依据标注指南进行测试", **kwargs)

    def test_initial_checklist_is_all_draft_and_reading_does_not_write(self):
        ledger = load_review_ledger(self.source, self.target)
        self.assertEqual(len(ledger["records"]), 60)
        self.assertEqual({record["review_status"] for record in ledger["records"]}, {"draft"})
        self.assertTrue(all(not record["history"] for record in ledger["records"]))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        checklist = list(csv.DictReader(io.StringIO(review_checklist_csv(ledger).decode("utf-8-sig"))))
        self.assertEqual(len(checklist), 60)
        self.assertIn("initial_gold_category", checklist[0])
        self.assertNotIn("prediction", " ".join(checklist[0]))

    def test_checklist_escapes_spreadsheet_formulas_without_altering_evidence(self):
        ledger = load_review_ledger(self.source, self.target)
        ledger["records"][0]["text"] = "=SUM(1,2)"
        checklist = list(csv.DictReader(io.StringIO(review_checklist_csv(ledger).decode("utf-8-sig"))))
        self.assertEqual(checklist[0]["text"], "'=SUM(1,2)")
        self.assertEqual(ledger["records"][0]["text"], "=SUM(1,2)")

    def test_initialize_preserves_source_bytes_and_creates_original_backup(self):
        initialize_review(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertTrue(self.target.is_file())
        self.assertEqual(len(list((self.root / "backups").glob("*.original.*.csv"))), 1)
        initialize_review(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_draft_edit_saves_progress_initial_labels_and_history_without_reviewing(self):
        changed = {**self.labels, "gold_priority": "high" if self.labels["gold_priority"] != "high" else "medium"}
        result = self.save(updates={"dev-001": changed})
        record = result["ledger"]["records"][0]
        self.assertEqual(record["initial_labels"], self.labels)
        self.assertEqual(record["current_labels"], changed)
        self.assertEqual(record["review_status"], "draft")
        self.assertIsNone(record["reviewer"])
        event = record["history"][-1]
        self.assertEqual(event["action"], "saved_draft")
        self.assertFalse(event["explicit_confirmation"])
        self.assertEqual(event["before_labels"], self.labels)
        self.assertEqual(event["after_labels"], changed)
        self.assertEqual(load_review_ledger(self.source, self.target), result["ledger"])
        self.assertNotEqual(result["dataset_hash"], self.initial_hash)
        with self.source.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(tuple(next(csv.reader(handle))), DATASET_FIELDS)

    def test_confirming_unchanged_labels_requires_explicit_ids_and_confirmation(self):
        for kwargs in ({"confirm_ids": ["dev-001"]}, {"human_confirmed": True},
                       {"confirm_ids": ["dev-002"], "human_confirmed": True},
                       {"confirm_ids": ["dev-001", "dev-001"], "human_confirmed": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ReviewValidationError):
                self.save(**kwargs)
        result = self.save(confirm_ids=["dev-001"], human_confirmed=True)
        record = result["ledger"]["records"][0]
        self.assertEqual(record["review_status"], "reviewed")
        self.assertEqual(record["current_labels"], self.labels)
        self.assertEqual(record["reviewer"], "test human fixture")
        self.assertTrue(record["reviewed_at_utc"].endswith("Z"))
        self.assertTrue(record["history"][-1]["explicit_confirmation"])
        self.assertEqual(result["confirmed_ids"], ["dev-001"])
        self.assertEqual(load_dataset(self.source)[0].review_status, "reviewed")

    def test_saved_draft_after_confirmation_reopens_record(self):
        self.save(confirm_ids=["dev-001"], human_confirmed=True)
        result = self.save()
        record = result["ledger"]["records"][0]
        self.assertEqual(record["review_status"], "draft")
        self.assertEqual(len(record["history"]), 2)
        self.assertEqual(record["history"][-1]["before_status"], "reviewed")
        self.assertIsNone(record["reviewed_at_utc"])

    def test_batch_confirmation_changes_only_named_records(self):
        ledger = load_review_ledger(self.source, self.target)
        named = ["dev-001", "test-001"]
        updates = {record["sample_id"]: record["current_labels"] for record in ledger["records"] if record["sample_id"] in named}
        result = self.save(updates=updates, confirm_ids=named, human_confirmed=True)
        reviewed = [record["sample_id"] for record in result["ledger"]["records"] if record["review_status"] == "reviewed"]
        self.assertEqual(reviewed, named)
        self.assertEqual(len(result["ledger"]["records"]), 60)

    def test_invalid_labels_identity_and_unknown_ids_do_not_modify_files(self):
        bad_calls = [
            {"updates": {"dev-001": {**self.labels, "gold_category": "made_up"}}, "reviewer": "fixture", "reasons": "rule"},
            {"updates": {"dev-001": self.labels}, "reviewer": " ", "reasons": "rule"},
            {"updates": {"dev-001": self.labels}, "reviewer": "fixture", "reasons": " "},
            {"updates": {"missing": self.labels}, "reviewer": "fixture", "reasons": "rule"},
        ]
        for kwargs in bad_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(ReviewValidationError):
                save_review(self.source, self.target, **kwargs)
            self.assertEqual(self.source.read_bytes(), self.original)
            self.assertFalse(self.target.exists())

    def test_backups_preserve_exact_pre_save_csv_and_ledger(self):
        first = self.save()
        self.assertEqual(Path(first["backup_paths"][0]).read_bytes(), self.original)
        previous_csv, previous_json = self.source.read_bytes(), self.target.read_bytes()
        second = self.save(confirm_ids=["dev-001"], human_confirmed=True)
        self.assertEqual(Path(second["backup_paths"][0]).read_bytes(), previous_csv)
        self.assertEqual(Path(second["backup_paths"][1]).read_bytes(), previous_json)

    def test_failed_ledger_replace_restores_prior_csv_and_evidence(self):
        self.save()
        previous_csv, previous_json = self.source.read_bytes(), self.target.read_bytes()
        from evaluation.review import _atomic_write
        fail_once = True

        def replace(target, data):
            nonlocal fail_once
            if target == self.target and fail_once:
                fail_once = False
                raise OSError("simulated filesystem failure")
            _atomic_write(target, data)

        with patch("evaluation.review._atomic_write", side_effect=replace), self.assertRaises(OSError):
            self.save(confirm_ids=["dev-001"], human_confirmed=True)
        self.assertEqual(self.source.read_bytes(), previous_csv)
        self.assertEqual(self.target.read_bytes(), previous_json)
        self.assertEqual(load_review_ledger(self.source, self.target)["records"][0]["review_status"], "draft")

    def test_stale_screen_hash_cannot_overwrite_newer_changes(self):
        initial = dataset_hash(load_dataset(self.source))
        self.save(confirm_ids=["dev-001"], human_confirmed=True)
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ReviewValidationError, "changed since"):
            self.save(expected_dataset_hash=initial)
        self.assertEqual(self.source.read_bytes(), before)

    def test_modified_csv_or_initial_labels_or_forged_review_flag_is_rejected(self):
        result = self.save()
        ledger = result["ledger"]
        variants = []
        state = copy.deepcopy(ledger)
        state["records"][0]["review_status"] = "reviewed"
        variants.append(state)
        state = copy.deepcopy(ledger)
        state["records"][0]["initial_labels"]["gold_sentiment"] = "neutral"
        variants.append(state)
        state = copy.deepcopy(ledger)
        state["records"][0]["history"][0]["explicit_confirmation"] = True
        variants.append(state)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ReviewValidationError):
                validate_review_ledger(variant, load_dataset(self.source))
        changed = [sample.model_dump(mode="json") for sample in load_dataset(self.source)]
        changed[0]["review_status"] = "reviewed"
        with self.assertRaises(ReviewValidationError):
            validate_review_ledger(ledger, changed)
        with self.assertRaises(ReviewValidationError):
            new_review_ledger(changed)

    def test_partial_review_and_missing_evidence_cannot_freeze(self):
        with self.assertRaisesRegex(ReviewValidationError, "CSV flags alone"):
            validate_review_evidence(self.source, self.target, require_all_reviewed=True)
        self.save(confirm_ids=["dev-001"], human_confirmed=True)
        with self.assertRaisesRegex(ReviewValidationError, "59 records"):
            freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        self.assertFalse((self.root / "frozen").exists())

    def test_fully_confirmed_fixed_dataset_freezes_hash_and_evidence_once(self):
        ledger = load_review_ledger(self.source, self.target)
        updates = {record["sample_id"]: record["current_labels"] for record in ledger["records"]}
        self.save(updates=updates, confirm_ids=list(updates), human_confirmed=True)
        manifest = freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        self.assertEqual(manifest["reviewed_count"], 60)
        self.assertEqual(manifest["split_counts"], {"dev": 40, "test": 20})
        self.assertEqual(manifest["snapshot_sha256"], hashlib.sha256(Path(manifest["dataset_path"]).read_bytes()).hexdigest())
        self.assertEqual(manifest["dataset_hash"], dataset_hash(load_dataset(manifest["dataset_path"])))
        self.assertEqual(manifest, freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen"))

    def test_tampered_frozen_manifest_is_rejected_on_reuse(self):
        ledger = load_review_ledger(self.source, self.target)
        updates = {record["sample_id"]: record["current_labels"] for record in ledger["records"]}
        self.save(updates=updates, confirm_ids=list(updates), human_confirmed=True)
        manifest = freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        path = Path(manifest["dataset_path"]).parent / "dataset_manifest.json"
        manifest["snapshot_sha256"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ReviewValidationError, "manifest is invalid"):
            freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")

    def test_frozen_experiment_rejects_rule_changes(self):
        ledger = load_review_ledger(self.source, self.target)
        updates = {record["sample_id"]: record["current_labels"] for record in ledger["records"]}
        self.save(updates=updates, confirm_ids=list(updates), human_confirmed=True)
        modified = self.root / "modified_rules.md"
        modified.write_text("modified rules", encoding="utf-8")
        with patch("evaluation.review.ANNOTATION_GUIDE", modified), self.assertRaisesRegex(ReviewValidationError, "rules changed"):
            freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")

    def test_old_report_remains_loadable_but_new_hash_report_is_not_comparable(self):
        old = run_evaluation(load_dataset(self.source), mode="demo", prompt_version="v1")
        paths = save_report(old, output_dir=self.root / "reports")
        self.save(confirm_ids=["dev-001"], human_confirmed=True)
        new = run_evaluation(load_dataset(self.source), mode="demo", prompt_version="v2")
        old_loaded = load_report(paths["report"])
        self.assertEqual(old_loaded["metadata"]["dataset_hash"], old["metadata"]["dataset_hash"])
        with self.assertRaises(ValueError):
            compare_reports([old_loaded, new])


if __name__ == "__main__":
    unittest.main()
