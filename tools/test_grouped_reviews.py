"""Two-request authorised grouped pilot; default mode is entirely offline."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai.grouped_batch import GroupedFeedbackResponse
from core.config import Config
from core.csv_io import results_to_dataframe
from core.customer_report import build_customer_report, export_customer_csv, export_customer_html
from core.excel_io import read_feedback_xlsx, prepare_excel_feedback_rows
from core.product_service import ProductRequestBudget, ProductRunControl, analyze_customer_rows, estimate_customer_requests


class GroupedPilotBudget(ProductRequestBudget):
    def _day(self):
        return "authorised-grouped-pilot-2-total"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="合并调用20条公开评价验收，默认离线；--live最多2次真实请求。")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    folder = ROOT / "validation/grouped_public_reviews_pilot"
    summary_path = folder / "summary.json"
    if args.live and summary_path.exists():
        print("已存在分组验收结果，未重复调用：" + str(summary_path))
        return 0
    source = ROOT / "datasets/public_reviews/customer_reviews_sample.xlsx"
    frame = read_feedback_xlsx(source.read_bytes(), source.name, "公开顾客评价")
    rows = prepare_excel_feedback_rows(frame, "feedback")
    expected = estimate_customer_requests(rows, batch_size=10, max_characters=12000)
    assert len(rows) == 20 and expected == 2
    assert all(row.processing_status.value == "pending" for row in rows)
    if not args.live:
        print(json.dumps({"valid_rows": 20, "planned_requests": 2, "external_requests": 0}))
        return 0
    folder.mkdir(parents=True, exist_ok=True)
    budget = GroupedPilotBudget(folder / "authorised_usage.sqlite3", 2)
    if budget.remaining() != 2:
        raise RuntimeError("本轮授权已有消耗，禁止重置台账或自动追加调用；请读取已保存的逐条结果。")
    protected = [ROOT / "validation/phase3_request_budget.json",
                 ROOT / "validation/public_reviews_pilot/summary.json"]
    protected_before = {str(path.relative_to(ROOT)): digest(path) for path in protected}
    plan = {"sample_count": 20, "dataset_sha256": digest(source),
            "source_ids": frame["source_id"].tolist(), "prompt_version": "v3_grouped_v1",
            "grouped_code_sha256": digest(ROOT / "ai/grouped_batch.py"),
            "output_schema_sha256": hashlib.sha256(json.dumps(GroupedFeedbackResponse.model_json_schema(), sort_keys=True).encode()).hexdigest(),
            "planned_requests": 2, "max_attempts_total": 2, "batch_size": 10, "max_characters": 12000,
            "application_retries": 0, "sdk_retries": 0, "api_mode": Config.API_MODE,
            "started_at_utc": datetime.now(timezone.utc).isoformat()}
    (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    control = ProductRunControl(budget, 2)
    run_state = {}
    with (folder / "results.jsonl").open("a", encoding="utf-8") as journal:
        def persist(row):
            journal.write(json.dumps(row.model_dump(mode="json"), ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        results = analyze_customer_rows(rows, budget=budget, max_attempts=2, control=control,
                                        batch_size=10, max_characters=12000,
                                        result_callback=persist, run_state=run_state)
    report = build_customer_report(frame, results_to_dataframe(results), "feedback", "source",
                                   analysis_meta=results.analysis_meta)
    report["notes"].append("本验收使用UCI Sentiment Labelled Sentences（Dimitrios Kotzias, 2015, CC BY 4.0）的20条英文评价摘录；不代表中文业务效果。来源：https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences")
    (folder / "customer_analysis.csv").write_bytes(export_customer_csv(report))
    (folder / "customer_analysis.html").write_bytes(export_customer_html(report))
    predicted = [row.sentiment.value if row.sentiment else "__failure__" for row in results]
    gold = frame["sentiment_label"].tolist()
    f1s = []
    for label in ("positive", "negative"):
        tp = sum(a == label and b == label for a, b in zip(gold, predicted))
        fp = sum(a != label and b == label for a, b in zip(gold, predicted))
        fn = sum(a == label and b != label for a, b in zip(gold, predicted))
        f1s.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0)
    protected_unchanged = all(digest(path) == protected_before[str(path.relative_to(ROOT))] for path in protected)
    assert protected_unchanged
    meta = results.analysis_meta
    summary = {**plan, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
               "actual_attempts": control.attempts, "successful_rows": report["metrics"]["success_rows"],
               "failed_rows": report["metrics"]["failed_rows"], "pending_rows": report["metrics"]["pending_rows"],
               "sentiment_accuracy_all_20": sum(a == b for a, b in zip(gold, predicted)) / 20,
               "sentiment_macro_f1_binary": sum(f1s) / 2, "predicted_neutral_count": predicted.count("neutral"),
               "total_tokens": meta["total_tokens"], "usage_reported_requests": meta["usage_reported_requests"],
               "elapsed_seconds": meta["elapsed_seconds"], "protected_history_unchanged": protected_unchanged,
               "interpretation": "20条公开英文明确正负评价的一次合并检查；无中性、类别或优先级金标，不能证明中文业务准确率。"}
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (folder / "usage.json").write_text(json.dumps(control.usage, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("actual_attempts", "successful_rows", "failed_rows", "pending_rows", "sentiment_accuracy_all_20", "total_tokens", "protected_history_unchanged")}))
    return 0 if report["metrics"]["success_rows"] == 20 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
