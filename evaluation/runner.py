"""Evaluation keeps gold labels local and passes only feedback text to the model."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import uuid4
import time
import threading

from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError
from pydantic import ValidationError

from ai.analyzer import StructuredOutputError, analyze_feedback
from ai.client import get_openai_client, request_settings
from ai.prompts import get_prompt, prompt_hash
from core.config import Config
from evaluation.data import COMPLETED_REVIEW_STATUSES, dataset_hash, select_split, validate_samples
from evaluation.control import RequestControlStopped, RunControl, RunJournal
from evaluation.metrics import LABELS, compute_metrics
from models.models import CustomerFeedbackAnalysis


def _safe_error(error: Exception) -> str:
    """Never include exception bodies, arbitrary class names, URLs, or SDK payloads."""
    if isinstance(error, (ValidationError, StructuredOutputError)):
        return "结构化输出校验失败：响应不符合完整输出模型。"
    if isinstance(error, AuthenticationError):
        return "API 身份验证失败，请检查本地密钥配置。"
    if isinstance(error, APITimeoutError):
        return "模型请求超时。"
    if isinstance(error, APIConnectionError):
        return "无法连接模型服务，请检查网络和地址配置。"
    if isinstance(error, APIStatusError):
        return f"模型服务返回 HTTP {int(error.status_code)}。"
    return "模型调用失败；异常原文已隐藏。"


def _base_prediction(sample) -> dict[str, Any]:
    return {
        "sample_id": sample.sample_id,
        "text": sample.text,
        "gold_sentiment": sample.gold_sentiment.value,
        "gold_category": sample.gold_category.value,
        "gold_priority": sample.gold_priority.value,
        "split": sample.split,
        "review_status": sample.review_status,
        "predicted_sentiment": None,
        "predicted_category": None,
        "predicted_priority": None,
        "summary": None,
        "status": "failed",
        "latency_ms": 0.0,
        "error_message": None,
        "error_fields": [],
        "actionable_items": None,
        "request_attempts": 0,
        "token_usage": [],
        "failure_type": None,
    }


def _error_fields(row: dict) -> list[str]:
    if row["status"] == "not_executed":
        return []
    if row["status"] == "failed":
        return ["调用失败"]
    if row["status"] == "validation_error":
        return ["结构校验失败"]
    names = {"sentiment": "情感错误", "category": "类别错误", "priority": "优先级错误"}
    return [name for field, name in names.items() if row[f"gold_{field}"] != row[f"predicted_{field}"]]


def _failure_type(error: Exception) -> str:
    if isinstance(error, (ValidationError, StructuredOutputError)):
        return "structure"
    if isinstance(error, APIStatusError) and error.status_code in {401, 403}:
        return "authentication"
    if isinstance(error, APITimeoutError):
        return "timeout"
    if isinstance(error, APIConnectionError):
        return "connection"
    if isinstance(error, APIStatusError):
        return "service"
    return "unknown"


def _not_executed(sample) -> dict:
    row = _base_prediction(sample)
    row.update(status="not_executed", error_message="本次运行中止，尚未执行请求。", prediction_source="live")
    return row


def _live_prediction(sample, client, prompt_version: str, *, max_retries: int, control: RunControl, run_id: str) -> dict:
    row = _base_prediction(sample)
    trace = control.trace(run_id, sample.sample_id, prompt_version)
    started = time.perf_counter()
    try:
        # Gold, split, status and identifiers do not enter the model boundary.
        analysis = analyze_feedback(sample.text, client=client, prompt_version=prompt_version,
                                    max_retries=max_retries, request_control=trace)
        raw = analysis.model_dump(mode="python", warnings=False) if isinstance(analysis, CustomerFeedbackAnalysis) else analysis
        analysis = CustomerFeedbackAnalysis.model_validate(raw)
        row.update(
            predicted_sentiment=analysis.sentiment.value,
            predicted_category=analysis.category.value,
            predicted_priority=analysis.priority.value,
            summary=analysis.summary,
            actionable_items=[item.model_dump(mode="json") for item in analysis.actionable_items],
            status="success",
        )
    except RequestControlStopped:
        return _not_executed(sample)
    except Exception as error:
        row.update(
            status="validation_error" if isinstance(error, (ValidationError, StructuredOutputError)) else "failed",
            error_message=_safe_error(error),
            failure_type=_failure_type(error),
        )
        # Also observe exceptions from injected analyzers; normal SDK errors
        # are already observed at the transport boundary and must not double count.
        if trace.attempts == 0:
            control.request_failed(error)
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    row["error_fields"] = _error_fields(row)
    row["prediction_source"] = "live"
    row["request_attempts"] = trace.attempts
    row["token_usage"] = trace.token_usage
    return row


def _mock_prediction(sample, prompt_version: str, forced_status: str | None) -> dict:
    """A reproducible, deliberately non-semantic mock independent of gold labels."""
    row = _base_prediction(sample)
    digest = sha256(f"mock-v1|{prompt_version}|{sample.sample_id}|{sample.text}".encode("utf-8")).digest()
    row["latency_ms"] = float(10 + digest[5] % 80)
    status = forced_status or ("failed" if digest[6] % 19 == 0 else "success")
    if status == "success":
        analysis = CustomerFeedbackAnalysis.model_validate({
            "sentiment": LABELS["sentiment"][digest[0] % len(LABELS["sentiment"])],
            "category": LABELS["category"][digest[1] % len(LABELS["category"])],
            "priority": LABELS["priority"][digest[2] % len(LABELS["priority"])],
            "summary": "DEMO：按反馈文本和 ID 的哈希生成确定性预测。",
            "actionable_items": [],
        })
        row.update(
            predicted_sentiment=analysis.sentiment.value,
            predicted_category=analysis.category.value,
            predicted_priority=analysis.priority.value,
            summary=analysis.summary,
            actionable_items=[],
        )
    else:
        row["error_message"] = (
            "DEMO：模拟调用失败。" if status == "failed" else "DEMO：模拟完整输出模型校验失败。"
        )
    row["status"] = status
    row["failure_type"] = None if status == "success" else "structure" if status == "validation_error" else "unknown"
    row["error_fields"] = _error_fields(row)
    row["prediction_source"] = "demo"
    return row


def select_samples(samples, split: str, *, sample_ids=None, limit: int | None = None):
    """Select deterministically by preserved ID, independent of CSV row order."""
    selected = sorted(select_split(samples, split), key=lambda sample: sample.sample_id)
    if sample_ids is not None and limit is not None:
        raise ValueError("按 sample_id 选择与限制样本数不能同时使用。")
    if sample_ids is not None:
        if (not isinstance(sample_ids, (list, tuple)) or not sample_ids
                or any(not isinstance(value, str) or not value for value in sample_ids)
                or len(set(sample_ids)) != len(sample_ids)):
            raise ValueError("sample_ids 必须是非空且无重复的 ID 列表。")
        requested = set(sample_ids)
        if not requested.issubset({sample.sample_id for sample in selected}):
            raise ValueError("所选 sample_id 不存在或不属于指定 split。")
        selected = [sample for sample in selected if sample.sample_id in requested]
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("样本数限制必须是正整数。")
        selected = selected[:limit]
    return selected


def run_evaluation(
    samples, *, split: str = "dev", prompt_version: str = "v1",
    mode: str = "demo", max_workers: int = 2, client=None,
    sample_ids=None, limit: int | None = None, max_retries: int = 0,
    max_attempts: int = 180, control: RunControl | None = None, journal_dir=None,
    review_ledger: dict | None = None, experiment_manifest: dict | None = None,
) -> dict:
    """Run explicitly selected mode once; no fallback between live and mock."""
    get_prompt(prompt_version)  # Reject unknown versions before constructing a client.
    if mode not in {"demo", "live"}:
        raise ValueError("评测模式必须为 demo 或 live。")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= Config.MAX_WORKERS:
        raise ValueError(f"并发数必须是 1 到 {Config.MAX_WORKERS} 之间的整数。")
    all_samples = validate_samples(samples)
    if type(max_retries) is not int or max_retries < 0:
        raise ValueError("重试次数必须是非负整数。")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 180:
        raise ValueError("本次请求硬上限必须是 1 到 180 之间的整数。")
    selected = select_samples(all_samples, split, sample_ids=sample_ids, limit=limit)
    if not selected:
        raise ValueError("所选 split 没有有效评测样本。")
    if mode == "live" and split == "dev" and len(selected) > 5:
        from evaluation.review import ANNOTATION_GUIDE, validate_review_ledger
        if review_ledger is None:
            raise ValueError("完整开发集真实实验须先完成全部 60 条标签复核并提供独立复核记录；草稿试跑最多 5 条。")
        evidence = validate_review_ledger(review_ledger, all_samples)
        expected = {f"dev-{i:03d}" for i in range(1, 41)} | {f"test-{i:03d}" for i in range(1, 21)}
        if {row.sample_id for row in all_samples} != expected or any(row.review_status not in COMPLETED_REVIEW_STATUSES for row in all_samples):
            raise ValueError("全部 60 条标签尚未完成复核，不能进行完整开发集实验。")
        if any(record["review_status"] not in COMPLETED_REVIEW_STATUSES for record in evidence["records"]):
            raise ValueError("复核证据尚未完成，不能进行完整开发集实验。")
        if any(row.split != row.sample_id.split("-", 1)[0] for row in all_samples):
            raise ValueError("固定的 40/20 划分已变化，不能进行本次实验。")
        if evidence["annotation_guide_sha256"] != sha256(ANNOTATION_GUIDE.read_bytes()).hexdigest():
            raise ValueError("标注规则已变化，请先核对全部复核记录。")
    if mode == "live" and split == "test":
        if experiment_manifest is None:
            raise ValueError("测试集真实运行须先冻结实验清单，请使用 evaluation.workflow_cli test-final。")
        if sample_ids is not None or limit is not None or max_retries != 0 or max_workers != 2:
            raise ValueError("最终测试必须完整 20 条、并发 2、应用重试 0；不得抽样探测。")
        from evaluation.experiment import validate_experiment_manifest
        snapshot = experiment_manifest.get("dataset", {})
        runtime = {"request_settings": {**request_settings(), "max_retries": max_retries},
                   "model": Config.OPENAI_MODEL, "provider": Config.LLM_PROVIDER,
                   "api_mode": Config.API_MODE, "configured_model": Config.OPENAI_MODEL,
                   "configured_api_mode": Config.API_MODE, "max_workers": max_workers,
                   "application_max_retries": max_retries, "sdk_max_retries": 0}
        frozen = validate_experiment_manifest(experiment_manifest, dataset_path=snapshot.get("dataset_path", ""),
                                              review_path=snapshot.get("review_path"), request_parameters=runtime)
        if dataset_hash(all_samples) != frozen["dataset"]["dataset_hash"] or prompt_version not in frozen["test_plan"]["versions"]:
            raise ValueError("测试数据或版本不属于冻结实验清单。")
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = uuid4().hex
    selection = {"method": "sample_ids" if sample_ids is not None else "limit" if limit is not None else "all",
                 "requested_sample_ids": sorted(sample_ids) if sample_ids is not None else [],
                 "limit": limit, "stable_order": "sample_id"}
    journal = None
    wall_started = time.perf_counter()
    if mode == "demo":
        # Pick failure cases using IDs/text only; labels never influence mock predictions.
        ranked = sorted(selected, key=lambda item: sha256(f"{item.sample_id}|{item.text}".encode("utf-8")).hexdigest())
        forced = {ranked[0].sample_id: "failed"}
        if len(ranked) > 1:
            forced[ranked[1].sample_id] = "validation_error"
        predictions = [_mock_prediction(item, prompt_version, forced.get(item.sample_id)) for item in selected]
    else:
        control = control if control is not None else RunControl(max_attempts)
        # Validate the durable budget before constructing a network client.
        control.snapshot(run_id)
        if journal_dir is None:
            from evaluation.reports import DEFAULT_REPORT_DIR
            journal_dir = DEFAULT_REPORT_DIR / "journals"
        journal = RunJournal(journal_dir, run_id, {
            "run_id": run_id, "mode": mode, "split": split, "prompt_version": prompt_version,
            "prompt_hash": prompt_hash(prompt_version), "dataset_hash": dataset_hash(all_samples),
            "selected_samples_hash": dataset_hash(selected), "sample_ids": [sample.sample_id for sample in selected],
            "selection": selection, "request_settings": {**request_settings(), "max_retries": max_retries},
            "max_workers": max_workers, "request_attempts_hard_limit": control.max_attempts,
        })
        owns_client = client is None
        active_client = None
        rows_by_id = {}
        rows_lock = threading.Lock()

        def persist_row(row):
            while True:
                try:
                    journal.append(row)
                    return
                except KeyboardInterrupt:
                    control.stop("interrupted")

        def execute_and_record(sample):
            # Workers retain and persist their own evidence. If Ctrl+C occurs
            # just after executor.submit queued work, the caller may never get
            # that Future; shutdown still waits for its recorded row.
            row = _live_prediction(sample, active_client, prompt_version,
                                   max_retries=max_retries, control=control, run_id=run_id)
            with rows_lock:
                rows_by_id[sample.sample_id] = row
            persist_row(row)
            return row
        try:
            if not control.stopped:
                try:
                    active_client = get_openai_client() if owns_client else client
                except ValueError:
                    control.stop("configuration_error")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                next_index = 0
                pending = {}

                def submit_available():
                    nonlocal next_index
                    while (not control.stopped and next_index < len(selected)
                           and len(pending) < max_workers):
                        sample = selected[next_index]
                        next_index += 1
                        future = executor.submit(execute_and_record, sample)
                        pending[future] = sample

                while True:
                    try:
                        submit_available()
                        if not pending:
                            break
                        completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in completed:
                            future.result()
                            pending.pop(future)
                    except KeyboardInterrupt:
                        control.stop("interrupted")
                        continue
        finally:
            if owns_client and active_client is not None:
                active_client.close()
        for sample in selected:
            if sample.sample_id not in rows_by_id:
                row = _not_executed(sample)
                rows_by_id[sample.sample_id] = row
                persist_row(row)
        predictions = [rows_by_id[sample.sample_id] for sample in selected]
    from evaluation.reports import report_interpretation, review_status_metadata
    review_metadata = review_status_metadata(selected)
    all_reviewed = review_metadata["all_labels_reviewed"]
    has_ai_review = review_metadata.get("review_provenance") == "ai_assisted"
    settings = request_settings()
    settings["max_retries"] = max_retries if mode == "live" else settings["max_retries"]
    not_executed_count = sum(row["status"] == "not_executed" for row in predictions)
    run_status = "incomplete" if not_executed_count or (mode == "live" and control.stop_reason) else "completed"
    metadata = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "display_label": "DEMO 合成离线演示" if mode == "demo" else "LIVE 真实模型调用",
        "provider": "mock" if mode == "demo" else Config.LLM_PROVIDER,
        "model": "deterministic-mock-v1" if mode == "demo" else Config.OPENAI_MODEL,
        "api_mode": "mock" if mode == "demo" else Config.API_MODE,
        "configured_model": Config.OPENAI_MODEL,
        "configured_api_mode": Config.API_MODE,
        "request_settings": settings,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash(prompt_version),
        "dataset_hash": dataset_hash(all_samples),
        "selected_samples_hash": dataset_hash(selected),
        "sample_ids": [item.sample_id for item in selected],
        "sample_count": len(selected),
        "split": split,
        "max_workers": max_workers,
        "application_max_retries": max_retries if mode == "live" else 0,
        "sdk_max_retries": 0,
        "request_attempts_upper_bound": len(selected) * (max_retries + 1) if mode == "live" else 0,
        **review_metadata,
        "interpretation": report_interpretation(
            mode, all_labels_reviewed=all_reviewed, ai_reviewed=has_ai_review,
            incomplete=run_status == "incomplete",
        ),
        "wall_duration_ms": round((time.perf_counter() - wall_started) * 1000, 3),
        "selection": selection,
        "run_status": run_status,
        "completed_sample_count": len(selected) - not_executed_count,
        "not_executed_count": not_executed_count,
        "journal_path": str(journal.path.resolve()) if journal else None,
    }
    metadata.update(control.snapshot(run_id) if mode == "live" else {
        "request_attempts_actual": 0, "request_attempts_hard_limit": 0,
        "cumulative_request_attempts": 0, "cumulative_request_limit": 180, "stop_reason": None,
    })
    if journal:
        while True:
            try:
                journal.finish(metadata)
                break
            except KeyboardInterrupt:
                control.stop("interrupted")
                metadata.update(control.snapshot(run_id))
                metadata.update(run_status="incomplete", interpretation=report_interpretation(
                    mode, all_labels_reviewed=all_reviewed, ai_reviewed=has_ai_review, incomplete=True,
                ))
    return {
        "schema_version": 2,
        "metadata": metadata,
        "metrics": compute_metrics(predictions, mode=mode, run_complete=metadata["run_status"] == "completed"),
        "predictions": predictions,
        "errors": [row.copy() for row in predictions if row["error_fields"]],
    }


def compare_reports(reports: list[dict]) -> dict:
    """Reject confounded comparisons; different prompts are the intended variable."""
    if len(reports) < 2:
        raise ValueError("版本比较至少需要两份报告。")
    # Delayed import avoids a reports/runner cycle.
    from evaluation.reports import AI_REVIEW_NOTICE, validate_report
    checked = [validate_report(report) for report in reports]
    if any(report["metadata"].get("run_status", "completed") != "completed" for report in checked):
        raise ValueError("运行未完成的报告不能参与完整数据集版本比较。")
    first = checked[0]["metadata"]
    required_equal = (
        "dataset_hash", "selected_samples_hash", "split", "model", "provider", "api_mode",
        "configured_model", "configured_api_mode", "request_settings", "mode", "sample_count",
        "max_workers", "application_max_retries", "sdk_max_retries",
    )
    for report in checked[1:]:
        metadata = report["metadata"]
        if any(metadata.get(key) != first.get(key) for key in required_equal):
            raise ValueError("报告的数据集、split、样本、模型、接口、请求设置、并发或运行模式不一致，不能比较。")
        if set(metadata["sample_ids"]) != set(first["sample_ids"]):
            raise ValueError("报告的评测样本不一致，不能比较。")
    versions = [report["metadata"]["prompt_version"] for report in checked]
    if len(set(versions)) != len(versions):
        raise ValueError("版本比较需要不同的 Prompt 版本。")
    rows = []
    for report in checked:
        metadata, metrics = report["metadata"], report["metrics"]
        row = {
            "run_id": metadata["run_id"], "prompt_version": metadata["prompt_version"],
            "mode": metadata["mode"], "sample_count": metrics["sample_count"],
            "structured_success_rate": metrics["structured_success_rate"],
            "failed_count": metrics["failed_count"],
            "average_latency_ms": metrics["latency"]["average_ms"],
            "p95_latency_ms": metrics["latency"]["p95_ms"],
        }
        for field in LABELS:
            row[f"{field}_accuracy"] = metrics[field]["accuracy"]
            row[f"{field}_macro_f1"] = metrics[field]["macro_f1"]
        rows.append(row)
    comparison = {
        "compatible": True, "mode": first["mode"], "split": first["split"],
        "dataset_hash": first["dataset_hash"], "rows": rows,
        "interpretation": (
            "DEMO 对比仅展示流程，不代表 Prompt 效果。" if first["mode"] == "demo" else
            "临时评测对比；待复核标签不能支持模型准确率结论。"
            if any(not report["metadata"]["all_labels_reviewed"] for report in checked) else
            "同一数据集及请求设置下的 Prompt 版本比较。"
        ),
    }
    if first.get("review_provenance") == "ai_assisted":
        comparison["review_provenance"] = "ai_assisted"
        comparison["interpretation"] += AI_REVIEW_NOTICE
    return comparison
