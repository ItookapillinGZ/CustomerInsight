"""Local review evidence; AI review never constitutes human confirmation.

The evaluation CSV retains its seven-column format. Review history, immutable
initial labels, AI review, and human explicit confirmation live in separate JSON.
This is a local evidence trail, not a signed identity or independent audit.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.csv_io import _escape_csv_cell
from evaluation.data import (
    COMPLETED_REVIEW_STATUSES, DATASET_FIELDS, DEFAULT_DATASET, EvaluationSample,
    ReviewStatus, dataset_hash,
    load_dataset, validate_samples,
)
from models.enums import ActionItemPriorityType, SentimentType, TopicType


DEFAULT_REVIEW_PATH = Path(__file__).parent / "reviews" / "feedback_eval.review.json"
ANNOTATION_GUIDE = Path(__file__).parent / "ANNOTATION_GUIDE.md"
LABEL_FIELDS = ("gold_sentiment", "gold_category", "gold_priority")
AI_REVIEWER = "Codex（AI）"


class ReviewValidationError(ValueError):
    """Review data is invalid or does not match the current evaluation CSV."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


class ReviewLabels(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    gold_sentiment: SentimentType
    gold_category: TopicType
    gold_priority: ActionItemPriorityType


class ReviewEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    action: Literal["saved_draft", "human_confirmation", "ai_review"]
    reviewer: str
    timestamp_utc: str
    before_labels: ReviewLabels
    after_labels: ReviewLabels
    before_status: ReviewStatus
    after_status: ReviewStatus
    reason: str
    explicit_confirmation: bool = Field(strict=True)

    @field_validator("reviewer", "reason")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewer and reason must contain text")
        return value.strip()

    @field_validator("timestamp_utc")
    @classmethod
    def valid_time(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("timestamp must include the UTC timezone")
        return value

    @model_validator(mode="after")
    def confirmation_matches_action(self):
        confirmed = self.action == "human_confirmation"
        if self.explicit_confirmation is not confirmed:
            raise ValueError("explicit confirmation must match the action")
        expected_status = {"human_confirmation": "reviewed", "saved_draft": "draft", "ai_review": "ai_reviewed"}[self.action]
        if self.after_status != expected_status:
            raise ValueError("review status must match the recorded review action")
        if self.action == "ai_review":
            if self.reviewer != AI_REVIEWER:
                raise ValueError("AI review must identify Codex (AI) as the reviewer")
            if self.before_status == "reviewed":
                raise ValueError("AI review cannot overwrite human-reviewed labels")
        return self


class ReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    text: str
    split: Literal["dev", "test"]
    initial_labels: ReviewLabels
    current_labels: ReviewLabels
    review_status: ReviewStatus
    reviewer: str | None = None
    reviewed_at_utc: str | None = None
    history: list[ReviewEvent]


class ReviewLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    data_origin: Literal["synthetic_examples"]
    created_at_utc: str
    updated_at_utc: str
    annotation_guide_sha256: str
    initial_dataset_hash: str
    current_dataset_hash: str
    original_records: list[EvaluationSample]
    records: list[ReviewRecord]


def review_path_for(dataset_path: str | Path, review_path: str | Path | None = None) -> Path:
    if review_path is not None:
        return Path(review_path)
    source = Path(dataset_path)
    if source.resolve() == DEFAULT_DATASET.resolve():
        return DEFAULT_REVIEW_PATH
    return source.with_suffix(".review.json")


def _labels(sample: EvaluationSample) -> dict:
    record = sample.model_dump(mode="json")
    return {field: record[field] for field in LABEL_FIELDS}


def _parse_labels(value: Mapping[str, str] | ReviewLabels) -> ReviewLabels:
    # Short names are accepted for callers; ledger/CSV always use gold_* names.
    if isinstance(value, Mapping) and set(value) == {"sentiment", "category", "priority"}:
        value = {f"gold_{key}": label for key, label in value.items()}
    try:
        return ReviewLabels.model_validate(value)
    except ValidationError as error:
        raise ReviewValidationError("Review labels must use the existing fixed label sets.") from error


def new_review_ledger(samples: list[EvaluationSample]) -> dict:
    """Build an in-memory checklist; this does not save or confirm any record."""
    validated = validate_samples(samples)
    if any(sample.review_status != "draft" for sample in validated):
        raise ReviewValidationError("A reviewed CSV without its review history cannot initialize evidence.")
    now = _utc_now()
    return {
        "schema_version": 1,
        "data_origin": "synthetic_examples",
        "created_at_utc": now,
        "updated_at_utc": now,
        "annotation_guide_sha256": _sha256(ANNOTATION_GUIDE.read_bytes()),
        "initial_dataset_hash": dataset_hash(validated),
        "current_dataset_hash": dataset_hash(validated),
        "original_records": [sample.model_dump(mode="json") for sample in validated],
        "records": [
            {"sample_id": sample.sample_id, "text": sample.text, "split": sample.split,
             "initial_labels": _labels(sample), "current_labels": _labels(sample),
             "review_status": "draft", "reviewer": None, "reviewed_at_utc": None, "history": []}
            for sample in validated
        ],
    }


def validate_review_ledger(ledger: dict, samples: list[EvaluationSample]) -> dict:
    """Recompute history and hashes; no CSV reviewed flag substitutes for evidence."""
    try:
        parsed = ReviewLedger.model_validate(ledger)
    except (ValidationError, TypeError, ValueError) as error:
        raise ReviewValidationError("Review ledger structure is invalid.") from error
    current = validate_samples(samples)
    original = validate_samples(parsed.original_records)
    if any(sample.review_status != "draft" for sample in original):
        raise ReviewValidationError("The immutable initial review state must be draft.")
    if parsed.initial_dataset_hash != dataset_hash(original):
        raise ReviewValidationError("Initial review labels or dataset hash were changed.")
    if parsed.current_dataset_hash != dataset_hash(current):
        raise ReviewValidationError("Review ledger and CSV hashes differ; reload or restore the saved backup.")
    originals = {sample.sample_id: sample for sample in original}
    actual = {sample.sample_id: sample for sample in current}
    records = {record.sample_id: record for record in parsed.records}
    if len(records) != len(parsed.records) or records.keys() != actual.keys() or actual.keys() != originals.keys():
        raise ReviewValidationError("Review record IDs must exactly match the initial and current dataset.")
    for sample_id, record in records.items():
        source, sample = originals[sample_id], actual[sample_id]
        if (record.text, record.split) != (source.text, source.split) or (sample.text, sample.split) != (source.text, source.split):
            raise ReviewValidationError("Review must retain initial sample text, IDs, and fixed splits.")
        initial = ReviewLabels.model_validate(_labels(source))
        if record.initial_labels != initial:
            raise ReviewValidationError("Immutable initial labels do not match the original record.")
        labels, status = initial, "draft"
        reviewer, reviewed_at = None, None
        for sequence, event in enumerate(record.history, start=1):
            if event.sequence != sequence or event.before_labels != labels or event.before_status != status:
                raise ReviewValidationError("Review history is incomplete or does not form a valid sequence.")
            labels, status = event.after_labels, event.after_status
            reviewer = event.reviewer if status in COMPLETED_REVIEW_STATUSES else None
            reviewed_at = event.timestamp_utc if status in COMPLETED_REVIEW_STATUSES else None
        if (record.current_labels, record.review_status, record.reviewer, record.reviewed_at_utc) != (labels, status, reviewer, reviewed_at):
            raise ReviewValidationError("Review state does not match the saved review history and provenance.")
        if record.current_labels.model_dump(mode="json") != _labels(sample) or sample.review_status != status:
            raise ReviewValidationError("CSV labels or state differ from the recorded review evidence.")
    return parsed.model_dump(mode="json")


def load_review_ledger(dataset_path: str | Path = DEFAULT_DATASET, review_path: str | Path | None = None) -> dict:
    samples = load_dataset(dataset_path)
    target = review_path_for(dataset_path, review_path)
    if not target.exists():
        return new_review_ledger(samples)
    try:
        ledger = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReviewValidationError("Cannot read the review ledger JSON.") from error
    return validate_review_ledger(ledger, samples)


def review_checklist_csv(ledger: dict) -> bytes:
    """A prediction-free editable reading checklist; importing it never confirms."""
    output = io.StringIO(newline="")
    fields = ["sample_id", "split", "text", "review_status",
              *[f"initial_{field}" for field in LABEL_FIELDS], *LABEL_FIELDS,
              "reviewer", "reviewed_at_utc", "last_reason"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for record in ledger["records"]:
        history = record["history"]
        row = {
            "sample_id": record["sample_id"], "split": record["split"], "text": record["text"],
            "review_status": record["review_status"],
            **{f"initial_{key}": value for key, value in record["initial_labels"].items()},
            **record["current_labels"], "reviewer": record["reviewer"] or "",
            "reviewed_at_utc": record["reviewed_at_utc"] or "",
            "last_reason": history[-1]["reason"] if history else "",
        }
        writer.writerow({key: _escape_csv_cell(value) for key, value in row.items()})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def review_checklist_json(ledger: dict) -> bytes:
    return _json_bytes(ledger)


def _dataset_csv(samples: list[EvaluationSample]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=DATASET_FIELDS)
    writer.writeheader()
    writer.writerows(sample.model_dump(mode="json") for sample in samples)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


@contextmanager
def _review_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(target.suffix + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise ReviewValidationError("Another review save is in progress; retry after it finishes.") from error
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _save_pair(source: Path, target: Path, samples: list[EvaluationSample], ledger: dict, *, write_dataset: bool = True) -> list[str]:
    # Validate the full prospective state before creating backups or overwriting.
    validate_review_ledger(ledger, samples)
    source_before = source.read_bytes()
    review_before = target.read_bytes() if target.exists() else None
    backups = target.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
    paths = []
    for path, data in [(source, source_before), (target, review_before)]:
        if data is not None:
            backup = backups / f"{path.name}.{token}.bak"
            backup.write_bytes(data)
            paths.append(str(backup.resolve()))
    original = backups / f"{source.name}.original.{ledger['initial_dataset_hash']}.csv"
    if not original.exists():
        original.write_bytes(source_before if dataset_hash(load_dataset(source)) == ledger["initial_dataset_hash"]
                             else _dataset_csv(validate_samples(ledger["original_records"])))
    try:
        if write_dataset:
            _atomic_write(source, _dataset_csv(samples))
        _atomic_write(target, _json_bytes(ledger))
    except OSError:
        # Each replacement is atomic; restore the CSV if the second replacement fails.
        _atomic_write(source, source_before)
        if review_before is not None:
            _atomic_write(target, review_before)
        elif target.exists():
            target.unlink()
        raise
    return paths


def initialize_review(dataset_path: str | Path = DEFAULT_DATASET, review_path: str | Path | None = None) -> dict:
    """Persist the initial draft checklist and source backup without confirmation."""
    source, target = Path(dataset_path), review_path_for(dataset_path, review_path)
    with _review_lock(target):
        ledger = load_review_ledger(source, target)
        if not target.exists():
            _save_pair(source, target, load_dataset(source), ledger, write_dataset=False)
    return ledger


def save_review(
    dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None,
    *,
    updates: Mapping[str, Mapping[str, str] | ReviewLabels],
    reviewer: str,
    reasons: str | Mapping[str, str],
    confirm_ids: list[str] | tuple[str, ...] = (),
    human_confirmed: bool = False,
    expected_dataset_hash: str | None = None,
) -> dict:
    """Save validated changes; only explicit confirmation of listed IDs reviews.

    The UI calls the confirmation branch only after the user checks its separate
    human-confirmation box and presses the confirmation button. Do not call that
    branch for AI suggestions, automated review, or agent-written labels.
    """
    if not updates:
        raise ReviewValidationError("Select at least one concrete sample to save.")
    confirmed = set(confirm_ids)
    if len(confirmed) != len(confirm_ids) or not confirmed.issubset(updates):
        raise ReviewValidationError("Confirmation IDs must be unique and explicitly included in updates.")
    if confirmed and human_confirmed is not True:
        raise ReviewValidationError("Only the human's explicit confirmation can mark records reviewed.")
    if human_confirmed and not confirmed:
        raise ReviewValidationError("Explicit confirmation must list the involved sample IDs.")
    parsed_updates = {sample_id: _parse_labels(labels) for sample_id, labels in updates.items()}
    return _save_review_updates(
        dataset_path, review_path, parsed_updates=parsed_updates,
        reviewer=reviewer, reasons=reasons, confirmed=confirmed,
        expected_dataset_hash=expected_dataset_hash, ai_review=False,
    )


def save_ai_review(
    dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None,
    *,
    updates: Mapping[str, Mapping[str, str] | ReviewLabels],
    reasons: Mapping[str, str],
    expected_dataset_hash: str | None = None,
) -> dict:
    """Record an actual agent review as AI evidence, never human confirmation.

    The caller must first review each feedback against the annotation rules.
    An unchanged label still needs its own concrete rule-based reason. Human
    reviewed records cannot be overwritten or silently downgraded by this API.
    """
    if not updates:
        raise ReviewValidationError("Select at least one concrete sample for AI review.")
    if not isinstance(reasons, Mapping) or set(reasons) != set(updates):
        raise ReviewValidationError("AI review requires a separate reason for every selected sample ID.")
    parsed_updates = {sample_id: _parse_labels(labels) for sample_id, labels in updates.items()}
    return _save_review_updates(
        dataset_path, review_path, parsed_updates=parsed_updates,
        reviewer=AI_REVIEWER, reasons=reasons, confirmed=set(),
        expected_dataset_hash=expected_dataset_hash, ai_review=True,
    )


def _save_review_updates(
    dataset_path: str | Path,
    review_path: str | Path | None,
    *,
    parsed_updates: Mapping[str, ReviewLabels],
    reviewer: str,
    reasons: str | Mapping[str, str],
    confirmed: set[str],
    expected_dataset_hash: str | None,
    ai_review: bool,
) -> dict:
    """Share validation, locking, backups, and rollback for both review paths."""
    source, target = Path(dataset_path), review_path_for(dataset_path, review_path)
    with _review_lock(target):
        ledger = load_review_ledger(source, target)
        if expected_dataset_hash is not None and ledger["current_dataset_hash"] != expected_dataset_hash:
            raise ReviewValidationError("The dataset changed since this review screen loaded; reload before saving.")
        samples = load_dataset(source)
        records = {record["sample_id"]: record for record in ledger["records"]}
        if not parsed_updates.keys() <= records.keys():
            raise ReviewValidationError("Review updates contain unknown sample IDs.")
        if ai_review and any(records[sample_id]["review_status"] == "reviewed" for sample_id in parsed_updates):
            raise ReviewValidationError("AI review cannot overwrite human-reviewed records.")
        now = _utc_now()
        for sample_id, labels in parsed_updates.items():
            record = records[sample_id]
            status = "ai_reviewed" if ai_review else "reviewed" if sample_id in confirmed else "draft"
            try:
                event = ReviewEvent.model_validate({
                    "sequence": len(record["history"]) + 1,
                    "action": "ai_review" if ai_review else "human_confirmation" if sample_id in confirmed else "saved_draft",
                    "reviewer": reviewer, "timestamp_utc": now,
                    "before_labels": record["current_labels"], "after_labels": labels,
                    "before_status": record["review_status"], "after_status": status,
                    "reason": reasons.get(sample_id, "") if isinstance(reasons, Mapping) else reasons,
                    "explicit_confirmation": sample_id in confirmed,
                }).model_dump(mode="json")
            except (ValidationError, TypeError) as error:
                raise ReviewValidationError("Provide a reviewer and a non-empty rule-based reason before saving.") from error
            record["history"].append(event)
            record["current_labels"] = event["after_labels"]
            record["review_status"] = status
            record["reviewer"] = event["reviewer"] if status in COMPLETED_REVIEW_STATUSES else None
            record["reviewed_at_utc"] = now if status in COMPLETED_REVIEW_STATUSES else None
        revised = validate_samples([
            {**sample.model_dump(mode="json"), **records[sample.sample_id]["current_labels"],
             "review_status": records[sample.sample_id]["review_status"]}
            for sample in samples
        ])
        ledger["current_dataset_hash"] = dataset_hash(revised)
        ledger["updated_at_utc"] = now
        try:
            ledger = validate_review_ledger(ledger, revised)
            backup_paths = _save_pair(source, target, revised, ledger)
        except ValidationError as error:
            raise ReviewValidationError("Provide a reviewer and a non-empty rule-based reason before saving.") from error
    return {"ledger": ledger, "dataset_hash": ledger["current_dataset_hash"],
            "review_path": str(target.resolve()), "backup_paths": backup_paths,
            "confirmed_ids": [sample_id for sample_id in parsed_updates if sample_id in confirmed],
            "ai_reviewed_ids": list(parsed_updates) if ai_review else []}


def validate_review_evidence(
    dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None,
    *, require_all_reviewed: bool = False,
) -> dict:
    """Require saved evidence; completion can be human, AI, or mixed review."""
    source, target = Path(dataset_path), review_path_for(dataset_path, review_path)
    if not target.is_file():
        raise ReviewValidationError("A saved review ledger is required; CSV flags alone are insufficient.")
    ledger = load_review_ledger(source, target)
    if require_all_reviewed:
        expected = {**{f"dev-{number:03d}": "dev" for number in range(1, 41)},
                    **{f"test-{number:03d}": "test" for number in range(1, 21)}}
        actual = {record["sample_id"]: record["split"] for record in ledger["records"]}
        if actual != expected:
            raise ReviewValidationError("Final experiments require the fixed 60 IDs and unchanged 40/20 split.")
        pending = [record["sample_id"] for record in ledger["records"] if record["review_status"] not in COMPLETED_REVIEW_STATUSES]
        if pending:
            raise ReviewValidationError(f"Review is incomplete: {len(pending)} records remain draft.")
        if ledger["annotation_guide_sha256"] != _sha256(ANNOTATION_GUIDE.read_bytes()):
            raise ReviewValidationError("Annotation rules changed; reconcile the review evidence before freezing.")
    return ledger


def freeze_reviewed_dataset(
    dataset_path: str | Path = DEFAULT_DATASET,
    review_path: str | Path | None = None,
    *, output_dir: str | Path = Path("evaluation_reports") / "frozen",
) -> dict:
    """Freeze the reviewed CSV, evidence, rules, and byte/canonical hashes together."""
    source, target = Path(dataset_path), review_path_for(dataset_path, review_path)
    with _review_lock(target):
        ledger = validate_review_evidence(source, target, require_all_reviewed=True)
        samples = load_dataset(source)
        canonical_hash = dataset_hash(samples)
        root = Path(output_dir) / canonical_hash
        root.mkdir(parents=True, exist_ok=True)
        snapshot, evidence, guide = root / "feedback_eval.csv", root / "feedback_eval.review.json", root / "ANNOTATION_GUIDE.md"
        contents = [(snapshot, source.read_bytes()), (evidence, _json_bytes(ledger)), (guide, ANNOTATION_GUIDE.read_bytes())]
        for path, data in contents:
            if path.exists() and path.read_bytes() != data:
                raise ReviewValidationError("A frozen artifact with this dataset hash already exists and differs.")
        for path, data in contents:
            if not path.exists():
                _atomic_write(path, data)
        counts = Counter(sample.review_status for sample in samples)
        human_count, ai_count = counts["reviewed"], counts["ai_reviewed"]
        provenance = "mixed_human_ai" if human_count and ai_count else "human" if human_count else "ai"
        review_metadata = {
            "review_status_counts": {"draft": counts["draft"], "reviewed": human_count, "ai_reviewed": ai_count},
            "human_reviewed_count": human_count,
            "ai_reviewed_count": ai_count,
            "review_provenance": provenance,
            "review_note": (
                f"合成示例共 {len(samples)} 条：{human_count} 条人工确认、{ai_count} 条 AI 复审。"
                "AI 复审不等同于人工确认，也不是独立人工标注的业务测试集。"
                if ai_count else f"合成示例共 {len(samples)} 条，均有人工明确确认记录；仍不是业务数据。"
            ),
        }
        manifest = {
            "dataset_hash": canonical_hash, "snapshot_sha256": _sha256(snapshot.read_bytes()),
            "dataset_path": str(snapshot.resolve()), "review_path": str(evidence.resolve()),
            "review_sha256": _sha256(evidence.read_bytes()),
            "annotation_guide_sha256": _sha256(guide.read_bytes()), "reviewed_count": len(samples),
            "split_counts": dict(Counter(sample.split for sample in samples)),
            "sample_ids": [sample.sample_id for sample in samples], "frozen_at_utc": _utc_now(),
            "data_origin": "synthetic_examples",
            **review_metadata,
        }
        manifest_path = root / "dataset_manifest.json"
        if manifest_path.exists():
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
                frozen_time = datetime.fromisoformat(prior["frozen_at_utc"].replace("Z", "+00:00"))
                # Old all-human snapshots remain readable without rewriting
                # their evidence or claiming old AI review they never recorded.
                legacy_keys = manifest.keys() - review_metadata.keys()
                is_legacy_human = ai_count == 0 and prior.keys() == legacy_keys
                if not is_legacy_human and prior.keys() != manifest.keys():
                    raise ValueError("Frozen manifest fields differ")
                stable_keys = prior.keys() - {"frozen_at_utc"}
                if (frozen_time.tzinfo is None or frozen_time.utcoffset().total_seconds() != 0
                        or any(prior[key] != manifest[key] for key in stable_keys)):
                    raise ValueError("Frozen manifest differs from the verified artifacts")
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise ReviewValidationError("Frozen manifest is invalid or does not match the frozen artifacts.") from error
            return {**prior, **review_metadata}
        _atomic_write(manifest_path, _json_bytes(manifest))
        return manifest
