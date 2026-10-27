"""Mixed review provenance remains bound to frozen reference labels."""

import copy
from pathlib import Path
import tempfile
import unittest

from evaluation.data import load_dataset
from evaluation.experiment import _complete_dataset, _review_provenance, _validate_snapshot
from evaluation.review import freeze_reviewed_dataset, save_ai_review, save_review
from tests.helpers import write_draft_dataset


class MixedReviewExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dataset, self.ledger = self.root / "data.csv", self.root / "review.json"
        write_draft_dataset(self.dataset)
        rows = load_dataset(self.dataset)
        labels = {row.sample_id: {key: value for key, value in row.model_dump(mode="json").items() if key.startswith("gold_")} for row in rows}
        first = {sid: value for sid, value in labels.items() if sid in {f"dev-{i:03d}" for i in range(1, 6)}}
        save_review(self.dataset, self.ledger, updates=first, reviewer="isolated human fixture", reasons="test evidence only",
                    confirm_ids=list(first), human_confirmed=True)
        remaining = {sid: value for sid, value in labels.items() if sid not in first}
        save_ai_review(self.dataset, self.ledger, updates=remaining, reasons={sid: "AI fixture rationale" for sid in remaining})
        self.samples = load_dataset(self.dataset)
        self.snapshot = freeze_reviewed_dataset(self.dataset, self.ledger, output_dir=self.root / "frozen")

    def test_complete_mixed_dataset_and_snapshot_retain_actual_review_source(self):
        self.assertEqual(len(_complete_dataset(self.samples)), 60)
        self.assertEqual(_validate_snapshot(self.snapshot, self.samples), self.snapshot)
        self.assertEqual(_review_provenance(self.samples)["review_status_counts"], {"draft": 0, "reviewed": 5, "ai_reviewed": 55})

    def test_snapshot_cannot_claim_ai_references_are_all_human(self):
        forged = copy.deepcopy(self.snapshot)
        forged.update(human_reviewed_count=60, ai_reviewed_count=0, review_provenance="human")
        with self.assertRaisesRegex(ValueError, "人工与 AI 复核来源"):
            _validate_snapshot(forged, self.samples)


if __name__ == "__main__":
    unittest.main()
