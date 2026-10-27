"""One bounded follow-up to the recorded five-request workspace acceptance."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.test_workspace_acceptance import (
    OUTPUT_DIR, SUMMARY_PATH, USAGE_DB, WorkspaceAcceptanceBudget,
    _fixture_batches, _report, _report_view, _report_checks,
)
from core.csv_io import results_to_dataframe
from core.excel_io import prepare_excel_feedback_rows
from core.history_store import HistoryStore
from core.history_workflow import attach_historical_context
from core.product_service import ProductRunControl, analyze_customer_rows
from models.models import FeedbackResult


def main():
    parser = argparse.ArgumentParser(description="最多使用原授权5次中的最后1次，不重置原台账。默认不调用模型。")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-requests", type=int)
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"mode": "offline_plan", "external_requests": 0, "scope": "Recheck current three synthetic reviews after the recorded semantic boundary defect."}))
        return 0
    if args.max_requests != 1:
        parser.error("显式 --max-requests 1 才可执行；未发送请求。")
    baseline_bytes = SUMMARY_PATH.read_bytes()
    baseline = json.loads(baseline_bytes)
    destination = OUTPUT_DIR / "correction_check.json"
    lock = OUTPUT_DIR / "correction.lock"
    budget = WorkspaceAcceptanceBudget(USAGE_DB, daily_limit=5)
    if baseline["actual_attempts"] != 4 or budget.remaining() != 1 or destination.exists() or lock.exists():
        raise ValueError("本次修正仅适用原台账已用4次、剩余1次且尚未修正的记录。")
    with lock.open("x", encoding="utf-8") as handle:
        handle.write("One remaining request only. Do not reset the ledger.\n")
    control = ProductRunControl(budget, max_attempts=1)
    record = {"baseline_summary_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "authorization_total": 5,
        "original_attempts": 4, "max_additional_attempts": 1,
        "reason": "Product intact was overgeneralized to packaging intact; factual intact plus future request should be neutral.",
        "grouped_code_sha256": hashlib.sha256((ROOT / "ai/grouped_batch.py").read_bytes()).hexdigest(),
        "results": None, "checks": {}, "error_type": None, "human_review": "pending"}
    try:
        batches = _fixture_batches()
        rows = prepare_excel_feedback_rows(batches[-1]["frame"], "评价")
        results = analyze_customer_rows(rows, budget=budget, control=control, max_attempts=1, batch_size=10)
        report = _report(batches[-1], results, results.analysis_meta)
        record["results"] = [row.model_dump(mode="json") for row in results]
        record["report"] = _report_view(report)
        record["checks"] = _report_checks(report, batches[-1])
        fourth = next(row for row in results if row.source_row == 4)
        record["checks"].update(
            intact_product_not_inferred_as_intact_packaging=not any(aspect.topic == "包装完整" for aspect in fourth.aspects),
            factual_intact_plus_future_request_is_neutral=fourth.sentiment.value == "neutral",
            reduction_requests_preserved=any(topic["key"] == "request:包装减塑" and topic["review_count"] == 3 for topic in report["topics"]),
        )
        with tempfile.TemporaryDirectory(dir=OUTPUT_DIR, prefix="corrected-") as directory:
            store = HistoryStore(directory)
            previous_results = [FeedbackResult.model_validate(row) for row in baseline["batch_records"][0]["results"]]
            old = _report(batches[0], previous_results, baseline["batch_records"][0]["report"]["analysis_meta"])
            ids = [store.save_batch(value, filename=batch["filename"], file_bytes=batch["file_bytes"],
                      mapping={"text_column": "评价", "group_column": "产品", "date_column": "日期"})
                   for batch, value in zip(batches, (old, report))]
            context = attach_historical_context(report, store.load_batch(ids[0]))
            record["comparison"] = context["historical_context"]["comparison"]
            record["batch_ids"] = ids
            (OUTPUT_DIR / "corrected_workspace_backup.zip").write_bytes(store.export_backup())
    except BaseException as error:
        record["error_type"] = type(error).__name__
        if not isinstance(error, Exception):
            raise
    finally:
        record.update(actual_additional_attempts=control.attempts, usage=control.usage,
            cumulative_attempts=5 - budget.remaining(), remaining_authorized_requests=budget.remaining(),
            baseline_unchanged=SUMMARY_PATH.read_bytes() == baseline_bytes,
            completed_at_utc=datetime.now(timezone.utc).isoformat())
        record["checks_passed"] = bool(record["checks"]) and all(record["checks"].values()) and record["error_type"] is None
        destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks_passed": record["checks_passed"], "actual_additional_attempts": control.attempts,
                      "cumulative_attempts": record["cumulative_attempts"], "baseline_unchanged": record["baseline_unchanged"]}))
    return 0 if record["checks_passed"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
