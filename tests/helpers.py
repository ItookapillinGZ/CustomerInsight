"""Small deterministic offline fixtures and SDK doubles."""

import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def offline_temp_root():
    """Keep disposable test files under the project's writable temp root."""
    directory = Path(__file__).resolve().parents[1] / ".test-tmp"
    directory.mkdir(exist_ok=True)
    return directory


def write_draft_dataset(path):
    """Copy the current examples into an isolated, all-draft test fixture."""
    from evaluation.data import DEFAULT_DATASET

    target = Path(path)
    if target.resolve() == DEFAULT_DATASET.resolve():
        raise ValueError("A draft fixture must not replace the source dataset.")
    with DEFAULT_DATASET.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        records = list(reader)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({**record, "review_status": "draft"})
    return target


def valid_analysis(**changes):
    """Return realistic structured output with the original actionable items."""
    payload = {
        "sentiment": "negative",
        "category": "bugs",
        "priority": "high",
        "summary": "Checkout fails and prevents the customer from ordering.",
        "actionable_items": [
            {
                "action": "Fix the checkout failure.",
                "priority": "high",
                "category": "bugs",
                "reasoning": "The customer reports they cannot complete an order.",
            }
        ],
    }
    payload.update(changes)
    return payload


def fake_response(payload=None, **changes):
    response = {
        "output_parsed": payload if payload is not None else valid_analysis(),
        "status": "completed",
        "output": [],
    }
    response.update(changes)
    return SimpleNamespace(**response)


def fake_client():
    client = MagicMock(name="offline_openai_client")
    client.responses.parse.return_value = fake_response()
    return client
