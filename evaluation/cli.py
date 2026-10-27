"""CLI defaults to deterministic DEMO; only --live permits model requests."""

import argparse
import json
from pathlib import Path
import sys

from core.config import Config
from ai.prompts import available_prompt_versions
from evaluation.control import RunControl
from evaluation.data import DEFAULT_DATASET, dataset_hash, load_dataset, select_split
from evaluation.reports import AI_REVIEW_NOTICE, DEFAULT_REPORT_DIR, load_report, review_status_metadata, save_report
from evaluation.runner import compare_reports, run_evaluation, select_samples
from evaluation.review import load_review_ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合成反馈评测：默认 DEMO 离线运行；真实调用需明确 --live。")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate", help="独立校验数据，不请求模型。")
    validate.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    for name in ("demo", "run", "compare"):
        command = subcommands.add_parser(name, help="两个 Prompt 对比。" if name == "compare" else "运行并导出评测。")
        command.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        command.add_argument("--split", choices=("dev", "test"), default="dev")
        command.add_argument("--workers", type=int, default=2)
        selection = command.add_mutually_exclusive_group()
        selection.add_argument("--sample-ids", nargs="+", help="按明确 ID 选择；结果稳定按 sample_id 排序。")
        selection.add_argument("--limit", type=int, help="只运行按 sample_id 排序后的前 N 条。")
        command.add_argument("--max-retries", type=int, default=0, help="仅覆盖本次应用重试次数，不改 .env；默认 0。")
        command.add_argument("--max-attempts", type=int, default=180, help="本次运行（包括比较中的所有版本）的共享请求硬上限；累计上限仍为 180。")
        command.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
        if name != "demo":
            command.add_argument("--live", action="store_true", help="明确授权本次真实接口评测；不添加该参数只生成 DEMO。")
        if name == "compare":
            command.add_argument("--reports", nargs="+", type=Path, help="比较已保存报告；不发起模型请求。")
        else:
            command.add_argument("--prompt-version", choices=available_prompt_versions(), default="v1")
    return parser


def _run_summary(report, paths) -> dict:
    return {
        "run_id": report["metadata"]["run_id"], "mode": report["metadata"]["mode"],
        "prompt_version": report["metadata"]["prompt_version"],
        "sample_count": report["metadata"]["sample_count"],
        "interpretation": report["metadata"]["interpretation"],
        "structured_success_rate": report["metrics"]["structured_success_rate"],
        "failed_count": report["metrics"]["failed_count"],
        "run_status": report["metadata"]["run_status"],
        "not_executed_count": report["metadata"]["not_executed_count"],
        "request_attempts_actual": report["metadata"]["request_attempts_actual"],
        "cumulative_request_attempts": report["metadata"]["cumulative_request_attempts"],
        "stop_reason": report["metadata"]["stop_reason"],
        "files": {kind: str(path.resolve()) for kind, path in paths.items()},
    }


def main(argv: list[str] | None = None) -> int:
    # Windows otherwise chooses the locale code page even when stdout is piped;
    # consistently emit UTF-8 JSON so redirected Chinese reports remain usable.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if sys.platform == "win32" and callable(reconfigure):
        reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "compare" and args.reports:
            if args.live:
                raise ValueError("--reports 只比较本地报告，请移除 --live。")
            result = compare_reports([load_report(path) for path in args.reports])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        samples = load_dataset(args.dataset)
        if args.command == "validate":
            review_metadata = review_status_metadata(samples)
            message = "数据校验通过；数据为合成示例，draft 标签仍待人工复核。"
            if review_metadata.get("review_provenance") == "ai_assisted":
                message = ("数据校验通过；数据为合成示例，仍有 draft 标签待复核。"
                           if review_metadata["review_status_counts"]["draft"] else
                           "数据校验通过；数据为合成示例，全部标签已标记为完成复核。")
                message += AI_REVIEW_NOTICE
            summary = {
                "sample_count": len(samples), "dataset_hash": dataset_hash(samples),
                "split_counts": {split: len(select_split(samples, split)) for split in ("dev", "test")},
                "review_status_counts": review_metadata["review_status_counts"],
                "message": message,
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        mode = "live" if getattr(args, "live", False) else "demo"
        if mode == "live" and args.split == "test":
            raise ValueError("测试集仅用于冻结后的最终评估，请先冻结清单，再使用 evaluation.workflow_cli test-final。")
        versions = ["v1", "v2"] if args.command == "compare" else [args.prompt_version]
        selected_count = len(select_samples(samples, args.split, sample_ids=args.sample_ids, limit=args.limit))
        control = RunControl(args.max_attempts) if mode == "live" else None
        if mode == "live":
            print(
                f"LIVE：{selected_count} 条样本 × {len(versions)} 个版本；"
                f"理论 {selected_count * len(versions) * (args.max_retries + 1)} 次尝试；"
                f"本次共享硬上限 {args.max_attempts}，本阶段累计硬上限 180（包括所有重试，SDK 重试为 0）。"
            )
        reports = []
        summaries = []
        for version in versions:
            report = run_evaluation(samples, split=args.split, prompt_version=version, mode=mode, max_workers=args.workers,
                                    sample_ids=args.sample_ids, limit=args.limit, max_retries=args.max_retries,
                                    max_attempts=args.max_attempts, control=control, journal_dir=args.output_dir / "journals",
                                    review_ledger=load_review_ledger(args.dataset) if mode == "live" else None)
            paths = save_report(report, args.output_dir)
            reports.append(report)
            summaries.append(_run_summary(report, paths))
        result = {"runs": summaries}
        if args.command == "compare":
            if all(report["metadata"]["run_status"] == "completed" for report in reports):
                result["comparison"] = compare_reports(reports)
            else:
                result["comparison"] = {"compatible": False, "message": "运行未完成；已保存逐条证据，不能比较完整数据集效果。"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(report["metadata"]["run_status"] == "completed" for report in reports) else 2
    except ValueError as error:
        # Values raised by local evaluation validation use fixed safe messages.
        print(f"评测未完成：{error}")
        return 2
    except Exception:
        print("评测未完成：运行异常，原始错误已隐藏，请检查本地配置或报告路径。")
        return 2
