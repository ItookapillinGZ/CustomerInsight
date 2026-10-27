"""Acceptance preparation for the 1.6.0 reasoning Agent; offline by default.

Only --live together with an explicit --max-requests can send model requests.
The fixed fixture is synthetic, hand-authored, positive/neutral, and not evidence
of real customer accuracy. Automatic source checks do not score prose quality.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.analysis_agent import (  # noqa: E402
    AgentDecision, DEFAULT_MAX_MODEL_CALLS, DEFAULT_MAX_TOOL_CALLS,
    TOOL_NAMES, ReportAgentContext, report_fingerprint,
)
from core.config import Config  # noqa: E402
from core.customer_report import build_customer_report  # noqa: E402
from core.product_service import (  # noqa: E402
    ProductRequestBudget, ProductRunControl, run_customer_report_agent,
)


OUTPUT_DIR = ROOT / "validation" / "reasoning_agent_acceptance"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
USAGE_DB = OUTPUT_DIR / "authorised_usage.sqlite3"
SHANGHAI = timezone(timedelta(hours=8))
QUESTIONS = (
    "这些评价主要是在说哪些体验？请结合原文区分顾客认可之处和可改进的需求，给出具体建议。",
    "你刚才提到的包装和进度提醒，分别由哪些原文支持？给出可执行的小规模改进方案，说明哪些是建议、哪些是已知事实。",
)
RUBRIC = {
    "directness": "是否直接回答本轮问题，而非只重复整体情感比例",
    "source_faithfulness": "认可点与需求是否确实来自原文，是否避免将好评类别当作故障",
    "specificity": "建议是否有与原文相关的动作、执行方式及可观察指标",
    "uncertainty": "是否区分事实、假设和建议，是否说明样本小或缺失信息",
    "followup": "第二轮是否正确理解前文并用当前查询支持回答；单轮时记为不适用",
}


class AcceptanceBudget(ProductRequestBudget):
    """A fixed-scope ledger: dates never silently renew this acceptance budget."""

    def _day(self):
        return "reasoning-agent-v1.6.0-20261020-total"


def _fixture() -> dict:
    source = pd.DataFrame([
        {"门店": "东城店", "商品": "保温杯", "评价": "杯子保温效果不错，出门带着很方便，外盒也很结实。"},
        {"门店": "东城店", "商品": "保温杯", "评价": "物流准时，客服把清洗方法解释得很清楚。"},
        {"门店": "西湖店", "商品": "保温杯", "评价": "杯盖密封很好，放在包里没有漏水，包装也完整。"},
        {"门店": "西湖店", "商品": "保温杯", "评价": "希望能在发货后增加订单进度短信提醒，目前需要自己打开页面查询。"},
        {"门店": "西湖店", "商品": "保温杯", "评价": "外盒大小合适，如果可以少用一些塑料缓冲材料就更好了。"},
    ])
    labels = [
        ("positive", "product_quality", "low", "保温便携且外盒结实"),
        ("positive", "customer_service", "low", "物流准时且清洗讲解清楚"),
        ("positive", "product_quality", "low", "密封可靠且包装完整"),
        ("neutral", "features", "low", "建议增加订单进度短信提醒"),
        ("neutral", "product_quality", "low", "建议减少塑料缓冲包装"),
    ]
    results = pd.DataFrame([
        {"source_row": index + 2, "processing_status": "success", "sentiment": sentiment,
         "category": category, "priority": priority, "summary": summary, "latency_ms": 0.0}
        for index, (sentiment, category, priority, summary) in enumerate(labels)
    ])
    return build_customer_report(source, results, "评价", "门店")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checks(result, report: dict, attempts: int) -> dict:
    rows = set(report["detail"][report["detail_columns"]["source_row"]].tolist())
    cited = set(result.evidence_rows)
    checks = {
        "completed_with_model_prose": result.status == "completed" and result.answer_origin == "llm",
        "model_calls_match_ledger": result.model_calls == attempts and 1 <= attempts <= DEFAULT_MAX_MODEL_CALLS,
        "tool_limit_and_allowlist": result.tool_calls == len(result.tool_trace)
            and 2 <= result.tool_calls <= DEFAULT_MAX_TOOL_CALLS and set(result.tool_trace) <= set(TOOL_NAMES),
        "same_report_snapshot": result.report_fingerprint == report_fingerprint(report),
        "cited_rows_are_in_fixture": bool(cited) and cited <= rows,
        "raw_table_retrieval_used": bool({"search_reviews", "read_table_rows"} & set(result.tool_trace)),
        "answer_is_present": bool(result.conclusion.strip()),
        "answer_is_supported": result.answer_type == "supported",
    }
    return {"checks": checks, "passed": all(checks.values()),
            "scope": "Structure, transport ledger and source membership only; not semantic accuracy or human quality."}


def _offline_plan(report: dict, turns: int) -> dict:
    context = ReportAgentContext(report)
    overview = context.query("get_overview", {})
    fields = context.query("describe_table", {})
    positive = context.query("search_reviews", {"sentiment": "正面", "limit": 3})
    neutral = context.query("search_reviews", {"sentiment": "中性", "limit": 3})
    assert report["metrics"]["success_rows"] == 5
    assert report["metrics"]["positive_rows"] == 3
    assert report["metrics"]["neutral_rows"] == 2
    assert report["metrics"]["negative_rows"] == 0
    assert positive.get("evidence") and neutral.get("evidence")
    schema = json.dumps(AgentDecision.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return {
        "mode": "offline_preparation", "external_requests": 0,
        "fixture_kind": "hand_authored_synthetic_positive_and_neutral",
        "fixture_rows": 5, "fixture_expectations": {"positive": 3, "neutral": 2, "negative": 0},
        "planned_turns": turns, "max_model_requests_per_turn": DEFAULT_MAX_MODEL_CALLS,
        "max_total_requests_required_for_live": DEFAULT_MAX_MODEL_CALLS * turns,
        "max_local_queries_per_turn": DEFAULT_MAX_TOOL_CALLS,
        "questions": list(QUESTIONS[:turns]), "offline_tools_checked": [
            overview["tool"], fields["tool"], positive["tool"], neutral["tool"]],
        "decision_schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
        "human_review": {"status": "pending", "rubric": RUBRIC, "scores": None},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="1.6.0 Agent 小样本验收；默认离线，真实调用须显式授权并指定上限。")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=None,
                        help="真实总请求硬上限，一轮为3，两轮为6；失败尝试也计入")
    parser.add_argument("--turns", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    required = DEFAULT_MAX_MODEL_CALLS * args.turns
    if args.live and args.max_requests != required:
        parser.error(f"真实验收必须显式指定 --max-requests {required}，与 {args.turns} 轮的硬上限一致；未发送请求。")
    if not args.live and args.max_requests is not None:
        parser.error("--max-requests 仅用于 --live；默认离线无需额度。")

    report = _fixture()
    plan = _offline_plan(report, args.turns)
    if not args.live:
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if SUMMARY_PATH.exists():
        raise RuntimeError("已存在本版验收记录，禁止覆盖或自动重跑；请读取 validation/reasoning_agent_acceptance/summary.json。")
    if not Config.OPENAI_API_KEY:
        raise RuntimeError("真实验收需要配置接口凭据；未发送请求。")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    budget = AcceptanceBudget(USAGE_DB, daily_limit=args.max_requests)
    if budget.remaining() != args.max_requests:
        raise RuntimeError("本次独立累计额度已有消耗，禁止更换日期、重置账本或自动追加调用。")
    protected = [ROOT / "validation/agent_live_acceptance/summary.json",
                 ROOT / "validation/phase3_request_budget.json"]
    before = {path.as_posix(): _digest(path) for path in protected if path.exists()}
    plan.update(mode="live_acceptance", started_at=datetime.now(SHANGHAI).isoformat(),
                max_requests=args.max_requests, api_mode=Config.API_MODE,
                output_mode=Config.CHAT_OUTPUT_MODE if Config.API_MODE == "chat_completions" else "json_schema",
                agent_code_sha256=_digest(ROOT / "core/analysis_agent.py"))
    (OUTPUT_DIR / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results, turn_records = [], []
    interrupted = None
    for question in QUESTIONS[:args.turns]:
        if budget.remaining() < DEFAULT_MAX_MODEL_CALLS:
            break
        control = ProductRunControl(budget, DEFAULT_MAX_MODEL_CALLS)
        try:
            result = run_customer_report_agent(report, question, budget=budget, control=control, history=results)
            checks = _checks(result, report, control.attempts)
            turn_records.append({"question": question, "result": result.model_dump(mode="json"),
                                 "actual_attempts": control.attempts, "usage": control.usage,
                                 "automatic_checks": checks})
            results.append(result)
            if result.status != "completed":
                break
        except BaseException as error:
            # Retain usage and an error class even after transport interruption.
            # Never write exception messages, which may contain gateway details.
            turn_records.append({"question": question, "result": None,
                                 "actual_attempts": control.attempts, "usage": control.usage,
                                 "error_type": type(error).__name__,
                                 "automatic_checks": {"passed": False, "checks": {"answer_saved": False}}})
            if not isinstance(error, Exception):
                interrupted = error
            break
    unchanged = all(_digest(Path(path)) == digest for path, digest in before.items())
    summary = {
        **plan, "completed_at": datetime.now(SHANGHAI).isoformat(),
        "external_requests": args.max_requests - budget.remaining(),
        "actual_attempts": args.max_requests - budget.remaining(), "turns": turn_records,
        "all_planned_turns_completed": len(results) == args.turns
            and all(result.status == "completed" for result in results),
        "automatic_checks_passed": bool(turn_records) and len(turn_records) == args.turns
            and all(turn["automatic_checks"]["passed"] for turn in turn_records),
        "protected_history_unchanged": unchanged,
        "human_review": {"status": "pending", "reviewer": None, "reviewed_at": None,
                         "rubric": RUBRIC, "scores": None, "notes": None},
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(SUMMARY_PATH), "actual_attempts": summary["actual_attempts"],
                      "automatic_checks_passed": summary["automatic_checks_passed"],
                      "human_review": "pending", "protected_history_unchanged": unchanged}, ensure_ascii=False))
    if interrupted is not None:
        raise interrupted
    return 0 if summary["automatic_checks_passed"] and unchanged else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
