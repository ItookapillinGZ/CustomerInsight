"""Strict local-only evaluation data loading; gold fields never form model input.

Unlike the application CSV preparation flow, this module rejects bad datasets
as a whole and preserves every author-assigned sample identifier.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from core.config import Config
from models.enums import ActionItemPriorityType, SentimentType, TopicType


DEFAULT_DATASET = Path(__file__).parent / "data" / "feedback_eval.csv"
Split = Literal["dev", "test"]
ReviewStatus = Literal["draft", "reviewed", "ai_reviewed"]
COMPLETED_REVIEW_STATUSES = frozenset(("reviewed", "ai_reviewed"))
DATASET_FIELDS = (
    "sample_id", "text", "gold_sentiment", "gold_category", "gold_priority",
    "split", "review_status",
)


class DatasetValidationError(ValueError):
    """The dataset is invalid; no sample is silently dropped or relabelled."""


class EvaluationSample(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    sample_id: str
    text: str
    gold_sentiment: SentimentType
    gold_category: TopicType
    gold_priority: ActionItemPriorityType
    split: Split
    review_status: ReviewStatus

    @field_validator("sample_id")
    @classmethod
    def require_identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sample_id must contain text")
        if value != value.strip():
            raise ValueError("sample_id must not have surrounding whitespace")
        return value

    @field_validator("text")
    @classmethod
    def require_feedback(cls, value: str) -> str:
        if not normalize_text(value):
            raise ValueError("text must contain non-whitespace feedback")
        if len(value) > Config.MAX_TEXT_LENGTH:
            raise ValueError(f"text exceeds {Config.MAX_TEXT_LENGTH} characters")
        return value


def normalize_text(value: str) -> str:
    """Normalize Unicode compatibility forms and whitespace for leakage checks."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _gold(sample: EvaluationSample) -> tuple[str, str, str]:
    return (sample.gold_sentiment.value, sample.gold_category.value, sample.gold_priority.value)


def validate_samples(
    samples: Iterable[EvaluationSample | Mapping[str, object]],
) -> list[EvaluationSample]:
    """Validate individual fields and all cross-record constraints independently.

    Even model_construct() objects are validated again. Exact source text and
    sample_id are retained; normalization is used only to detect duplication.
    """
    validated: list[EvaluationSample] = []
    ids: dict[str, EvaluationSample] = {}
    texts: dict[str, EvaluationSample] = {}
    for number, raw in enumerate(samples, start=1):
        try:
            sample = EvaluationSample.model_validate(raw)
        except ValidationError as exc:
            # Do not echo full row contents into error messages.
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or 'record'}: {error['type']}"
                for error in exc.errors(include_input=False)
            )
            raise DatasetValidationError(f"Record {number} is invalid ({details}).") from exc
        prior_id = ids.get(sample.sample_id)
        if prior_id is not None:
            suffix = " with conflicting annotation" if _gold(sample) != _gold(prior_id) else ""
            raise DatasetValidationError(f"Duplicate sample_id {sample.sample_id!r}{suffix}.")
        normalized = normalize_text(sample.text)
        prior_text = texts.get(normalized)
        if prior_text is not None:
            if _gold(sample) != _gold(prior_text):
                reason = "conflicting annotation for normalized duplicate text"
            elif sample.split != prior_text.split:
                reason = "duplicate text across splits"
            else:
                reason = "duplicate text within a split"
            raise DatasetValidationError(
                f"Samples {prior_text.sample_id!r} and {sample.sample_id!r}: {reason}."
            )
        ids[sample.sample_id] = sample
        texts[normalized] = sample
        validated.append(sample)
    if not validated:
        raise DatasetValidationError("The evaluation dataset has no samples.")
    return validated


def load_dataset(path: str | Path = DEFAULT_DATASET) -> list[EvaluationSample]:
    """Read a strict UTF-8 CSV without application row rewriting or skipping."""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)
            if header is None or len(header) != len(DATASET_FIELDS) or set(header) != set(DATASET_FIELDS):
                raise DatasetValidationError(
                    "Dataset headers must contain exactly: " + ", ".join(DATASET_FIELDS)
                )
            records = []
            for row_number, values in enumerate(reader, start=2):
                if len(values) != len(header):
                    raise DatasetValidationError(f"CSV record {row_number} has the wrong field count.")
                records.append(dict(zip(header, values)))
    except (UnicodeError, csv.Error) as exc:
        raise DatasetValidationError("Dataset must be a well-formed UTF-8 CSV.") from exc
    except OSError as exc:
        raise DatasetValidationError(f"Cannot read evaluation dataset ({type(exc).__name__}).") from exc
    return validate_samples(records)


def select_split(samples: Iterable[EvaluationSample], split: Split) -> list[EvaluationSample]:
    if split not in ("dev", "test"):
        raise DatasetValidationError("split must be 'dev' or 'test'.")
    return [sample for sample in validate_samples(samples) if sample.split == split]


def dataset_hash(samples: Iterable[EvaluationSample]) -> str:
    """Hash complete canonical records including gold, split, and review state.

    Sorting by preserved identifier makes the hash independent of CSV row order.
    Callers should pass the whole dataset for v1/v2 comparison provenance.
    """
    records = [sample.model_dump(mode="json") for sample in validate_samples(samples)]
    serialized = json.dumps(
        sorted(records, key=lambda record: record["sample_id"]),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
