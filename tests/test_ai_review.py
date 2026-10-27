"""AI review provenance and transaction tests using isolated draft fixtures."""

import copy
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from evaluation.data import DATASET_FIELDS, DEFAULT_DATASET, dataset_hash, load_dataset
from evaluation.review import (
    AI_REVIEWER, ReviewEvent, ReviewValidationError, freeze_reviewed_dataset,
    initialize_review, load_review_ledger, save_ai_review, save_review,
    validate_review_evidence, validate_review_ledger,
)


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix=".ai-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "data.csv", self.root / "review.json"
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=DATASET_FIELDS)
        writer.writeheader()
        writer.writerows({**sample.model_dump(mode="json"), "review_status": "draft"}
                         for sample in load_dataset(DEFAULT_DATASET))
        self.source.write_bytes(output.getvalue().encode("utf-8-sig"))
        self.original = self.source.read_bytes()
        self.initial = initialize_review(self.source, self.target)
        self.updates = {record["sample_id"]: record["current_labels"] for record in self.initial["records"]}

    def ai_save(self, ids=("dev-006",), **kwargs):
        updates = {sample_id: self.updates[sample_id] for sample_id in ids}
        return save_ai_review(self.source, self.target, updates=updates,
                              reasons={sample_id: f"测试逐条复审依据：{sample_id} 原文符合三个标签的标注规则。"
                                       for sample_id in ids}, **kwargs)

    def human_save(self, ids):
        return save_review(self.source, self.target,
                           updates={sample_id: self.updates[sample_id] for sample_id in ids},
                           reviewer="test human fixture", reasons="人类确认测试夹具",
                           confirm_ids=list(ids), human_confirmed=True)

    def test_ai_review_has_separate_status_identity_and_no_human_confirmation(self):
        result = self.ai_save()
        record = next(row for row in result["ledger"]["records"] if row["sample_id"] == "dev-006")
        event = record["history"][-1]
        self.assertEqual(record["review_status"], "ai_reviewed")
        self.assertEqual(record["reviewer"], AI_REVIEWER)
        self.assertEqual(record["initial_labels"], self.updates["dev-006"])
        self.assertEqual(event["action"], "ai_review")
        self.assertFalse(event["explicit_confirmation"])
        self.assertEqual(event["before_status"], "draft")
        self.assertEqual(event["after_status"], "ai_reviewed")
        self.assertEqual(result["confirmed_ids"], [])
        self.assertEqual(result["ai_reviewed_ids"], ["dev-006"])
        self.assertEqual(load_review_ledger(self.source, self.target), result["ledger"])
        self.assertEqual(load_dataset(self.source)[5].review_status, "ai_reviewed")
        with self.source.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(tuple(next(csv.reader(handle))), DATASET_FIELDS)

    def test_ai_label_change_preserves_initial_labels_and_guide_hash(self):
        changed = {**self.updates["dev-006"], "gold_priority": "low"}
        before = self.target.read_bytes()
        result = save_ai_review(self.source, self.target, updates={"dev-006": changed},
                                reasons={"dev-006": "测试：独立审阅判定低优先级，保留初始值和修改依据。"})
        record = result["ledger"]["records"][5]
        self.assertEqual(record["initial_labels"], self.updates["dev-006"])
        self.assertEqual(record["current_labels"], changed)
        self.assertEqual(result["ledger"]["original_records"], self.initial["original_records"])
        self.assertEqual(result["ledger"]["annotation_guide_sha256"], self.initial["annotation_guide_sha256"])
        self.assertEqual(Path(result["backup_paths"][0]).read_bytes(), self.original)
        self.assertEqual(Path(result["backup_paths"][1]).read_bytes(), before)

    def test_ai_review_cannot_touch_human_records_even_in_a_mixed_batch(self):
        self.human_save(("dev-001",))
        before = self.source.read_bytes(), self.target.read_bytes()
        with self.assertRaisesRegex(ReviewValidationError, "cannot overwrite"):
            self.ai_save(("dev-006", "dev-001"))
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)
        self.assertEqual(load_review_ledger(self.source, self.target)["records"][5]["review_status"], "draft")

    def test_ai_review_requires_nonempty_reason_for_every_concrete_id(self):
        before = self.source.read_bytes(), self.target.read_bytes()
        for updates, reasons in (
            ({}, {}), ({"dev-006": self.updates["dev-006"]}, {}),
            ({"dev-006": self.updates["dev-006"]}, {"dev-006": " "}),
            ({"dev-006": self.updates["dev-006"]}, {"dev-006": "reason", "dev-007": "extra"}),
            ({"missing": self.updates["dev-006"]}, {"missing": "reason"}),
            ({"dev-006": {**self.updates["dev-006"], "gold_category": "fake"}}, {"dev-006": "reason"}),
        ):
            with self.subTest(reasons=reasons), self.assertRaises(ReviewValidationError):
                save_ai_review(self.source, self.target, updates=updates, reasons=reasons)
            self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)

    def test_ai_event_cannot_impersonate_human_or_overwrite_reviewed_history(self):
        result = self.ai_save()
        event = result["ledger"]["records"][5]["history"][-1]
        for change in ({"explicit_confirmation": True}, {"after_status": "reviewed"},
                       {"before_status": "reviewed"}, {"reviewer": "human name"},
                       {"action": "human_confirmation"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ReviewEvent.model_validate({**event, **change})

    def test_forged_ai_csv_flag_without_matching_event_is_rejected(self):
        samples = [sample.model_dump(mode="json") for sample in load_dataset(self.source)]
        samples[5]["review_status"] = "ai_reviewed"
        forged = copy.deepcopy(self.initial)
        forged["current_dataset_hash"] = dataset_hash(samples)
        forged["records"][5]["review_status"] = "ai_reviewed"
        forged["records"][5]["reviewer"] = AI_REVIEWER
        with self.assertRaises(ReviewValidationError):
            validate_review_ledger(forged, samples)

    def test_stale_ai_review_and_lock_conflict_do_not_modify_saved_state(self):
        old_hash = self.initial["current_dataset_hash"]
        self.ai_save()
        before = self.source.read_bytes(), self.target.read_bytes()
        with self.assertRaisesRegex(ReviewValidationError, "changed since"):
            self.ai_save(("dev-007",), expected_dataset_hash=old_hash)
        lock = self.target.with_suffix(self.target.suffix + ".lock")
        lock.write_text("existing operation", encoding="utf-8")
        try:
            with self.assertRaisesRegex(ReviewValidationError, "in progress"):
                self.ai_save(("dev-007",))
        finally:
            lock.unlink()
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)

    def test_failed_ai_ledger_write_rolls_back_csv_and_history(self):
        from evaluation.review import _atomic_write
        before = self.source.read_bytes(), self.target.read_bytes()
        failed = False

        def replace(target, data):
            nonlocal failed
            if target == self.target and not failed:
                failed = True
                raise OSError("test replacement failure")
            _atomic_write(target, data)

        with patch("evaluation.review._atomic_write", side_effect=replace), self.assertRaises(OSError):
            self.ai_save()
        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), before)
        self.assertEqual(load_review_ledger(self.source, self.target), self.initial)

    def test_human_can_explicitly_confirm_ai_review_and_preserve_both_events(self):
        self.ai_save()
        result = self.human_save(("dev-006",))
        record = result["ledger"]["records"][5]
        self.assertEqual([event["action"] for event in record["history"]], ["ai_review", "human_confirmation"])
        self.assertEqual(record["review_status"], "reviewed")
        self.assertTrue(record["history"][-1]["explicit_confirmation"])
        self.assertEqual(record["history"][-1]["before_status"], "ai_reviewed")

    def test_mixed_five_human_fifty_five_ai_review_freezes_with_true_provenance(self):
        human_ids = tuple(f"dev-{number:03d}" for number in range(1, 6))
        human = self.human_save(human_ids)
        human_records = copy.deepcopy(human["ledger"]["records"][:5])
        self.ai_save(tuple(sample_id for sample_id in self.updates if sample_id not in human_ids))
        ledger = validate_review_evidence(self.source, self.target, require_all_reviewed=True)
        self.assertEqual(ledger["records"][:5], human_records)
        manifest = freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        self.assertEqual(manifest["review_status_counts"], {"draft": 0, "reviewed": 5, "ai_reviewed": 55})
        self.assertEqual(manifest["human_reviewed_count"], 5)
        self.assertEqual(manifest["ai_reviewed_count"], 55)
        self.assertEqual(manifest["reviewed_count"], 60)
        self.assertEqual(manifest["review_provenance"], "mixed_human_ai")
        self.assertIn("5 条人工确认、55 条 AI 复审", manifest["review_note"])
        self.assertEqual(manifest["split_counts"], {"dev": 40, "test": 20})
        self.assertEqual(manifest, freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen"))

    def test_remaining_draft_and_changed_guide_still_block_freeze(self):
        self.ai_save(tuple(self.updates)[:-1])
        with self.assertRaisesRegex(ReviewValidationError, "1 records remain draft"):
            freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        self.ai_save((tuple(self.updates)[-1],))
        changed = self.root / "modified_guide.md"
        changed.write_text("different annotation rules", encoding="utf-8")
        with patch("evaluation.review.ANNOTATION_GUIDE", changed), self.assertRaisesRegex(ReviewValidationError, "rules changed"):
            freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")

    def test_legacy_all_human_manifest_stays_readable_without_rewriting_evidence(self):
        self.human_save(tuple(self.updates))
        manifest = freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        legacy = {key: value for key, value in manifest.items() if key not in (
            "review_status_counts", "human_reviewed_count", "ai_reviewed_count", "review_provenance", "review_note")}
        path = Path(manifest["dataset_path"]).parent / "dataset_manifest.json"
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        enriched = freeze_reviewed_dataset(self.source, self.target, output_dir=self.root / "frozen")
        self.assertEqual(enriched, manifest)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(enriched["review_provenance"], "human")
        self.assertEqual(enriched["human_reviewed_count"], 60)
        self.assertEqual(enriched["ai_reviewed_count"], 0)


if __name__ == "__main__":
    unittest.main()
