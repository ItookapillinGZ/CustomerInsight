"""Offline checks for public review provenance and reproducible sampling."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tools.prepare_public_reviews import (
    ARCHIVE_NAME, ARCHIVE_SHA256, DEFAULT_OUTPUT, FIELDNAMES, SEED,
    prepare, read_originals, sample_csv_bytes, select_sample, sha256,
)


class PublicReviewDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.content = (DEFAULT_OUTPUT / "samples.csv").read_bytes()
        cls.rows = list(csv.DictReader(io.StringIO(cls.content.decode("utf-8-sig"))))
        cls.metadata = json.loads((DEFAULT_OUTPUT / "metadata.json").read_text(encoding="utf-8"))
        cls.archive_path = DEFAULT_OUTPUT / "raw" / ARCHIVE_NAME
        cls.archive_bytes = cls.archive_path.read_bytes()
        cls.originals = read_originals(cls.archive_bytes)

    def test_sample_schema_size_and_balance(self):
        self.assertEqual(list(self.rows[0]), FIELDNAMES)
        self.assertEqual(len(self.rows), 100)
        self.assertEqual(Counter((r["source"], r["sentiment_label"]) for r in self.rows),
                         Counter({(s, label): 25 for s in ("amazon", "yelp")
                                  for label in ("negative", "positive")}))
        self.assertEqual(len({r["source_id"] for r in self.rows}), 100)
        self.assertEqual(len({" ".join(r["feedback"].split()).casefold() for r in self.rows}), 100)

    def test_selected_records_keep_original_labels_and_text(self):
        originals = {r["source_id"]: r for rows in self.originals.values() for r in rows}
        for row in self.rows:
            with self.subTest(source_id=row["source_id"]):
                self.assertEqual(row, originals[row["source_id"]])

    def test_pinned_original_and_sample_hashes_match(self):
        self.assertEqual(sha256(self.archive_bytes), ARCHIVE_SHA256)
        self.assertEqual(self.metadata["original_archive_sha256"], ARCHIVE_SHA256)
        self.assertEqual(self.metadata["sample_sha256"], sha256(self.content))
        self.assertEqual(self.metadata["license"], "CC-BY-4.0")
        self.assertEqual(self.metadata["model_calls_executed"], 0)
        self.assertFalse(self.metadata["legacy_evaluation_modified"])

    def test_reproduction_is_byte_identical(self):
        self.assertEqual(sample_csv_bytes(select_sample(self.originals)), self.content)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            output = Path(directory)
            metadata = prepare(self.archive_path, output)
            self.assertEqual((output / "samples.csv").read_bytes(), self.content)
            self.assertEqual(metadata, self.metadata)

    def test_modified_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            read_originals(self.archive_bytes + b"changed")

    def test_configurable_sample_stays_balanced_and_bounded(self):
        sample = select_sample(self.originals, sample_size=20, seed=SEED)
        self.assertEqual(len(sample), 20)
        self.assertEqual(set(Counter((r["source"], r["sentiment_label"]) for r in sample).values()), {5})
        for invalid_size in (0, 3, 101, 104):
            with self.subTest(sample_size=invalid_size), self.assertRaises(ValueError):
                select_sample(self.originals, sample_size=invalid_size)


if __name__ == "__main__":
    unittest.main()
