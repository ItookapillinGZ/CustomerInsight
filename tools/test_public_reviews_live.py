"""Explicitly authorised 20-review pilot; preserves the completed phase3 budget."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.csv_io import results_to_dataframe
from core.excel_io import read_feedback_xlsx, prepare_excel_feedback_rows
from core.customer_report import build_customer_report, export_customer_csv, export_customer_html
from core.product_service import ProductRequestBudget, ProductRunControl, analyze_customer_rows

class AuthorizedPilotBudget(ProductRequestBudget):
    def _day(self):
        return "authorised-public-pilot-20-total"  # Never resets on a new date.

def main():
    parser = argparse.ArgumentParser(description="20条公开评价验收；默认只校验，--live才调用模型。")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    output = ROOT / "validation/public_reviews_pilot"
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "summary.json"
    if args.live and summary_path.exists():
        print("已有验收结果，未重复发起调用：" + str(summary_path))
        return 0
    source = ROOT / "datasets/public_reviews/customer_reviews_sample.xlsx"
    frame = read_feedback_xlsx(source.read_bytes(), filename=source.name, sheet_name="公开顾客评价")
    rows = prepare_excel_feedback_rows(frame, "feedback")
    assert len(rows) == 20 and all(row.processing_status.value == "pending" for row in rows)
    assert Counter(frame["sentiment_label"]) == {"positive": 10, "negative": 10}
    if not args.live:
        print(json.dumps({"valid_rows": 20, "external_requests": 0, "live_requires": "--live"}))
        return 0
    phase_budget = ROOT / "validation/phase3_request_budget.json"
    phase_before = phase_budget.read_bytes()
    budget = AuthorizedPilotBudget(output / "authorised_usage.sqlite3", daily_limit=20)
    if budget.remaining() != 20:
        raise RuntimeError("本次20次授权已有消耗；请读取已保存日志，不清空台账或自动重跑。")
    control = ProductRunControl(budget, 20)
    plan = {"dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "source_ids": frame["source_id"].tolist(),
            "sample_count": 20, "source_distribution": dict(Counter(frame["source"])),
            "label_distribution": dict(Counter(frame["sentiment_label"])), "prompt_version": "v3",
            "max_attempts_total": 20, "application_retries": 0, "started_at_utc": datetime.now(timezone.utc).isoformat()}
    (output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "results.jsonl").open("a", encoding="utf-8") as journal:
        def persist(result):
            journal.write(json.dumps(result.model_dump(mode="json"), ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        # Preserve this completed, single-review historical pilot's semantics.
        results = analyze_customer_rows(rows, budget=budget, max_attempts=20, control=control,
                                       result_callback=persist, batch_size=1)
    report = build_customer_report(frame, results_to_dataframe(results), "feedback", "source")
    (output / "customer_analysis.csv").write_bytes(export_customer_csv(report))
    (output / "customer_analysis.html").write_bytes(export_customer_html(report))
    gold = frame["sentiment_label"].tolist()
    predicted = [row.sentiment.value if row.sentiment else "__failure__" for row in results]
    per_class = {}
    for label in ("positive", "negative"):
        tp = sum(a == label and b == label for a, b in zip(gold, predicted))
        fp = sum(a != label and b == label for a, b in zip(gold, predicted))
        fn = sum(a == label and b != label for a, b in zip(gold, predicted))
        per_class[label] = {"support": gold.count(label), "f1": 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0}
    assert phase_budget.read_bytes() == phase_before
    usage = [entry for entries in control.usage.values() for entry in entries if isinstance(entry, dict)]
    summary = {**plan, "completed_at_utc": datetime.now(timezone.utc).isoformat(), "actual_attempts": 20-budget.remaining(),
               "success_count": report["metrics"]["success_rows"], "failed_count": report["metrics"]["failed_rows"],
               "sentiment_accuracy_all_20": sum(a == b for a, b in zip(gold, predicted))/20,
               "sentiment_macro_f1_binary": sum(value["f1"] for value in per_class.values())/2,
               "per_class": per_class, "predicted_neutral_count": predicted.count("neutral"),
               "usage_returned_for": len(usage), "total_tokens": sum(entry.get("total_tokens",0) for entry in usage),
               "phase3_budget_unchanged": True,
               "interpretation": "UCI公开英文评价摘录20条的单次检查，原始正负情感标签；没有中性、类别和优先级金标，不能验证这些任务准确率或中文业务表现。"}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (output / "usage.json").write_text(json.dumps(control.usage, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("actual_attempts", "success_count", "failed_count", "sentiment_accuracy_all_20", "sentiment_macro_f1_binary", "total_tokens", "phase3_budget_unchanged")}))
    return 0

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
