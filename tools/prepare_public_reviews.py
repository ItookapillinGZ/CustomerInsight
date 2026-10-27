"""Reproduce the public review sample without invoking a model.

The input archive is pinned to the UCI-hosted original. Network access is opt-in
with --download; the normal command only reads the bundled archive.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "datasets" / "public_reviews"
ARCHIVE_NAME = "uci_sentiment_labelled_sentences.zip"
DOWNLOAD_URL = (
    "https://archive.ics.uci.edu/static/public/331/"
    "sentiment%2Blabelled%2Bsentences.zip"
)
DATASET_URL = "https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences"
ARCHIVE_SHA256 = "afc26626d710899948693e1a61405dce197f57ffa719fa1130d346b4cc095343"
SOURCE_MEMBERS = {
    "amazon": "sentiment labelled sentences/amazon_cells_labelled.txt",
    "yelp": "sentiment labelled sentences/yelp_labelled.txt",
}
SOURCE_SHA256 = {
    "amazon": "47003fc0a0d4840b00e96e715b6189bad09e7443a3da41c4cbe12ffc79f86ae3",
    "yelp": "c76468b7b5c6e56a0804d728345c5f84aa2142ddb214420f61cc9cfd4c00d2ea",
}
FIELDNAMES = ["source_id", "feedback", "source", "sentiment_label"]
LABELS = {"0": "negative", "1": "positive"}
SEED = 20261015


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_originals(archive_bytes: bytes) -> dict[str, list[dict[str, str]]]:
    """Validate original bytes and retain original one-based line identifiers."""
    if sha256(archive_bytes) != ARCHIVE_SHA256:
        raise ValueError("UCI archive SHA-256 does not match the pinned original.")
    sources: dict[str, list[dict[str, str]]] = {}
    with ZipFile(io.BytesIO(archive_bytes)) as archive:
        for source, member in SOURCE_MEMBERS.items():
            content = archive.read(member)
            if sha256(content) != SOURCE_SHA256[source]:
                raise ValueError(f"Original member SHA-256 mismatch: {source}")
            rows = []
            for line_number, line in enumerate(content.decode("utf-8").splitlines(), 1):
                feedback, raw_label = line.rsplit("\t", 1)
                if not feedback.strip() or raw_label not in LABELS:
                    raise ValueError(f"Invalid original record: {source}:{line_number}")
                rows.append({
                    "source_id": f"uci331-{source}-{line_number:04d}",
                    "feedback": feedback,
                    "source": source,
                    "sentiment_label": LABELS[raw_label],
                })
            if len(rows) != 1000:
                raise ValueError(f"Expected 1000 original {source} rows.")
            if any(sum(row["sentiment_label"] == label for row in rows) != 500
                   for label in LABELS.values()):
                raise ValueError(f"Expected balanced binary labels: {source}")
            sources[source] = rows
    return sources


def select_sample(
    sources: dict[str, list[dict[str, str]]], sample_size: int = 100, seed: int = SEED,
) -> list[dict[str, str]]:
    """Draw equally from each source/label stratum; keep distinct review text."""
    if not 4 <= sample_size <= 100 or sample_size % 4:
        raise ValueError("sample_size must be a multiple of 4 between 4 and 100.")
    generator = random.Random(seed)
    selected: list[dict[str, str]] = []
    seen_text: set[str] = set()
    for source in sorted(SOURCE_MEMBERS):
        for label in ("negative", "positive"):
            candidates = [row for row in sources[source] if row["sentiment_label"] == label]
            generator.shuffle(candidates)
            taken = 0
            for row in candidates:
                key = " ".join(row["feedback"].split()).casefold()
                if key in seen_text:
                    continue
                selected.append(dict(row))
                seen_text.add(key)
                taken += 1
                if taken == sample_size // 4:
                    break
            if taken != sample_size // 4:
                raise ValueError(f"Too few distinct records: {source}/{label}")
    return sorted(selected, key=lambda row: row["source_id"])


def sample_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def prepare(archive_path: Path, output_dir: Path, sample_size: int = 100,
            seed: int = SEED) -> dict:
    archive_bytes = archive_path.read_bytes()
    sources = read_originals(archive_bytes)
    rows = select_sample(sources, sample_size, seed)
    content = sample_csv_bytes(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    with ZipFile(io.BytesIO(archive_bytes)) as archive:
        for member in (*SOURCE_MEMBERS.values(), "sentiment labelled sentences/readme.txt"):
            # Fixed filenames only: never extract untrusted archive paths.
            (raw_dir / member.rsplit("/", 1)[1]).write_bytes(archive.read(member))
    (output_dir / "samples.csv").write_bytes(content)
    metadata = {
        "dataset": "Sentiment Labelled Sentences",
        "dataset_id": "uci-331",
        "dataset_creator": "Dimitrios Kotzias",
        "dataset_year": 2015,
        "dataset_doi": "10.24432/C57604",
        "dataset_url": DATASET_URL,
        "download_url": DOWNLOAD_URL,
        "source_checked_on": "2026-10-15",
        "license": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "license_evidence_url": DATASET_URL,
        "original_archive_sha256": sha256(archive_bytes),
        "original_archive_bytes": len(archive_bytes),
        "original_members": {
            source: {
                "member": member,
                "sha256": SOURCE_SHA256[source],
                "rows": len(sources[source]),
                "positive": 500,
                "negative": 500,
            } for source, member in SOURCE_MEMBERS.items()
        },
        "language": "en",
        "sample_file": "samples.csv",
        "sample_sha256": sha256(content),
        "sample_rows": len(rows),
        "sample_seed": seed,
        "sampling": "Fixed-seed shuffled distinct-text stratified sample; equal source/label counts; sorted source_id output.",
        "stratum_counts": {
            f"{source}/{label}": sum(row["source"] == source and row["sentiment_label"] == label for row in rows)
            for source in SOURCE_MEMBERS for label in ("negative", "positive")
        },
        "source_id_rule": "uci331-{source}-{original one-based line number:04d}",
        "transformations": [
            "Selected Amazon and Yelp domains only; excluded IMDb movie sentences.",
            "Mapped original score 0 to negative and 1 to positive.",
            "Added stable provenance columns; original feedback text is unchanged.",
            "Subsampled distinct texts; encoded CSV as UTF-8 with BOM for spreadsheet compatibility.",
        ],
        "reference_label_origin": "Original UCI binary sentiment annotations; not generated or re-labelled by this project.",
        "not_provided": ["neutral sentiment reference", "category gold labels", "priority gold labels", "aspect gold labels", "rating", "review date"],
        "usage_boundary": "Public real review excerpts for ingestion/integration testing and binary sentiment checks; no target-business representativeness or category/priority accuracy claim.",
        "model_calls_executed": 0,
        "legacy_evaluation_modified": False,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--download", action="store_true", help="Explicitly fetch the pinned public UCI ZIP.")
    args = parser.parse_args()
    archive_path = args.archive or args.output_dir / "raw" / ARCHIVE_NAME
    if args.download:
        request = Request(DOWNLOAD_URL, headers={"User-Agent": "CustomerInsight-public-data-preparation/1.0"})
        with urlopen(request, timeout=30) as response:
            archive_bytes = response.read(1024 * 1024 + 1)
        if sha256(archive_bytes) != ARCHIVE_SHA256:
            raise ValueError("Downloaded UCI archive does not match the pinned SHA-256.")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(archive_bytes)
    metadata = prepare(archive_path, args.output_dir, args.sample_size, args.seed)
    print(f"Prepared {metadata['sample_rows']} public reviews without model calls: {args.output_dir}")
    print(f"Sample SHA-256: {metadata['sample_sha256']}")


if __name__ == "__main__":
    main()
