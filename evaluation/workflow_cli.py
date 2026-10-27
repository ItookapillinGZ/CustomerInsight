"""Human-gated final experiments. All commands are local except test-final --live."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from ai.client import request_settings
from core.config import Config
from evaluation.control import RunControl
from evaluation.data import DEFAULT_DATASET, load_dataset, normalize_text
from evaluation.experiment import (
    compare_development_reports, freeze_experiment_manifest, save_development_comparison,
    save_test_summary, summarize_test_reports, validate_experiment_manifest,
)
from evaluation.reports import DEFAULT_REPORT_DIR, load_report, save_report
from evaluation.review import (
    freeze_reviewed_dataset, initialize_review, review_checklist_csv, review_checklist_json,
    validate_review_evidence,
)
from evaluation.runner import run_evaluation


DEFAULT_TEST_EXECUTION_DIR = DEFAULT_REPORT_DIR / "test_executions"


def _write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _test_exposure_key(samples):
    """Changing labels, IDs, or manifest paths does not create an unseen test.

    Exact normalized feedback sets are tracked locally. Semantic near-duplicate
    detection still requires human judgment when creating a future dataset.
    """
    texts = sorted(normalize_text(sample.text) for sample in samples if sample.split == "test")
    return sha256(json.dumps(texts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reserve(path, evidence, message):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(message) from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())


def _persist_evidence(paths, evidence):
    failed = False
    # Try each copy independently: a failed report-directory write must not
    # prevent retaining the project-wide exposure evidence, or vice versa.
    for path in paths:
        try:
            _write(path, evidence)
        except OSError:
            failed = True
    if failed:
        raise OSError("Unable to update every local test execution evidence copy.")


def run_final_test(manifest_path, *, dataset_path=DEFAULT_DATASET, review_path=None,
                   output_dir=DEFAULT_REPORT_DIR):
    settings = request_settings()
    settings["max_retries"] = 0
    runtime = {
        "request_settings": settings, "model": Config.OPENAI_MODEL,
        "provider": Config.LLM_PROVIDER, "api_mode": Config.API_MODE,
        "configured_model": Config.OPENAI_MODEL, "configured_api_mode": Config.API_MODE,
        "max_workers": 2, "application_max_retries": 0, "sdk_max_retries": 0,
    }
    manifest = validate_experiment_manifest(manifest_path, dataset_path=dataset_path,
                                           review_path=review_path, request_parameters=runtime)
    samples = load_dataset(dataset_path)
    source = Path(manifest_path)
    execution = source.with_suffix(".execution.json")
    exposure_key = _test_exposure_key(samples)
    DEFAULT_TEST_EXECUTION_DIR.mkdir(parents=True, exist_ok=True)
    exposure = DEFAULT_TEST_EXECUTION_DIR / f"{exposure_key}.execution.json"
    # Reserve the single test pass before submitting anything. Even an interrupted
    # pass remains consumed. The project-wide marker also prevents a new manifest
    # or changed gold/dev hash from reusing these same exposed test feedbacks.
    evidence = {"manifest_sha256": manifest["manifest_sha256"],
                "dataset_hash": manifest["dataset"]["dataset_hash"],
                "test_exposure_key": exposure_key,
                "manifest_path": str(source.resolve()),
                "execution_path": str(execution.resolve()),
                "exposure_path": str(exposure.resolve()),
                "started_at": datetime.now(timezone.utc).isoformat(), "status": "started",
                "planned_versions": manifest["test_plan"]["versions"], "runs": []}
    _reserve(exposure, evidence, "相同测试反馈已经启动过单次最终评估；更换清单或标签不能重置测试集暴露状态，请检查原执行证据。")
    evidence_paths = [exposure]
    reports = []
    try:
        _reserve(execution, evidence, "此冻结清单已经启动过测试集单次评估；请检查原执行记录，不能重复测试。")
        evidence_paths.append(execution)
        control = RunControl(manifest["test_plan"]["max_request_attempts"])
        for version in manifest["test_plan"]["versions"]:
            evidence["active_version"] = version
            _persist_evidence(evidence_paths, evidence)
            report = run_evaluation(samples, split="test", prompt_version=version,
                                    mode="live", max_workers=2, max_retries=0,
                                    control=control, journal_dir=Path(output_dir) / "journals",
                                    experiment_manifest=manifest)
            reports.append(report)
            evidence["runs"].append({"version": version, "run_id": report["metadata"]["run_id"],
                                      "run_status": report["metadata"]["run_status"],
                                      "request_attempts_actual": report["metadata"].get("request_attempts_actual"),
                                      "cumulative_request_attempts": report["metadata"].get("cumulative_request_attempts"),
                                      "journal_path": report["metadata"].get("journal_path"), "files": {}})
            # Preserve the completed run identity and journal before report
            # export, since exporting itself can fail after requests were sent.
            _persist_evidence(evidence_paths, evidence)
            paths = save_report(report, output_dir)
            evidence["runs"][-1]["files"] = {key: str(path.resolve()) for key, path in paths.items()}
            _persist_evidence(evidence_paths, evidence)
            if report["metadata"]["run_status"] != "completed" or control.stopped:
                break
        if len(reports) == len(manifest["test_plan"]["versions"]) and all(row["metadata"]["run_status"] == "completed" for row in reports):
            summary = summarize_test_reports(reports, manifest, dataset_path=dataset_path, review_path=review_path)
            summary_path = Path(output_dir) / f"final_test_{manifest['manifest_sha256'][:16]}.json"
            evidence["summary_files"] = {key: str(path.resolve()) for key, path in save_test_summary(summary, summary_path).items()}
            evidence["status"] = "completed"
        else:
            evidence["status"] = "incomplete"
    except BaseException:
        evidence["status"] = "incomplete"
        evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
        evidence["interpretation"] = "流程中止；已占用的单次测试评估不重置，已完成结果和逐条日志保留，不能视为完整测试结果。"
        try:
            _persist_evidence(evidence_paths, evidence)
        except OSError:
            # At least the exclusive started marker survives. Never clear it
            # automatically or mask the original interruption/export error.
            pass
        raise
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    evidence["interpretation"] = (
        "已完成冻结后的单次测试；不使用测试结果继续修改 Prompt。" if evidence["status"] == "completed" else
        "测试运行未完成；不能把已保存的部分结果当作完整测试集评估。"
    )
    _persist_evidence(evidence_paths, evidence)
    return evidence


def main(argv=None):
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="第三阶段人工复核与冻结实验流程。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("review-init", "freeze-dataset", "dev-analysis", "freeze-experiment", "test-final"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        command.add_argument("--review", type=Path)
        command.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR / "phase3")
        if name in {"dev-analysis", "freeze-experiment"}:
            command.add_argument("--reports", nargs="+", type=Path, required=True)
        if name == "test-final":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if args.command == "review-init":
            ledger = initialize_review(args.dataset, args.review)
            checklist = args.output_dir / "human_review_checklist.csv"
            checklist.write_bytes(review_checklist_csv(ledger))
            (args.output_dir / "human_review_history.json").write_bytes(review_checklist_json(ledger))
            result = {"reviewed": sum(row["review_status"] == "reviewed" for row in ledger["records"]),
                      "draft": sum(row["review_status"] == "draft" for row in ledger["records"]),
                      "checklist": str(checklist.resolve())}
        elif args.command == "freeze-dataset":
            result = freeze_reviewed_dataset(args.dataset, args.review, output_dir=args.output_dir / "frozen")
        elif args.command in {"dev-analysis", "freeze-experiment"}:
            validate_review_evidence(args.dataset, args.review, require_all_reviewed=True)
            reports = [load_report(path) for path in args.reports]
            if args.command == "dev-analysis":
                comparison = compare_development_reports(reports, load_dataset(args.dataset))
                result = {key: str(path.resolve()) for key, path in save_development_comparison(comparison, args.output_dir / "dev_comparison.json").items()}
            else:
                result = freeze_experiment_manifest(reports, dataset_path=args.dataset, review_path=args.review,
                                                    output_path=args.output_dir / "experiment_manifest.json")
        else:
            result = run_final_test(args.manifest, dataset_path=args.dataset, review_path=args.review, output_dir=args.output_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "incomplete" else 0
    except ValueError as error:
        print(f"流程未完成：{error}")
        return 2
    except Exception:
        print("流程未完成：本地保存或运行异常；请检查已保存证据，异常原文已隐藏。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
