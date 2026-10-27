"""Bounded 1.7 workspace acceptance; offline unless explicitly invoked --live.

The six reviews are hand-authored synthetic fixtures, not customer accuracy data.
Automatic checks cover transport, persistence and source membership only. They
never replace human review of the meaning or usefulness of an Agent answer.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import zipfile

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.analysis_agent import (  # noqa: E402
    DEFAULT_MAX_MODEL_CALLS, DEFAULT_MAX_TOOL_CALLS, TOOL_NAMES, report_fingerprint,
)
from core.config import Config  # noqa: E402
from core.csv_io import results_to_dataframe  # noqa: E402
from core.customer_report import build_customer_report  # noqa: E402
from core.excel_io import prepare_excel_feedback_rows  # noqa: E402
from core.history_store import HistoryStore  # noqa: E402
from core.history_workflow import attach_historical_context, date_scope  # noqa: E402
from core.product_service import (  # noqa: E402
    ProductRequestBudget, ProductRunControl, analyze_customer_rows, run_customer_report_agent,
)
from core.review_topics import validate_aspects  # noqa: E402
from models.models import FeedbackResult  # noqa: E402


OUTPUT_DIR = ROOT / "validation" / "workspace_acceptance"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
USAGE_DB = OUTPUT_DIR / "authorised_usage.sqlite3"
LOCK_PATH = OUTPUT_DIR / "live_run.lock"
MAX_REQUESTS = 5
SHANGHAI = timezone(timedelta(hours=8))
QUESTION = "与上批相比包装反馈有什么变化？结合上批和本批原文给建议，区分事实和推测。"
MAPPING = {"sheet_name": "Sheet1", "encoding": None, "text_column": "评价",
           "group_column": "产品", "date_column": "日期"}
CODE_FILES = (
    "ai/client.py", "ai/analyzer.py", "ai/grouped_batch.py", "ai/prompts.py",
    "core/analysis_agent.py", "core/agent_table_tools.py", "core/product_service.py",
    "core/config.py", "core/customer_report.py", "core/history_store.py", "core/history_workflow.py",
    "core/review_topics.py", "models/models.py", "models/enums.py", "tools/test_workspace_acceptance.py",
)
PROTECTED_FILES = (
    "validation/reasoning_agent_acceptance/summary.json",
    "validation/agent_live_acceptance/summary.json", "validation/phase3_request_budget.json",
)
RUBRIC = {
    "directness": "是否直接回答包装反馈的批次变化，而非重复整体情感比例",
    "grounding": "对两批的描述及引用是否来自各自原文，是否避免混淆同号原始行",
    "comparison": "是否展示样本数与分母，不将合成小样本变化当作普遍趋势",
    "actionability": "建议是否具体关联反馈，区分建议、已知事实与待验证推测",
    "causality": "是否避免编造原因或把变化认定为经营措施的因果效果",
}


class WorkspaceAcceptanceBudget(ProductRequestBudget):
    """One cumulative authorization; midnight never renews these five units."""

    def _day(self):
        return "workspace-agent-v1.7.0-20261020-total"


class AcceptanceCheckFailure(ValueError):
    pass


def _write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_batches() -> list[dict]:
    texts = (
        ("杯盖拧紧后没有漏水，外盒完整。", "杯子保温不错，塑料缓冲包装有点多，希望能减少。", "收到的杯子完好，外包装结实。"),
        ("杯盖拧紧后没有漏水，不过希望减少塑料缓冲包装。", "杯子保温不错，希望减少塑料缓冲包装。", "收到的杯子完好，希望外包装少用塑料。"),
    )
    dates = ("2026-10-01", "2026-10-15")
    batches = []
    for index, reviews in enumerate(texts):
        frame = pd.DataFrame({"产品": ["保温杯"] * 3, "评价": list(reviews), "日期": [dates[index]] * 3})
        buffer = BytesIO()
        frame.to_excel(buffer, index=False, sheet_name="Sheet1")
        batches.append({"key": "previous" if index == 0 else "current", "filename": f"synthetic_{index + 1}.xlsx",
                        "frame": frame, "file_bytes": buffer.getvalue()})
    return batches


def _synthetic_results(batch: dict) -> list[FeedbackResult]:
    results = []
    for row in prepare_excel_feedback_rows(batch["frame"], "评价"):
        text = row.original_text
        request = "希望" in text
        topic, kind, sentiment = ("包装减塑", "request", "neutral") if request else ("包装完整", "praise", "positive")
        results.append(FeedbackResult.model_validate({**row.model_dump(), "processing_status": "success",
            "sentiment": sentiment, "category": "product_quality", "priority": "low", "summary": text,
            "aspects": [{"topic": topic, "kind": kind, "sentiment": sentiment, "evidence": text}]}))
    return results


def _report(batch: dict, results, metadata: dict) -> dict:
    report = build_customer_report(batch["frame"], results_to_dataframe(results), "评价", "产品")
    report["analysis_meta"] = dict(metadata)
    report["period"] = date_scope(batch["frame"], "日期")
    return report


def _report_view(report: dict) -> dict:
    return {key: report.get(key) for key in (
        "metrics", "analysis_meta", "period", "topics_version", "topic_coverage", "topics", "aspects_by_row")}


def _report_checks(report: dict, batch: dict) -> dict:
    originals = {index + 2: text for index, text in enumerate(batch["frame"]["评价"].tolist())}
    records = report.get("aspects_by_row", [])
    aspects_valid = True
    try:
        for record in records:
            row = record["source_row"]
            if row not in originals or not validate_aspects(record["aspects"], originals[row]):
                aspects_valid = False
    except (ValueError, TypeError, KeyError):
        aspects_valid = False
    return {
        "three_valid_reviews": report["metrics"]["success_rows"] == 3,
        "complete_topic_coverage": report.get("topic_coverage", {}).get("status") == "complete",
        "every_source_has_aspects": len(records) == 3 and {item["source_row"] for item in records} == set(originals),
        "aspect_quotes_belong_to_their_original_rows": aspects_valid,
        "topics_present": bool(report.get("topics")),
    }


def _storage_checks(store: HistoryStore, batches: list[dict], reports: list[dict]) -> tuple[dict, dict, list[str], bytes]:
    ids = [store.save_batch(report, filename=batch["filename"], file_bytes=batch["file_bytes"], mapping=MAPPING)
           for batch, report in zip(batches, reports)]
    restarted = HistoryStore(store.root_dir)
    previous, current = (restarted.load_batch(identifier) for identifier in ids)
    prepared = attach_historical_context(current["report"], previous)
    backup = restarted.export_backup()
    with zipfile.ZipFile(BytesIO(backup)) as archive:
        names = archive.namelist()
        backup_ok = "customerinsight.sqlite3" in names and sum(name.startswith("originals/") for name in names) == 2
    checks = {
        "two_batches_after_store_restart": len(restarted.list_batches()) == 2,
        "originals_preserved": all(restarted.read_original(identifier) == batch["file_bytes"] for identifier, batch in zip(ids, batches)),
        "report_fingerprints_preserved": all(report_fingerprint(restarted.load_batch(identifier)["report"]) == report_fingerprint(report)
                                              for identifier, report in zip(ids, reports)),
        "selected_previous_batch_only": prepared["historical_context"]["batch"]["id"] == ids[0]
            and len(prepared["historical_context"]["evidence"]) == 3,
        "base_reports_have_no_historical_context": all("historical_context" not in restarted.load_batch(identifier)["report"] for identifier in ids),
        "batch_definitions_comparable": prepared["historical_context"]["comparison"]["comparable"] is True,
        "backup_contains_database_and_two_originals": backup_ok,
    }
    return prepared, checks, ids, backup


def _plan(batches: list[dict]) -> dict:
    model = {"model": Config.OPENAI_MODEL, "api_mode": Config.API_MODE,
             "output_mode": Config.CHAT_OUTPUT_MODE if Config.API_MODE == "chat_completions" else "json_schema"}
    return {
        "fixture_kind": "hand_authored_synthetic_cup_reviews_two_batches", "fixture_rows_per_batch": 3,
        "fixtures": [{"key": batch["key"], "filename": batch["filename"],
                      "records": batch["frame"].to_dict(orient="records")} for batch in batches],
        "question": QUESTION, "hard_maximum_requests": MAX_REQUESTS,
        "planned_requests": {"previous_batch": 1, "current_batch": 1, "agent_maximum": DEFAULT_MAX_MODEL_CALLS},
        "model_configuration": model,
        "model_configuration_sha256": hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest(),
        "source_code_sha256": {name: _digest(ROOT / name) for name in CODE_FILES},
        "human_review": {"status": "pending", "reviewer": None, "scores": None, "rubric": RUBRIC},
    }


def _offline(batches: list[dict], plan: dict) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # A hard guard documents that preparation cannot create a model client.
    with patch("core.analysis_agent.get_openai_client", side_effect=AssertionError("offline")), \
         patch("ai.client.get_openai_client", side_effect=AssertionError("offline")), \
         tempfile.TemporaryDirectory(prefix="offline-", dir=OUTPUT_DIR) as directory:
        reports = [_report(batch, _synthetic_results(batch), {"prompt_version": "v3_grouped_aspects_v2", "mode": "synthetic_offline"})
                   for batch in batches]
        batch_checks = [_report_checks(report, batch) for report, batch in zip(reports, batches)]
        prepared, storage, _, _ = _storage_checks(HistoryStore(directory), batches, reports)
        changes = prepared["historical_context"]["comparison"]["topics"]
        plastics = next((item for item in changes if item["key"] == "request:包装减塑"), {})
        checks = {**storage, "all_synthetic_topic_checks": all(all(item.values()) for item in batch_checks),
                  "known_synthetic_request_change": plastics.get("previous_count") == 1 and plastics.get("current_count") == 3}
        result = {**plan, "mode": "offline_preparation", "external_requests": 0, "actual_attempts": 0,
                  "checked_at": datetime.now(SHANGHAI).isoformat(), "checks": checks,
                  "automatic_checks_passed": all(checks.values()), "reports": [_report_view(report) for report in reports],
                  "comparison": prepared["historical_context"]["comparison"],
                  "scope": "Synthetic extraction plus local SQLite restart, report identity, comparison and backup structure; no model answer was tested."}
        _write_json(OUTPUT_DIR / "offline_summary.json", result)
    print(json.dumps({"mode": "offline_preparation", "external_requests": 0, "checks_passed": result["automatic_checks_passed"],
                      "output": str(OUTPUT_DIR / "offline_summary.json")}, ensure_ascii=False))
    return 0 if result["automatic_checks_passed"] else 1


def _agent_checks(result, prepared: dict, previous_id: str, current_control: ProductRunControl) -> dict:
    source_rows = set(prepared["detail"][prepared["detail_columns"]["source_row"]].tolist())
    past = {item["source_row"]: item["text"] for item in prepared["historical_context"]["evidence"]}
    references = result.historical_evidence
    historical_match = bool(references) and all(item.get("batch_id") == previous_id
        and item.get("filename") == prepared["historical_context"]["batch"]["filename"]
        and item.get("source_row") in past and bool(item.get("text"))
        and item["text"] in past[item["source_row"]] for item in references)
    return {
        "completed_llm_answer": result.status == "completed" and result.answer_origin == "llm" and bool(result.conclusion.strip()),
        "answer_is_supported": result.answer_type == "supported",
        "transport_attempts_match": result.model_calls == current_control.attempts and 1 <= result.model_calls <= DEFAULT_MAX_MODEL_CALLS,
        "tool_allowlist_and_budget": result.tool_calls == len(result.tool_trace) and 2 <= result.tool_calls <= DEFAULT_MAX_TOOL_CALLS
            and set(result.tool_trace) <= set(TOOL_NAMES),
        "selected_snapshot_fingerprint": result.report_fingerprint == report_fingerprint(prepared),
        "current_source_rows_cited": bool(result.evidence_rows) and set(result.evidence_rows) <= source_rows,
        "previous_source_rows_cited_in_separate_namespace": historical_match,
        "historical_comparison_tool_chosen": "compare_history" in result.tool_trace,
        "historical_original_tool_chosen": "read_previous_reviews" in result.tool_trace,
        "current_original_tool_chosen": bool(set(result.tool_trace) & {"get_review_topics", "search_reviews", "read_table_rows"}),
    }


def _live(batches: list[dict], plan: dict) -> int:
    if SUMMARY_PATH.exists() or LOCK_PATH.exists():
        raise AcceptanceCheckFailure("LiveAcceptanceAlreadyRecorded")
    if not Config.OPENAI_API_KEY:
        raise AcceptanceCheckFailure("MissingCredentials")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also blocks concurrent launches before either reserves.
    with LOCK_PATH.open("x", encoding="utf-8") as lock:
        lock.write("single-flight workspace acceptance, five cumulative requests\n")
    budget = WorkspaceAcceptanceBudget(USAGE_DB, daily_limit=MAX_REQUESTS)
    if budget.remaining() != MAX_REQUESTS:
        raise AcceptanceCheckFailure("ExistingCumulativeUsage")
    protected = {name: _digest(ROOT / name) for name in PROTECTED_FILES if (ROOT / name).exists()}
    records, reports, controls, ids = [], [], [], []
    agent_record, storage, comparison = None, {}, {}
    error_type = None
    interrupted = None
    plan = {**plan, "mode": "live_acceptance", "started_at": datetime.now(SHANGHAI).isoformat()}
    _write_json(OUTPUT_DIR / "plan.json", plan)
    try:
        with tempfile.TemporaryDirectory(prefix="live-", dir=OUTPUT_DIR) as directory:
            store = HistoryStore(directory)
            for batch in batches:
                control = ProductRunControl(budget, max_attempts=1)
                controls.append(control)
                record = {"key": batch["key"], "actual_attempts": 0, "usage": {}, "results": None, "checks": {}}
                records.append(record)
                try:
                    results = analyze_customer_rows(prepare_excel_feedback_rows(batch["frame"], "评价"),
                        budget=budget, control=control, max_attempts=1, batch_size=10, max_characters=12000)
                    # Never retain service exception strings, even on failed rows.
                    record["results"] = [{**row.model_dump(mode="json"), "error_message": "分析未完成" if row.error_message else None} for row in results]
                    report = _report(batch, results, results.analysis_meta)
                    record["report"] = _report_view(report)
                    record["checks"] = {**_report_checks(report, batch), "one_transport_attempt": control.attempts == 1}
                    if not all(record["checks"].values()):
                        raise AcceptanceCheckFailure("IncompleteGroundedAspectExtraction")
                    reports.append(report)
                finally:
                    record.update(actual_attempts=control.attempts, usage=control.usage)
            prepared, storage, ids, backup = _storage_checks(store, batches, reports)
            comparison = prepared["historical_context"]["comparison"]
            if not all(storage.values()):
                raise AcceptanceCheckFailure("PersistenceOrComparisonCheckFailed")
            (OUTPUT_DIR / "workspace_backup.zip").write_bytes(backup)
            control = ProductRunControl(budget, max_attempts=DEFAULT_MAX_MODEL_CALLS)
            controls.append(control)
            agent_record = {"question": QUESTION, "result": None, "actual_attempts": 0, "usage": {}, "checks": {}}
            try:
                result = run_customer_report_agent(prepared, QUESTION, budget=budget, control=control)
                agent_record["result"] = result.model_dump(mode="json")
                agent_record["checks"] = _agent_checks(result, prepared, ids[0], control)
                if result.status == "completed":
                    store.save_chat(ids[1], [{"question": QUESTION, "result": result.model_dump(mode="json"), "comparison_batch_id": ids[0]}])
                    agent_record["checks"]["comparison_chat_restored"] = len(HistoryStore(directory).load_chat(ids[1])) == 1
                    (OUTPUT_DIR / "workspace_backup.zip").write_bytes(store.export_backup())
            finally:
                agent_record.update(actual_attempts=control.attempts, usage=control.usage)
    except BaseException as error:
        error_type = type(error).__name__
        if not isinstance(error, Exception):
            interrupted = error
    finally:
        used = MAX_REQUESTS - budget.remaining()
        known_attempts = sum(control.attempts for control in controls)
        protected_unchanged = all((ROOT / name).exists() and _digest(ROOT / name) == digest for name, digest in protected.items())
        code_unchanged = all((ROOT / name).exists() and _digest(ROOT / name) == digest for name, digest in plan["source_code_sha256"].items())
        checks = {**storage, "two_batches_completed": len(reports) == 2,
                  "batch_extraction_checks": len(records) == 2 and all(record["checks"] and all(record["checks"].values()) for record in records),
                  "agent_checks": bool(agent_record and agent_record["checks"] and all(agent_record["checks"].values())),
                  "cumulative_attempt_ledger_matches": used == known_attempts and 0 <= used <= MAX_REQUESTS,
                  "protected_prior_acceptance_unchanged": protected_unchanged, "code_unchanged_during_run": code_unchanged,
                  "no_interruption_or_exception": error_type is None}
        summary = {**plan, "completed_at": datetime.now(SHANGHAI).isoformat(), "actual_attempts": used,
                   "external_requests": used, "remaining_authorized_requests": MAX_REQUESTS - used,
                   "batch_records": records, "batch_ids": ids, "comparison": comparison, "agent": agent_record,
                   "error_type": error_type, "checks": checks, "automatic_checks_passed": all(checks.values()),
                   "human_review": {"status": "pending", "reviewer": None, "reviewed_at": None, "scores": None,
                                    "notes": None, "rubric": RUBRIC},
                   "scope": "Transport accounting, schemas, local persistence and source membership only; no semantic accuracy or human quality score."}
        _write_json(SUMMARY_PATH, summary)
    print(json.dumps({"summary": str(SUMMARY_PATH), "actual_attempts": used,
                      "automatic_checks_passed": summary["automatic_checks_passed"], "human_review": "pending"}, ensure_ascii=False))
    if interrupted is not None:
        raise interrupted
    return 0 if summary["automatic_checks_passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="1.7 工作台小样本验收；默认离线，真实调用须另外取得用户授权。")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=None)
    args = parser.parse_args()
    if args.live and args.max_requests != MAX_REQUESTS:
        parser.error("真实验收必须显式指定 --live --max-requests 5；未发送请求。")
    if not args.live and args.max_requests is not None:
        parser.error("离线验收不接受请求额度参数；未发送请求。")
    try:
        batches = _fixture_batches()
        plan = _plan(batches)
        return _live(batches, plan) if args.live else _offline(batches, plan)
    except Exception as error:
        print(json.dumps({"completed": False, "error_type": type(error).__name__,
                          "message": "验收未能完成；已有记录、锁和累计额度不会自动覆盖或重置。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
