"""Portable, validated local evaluation artifacts; no credentials are recorded."""

import csv
from datetime import datetime
from io import StringIO
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any

from evaluation.data import COMPLETED_REVIEW_STATUSES, dataset_hash, validate_samples
from evaluation.metrics import LABELS, compute_metrics
from models.models import CustomerFeedbackAnalysis

DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[1] / "evaluation_reports"
REQUEST_SETTING_KEYS = {
    "provider", "model", "api_mode", "endpoint_sha256", "timeout_seconds",
    "max_output_tokens", "token_parameter", "reasoning_effort", "output_mode",
    "output_schema_sha256", "chat_json_instruction_sha256", "max_retries",
    "retry_backoff_seconds", "sdk_max_retries", "store",
}
PREDICTION_KEYS = {
    "sample_id", "text", "gold_sentiment", "gold_category", "gold_priority", "split", "review_status",
    "predicted_sentiment", "predicted_category", "predicted_priority", "summary", "status", "latency_ms",
    "error_message", "error_fields", "prediction_source",
}
CONTROL_PREDICTION_KEYS = {"request_attempts", "token_usage", "actionable_items", "failure_type"}
CONTROL_METADATA_KEYS = {
    "selection", "run_status", "stop_reason", "completed_sample_count", "not_executed_count",
    "request_attempts_actual", "request_attempts_hard_limit", "cumulative_request_attempts",
    "cumulative_request_limit", "journal_path",
}
STOP_REASONS = {None, "request_limit", "cumulative_budget", "authentication_error",
                "consecutive_service_errors", "interrupted", "configuration_error"}
USAGE_KEYS = {"input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
              "input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details"}
USAGE_DETAIL_KEYS = {"cached_tokens", "cache_creation_tokens", "cache_read_tokens", "audio_tokens",
                     "reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens",
                     "text_tokens", "image_tokens"}
AI_REVIEW_NOTICE = "本次标注包含 AI 辅助参考标签，并非全人工确认；指标不能作为业务准确率。"


def review_status_metadata(samples) -> dict:
    """Derive completion and provenance from rows, preserving all-human reports."""
    counts = {status: sum(sample.review_status == status for sample in samples)
              for status in ("draft", "reviewed")}
    ai_count = sum(sample.review_status == "ai_reviewed" for sample in samples)
    if ai_count:
        counts["ai_reviewed"] = ai_count
    complete = all(sample.review_status in COMPLETED_REVIEW_STATUSES for sample in samples)
    metadata = {
        "review_status_counts": counts,
        "all_labels_reviewed": complete,
        "result_status": ("ai_assisted_reference" if ai_count else "reviewed_reference") if complete else "provisional",
    }
    if ai_count:
        metadata["review_provenance"] = "ai_assisted"
    return metadata


def report_interpretation(mode: str, *, all_labels_reviewed: bool, ai_reviewed: bool = False,
                          incomplete: bool = False) -> str:
    """State execution limits and AI label provenance in every report mode."""
    interpretation = (
        "运行未完成：仅保存已执行样本证据，不构成完整数据集评测。" if incomplete else
        "DEMO 仅验证评测流程，预测及耗时均为 Mock；不代表模型效果或性能。" if mode == "demo" else
        "待人工复核草稿标签上的临时结果，不能作为模型准确率结论。" if not all_labels_reviewed else
        "仅反映本次数据集与请求设置下的结果；不代表真实业务总体效果。"
    )
    return interpretation + AI_REVIEW_NOTICE if ai_reviewed else interpretation


def _validate_control_fields(metadata, predictions, mode):
    selection = metadata["selection"]
    if not isinstance(selection, dict) or set(selection) != {"method", "requested_sample_ids", "limit", "stable_order"}:
        raise ValueError
    if selection["method"] not in {"all", "sample_ids", "limit"} or selection["stable_order"] != "sample_id":
        raise ValueError
    ids = selection["requested_sample_ids"]
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
        raise ValueError
    if selection["method"] == "sample_ids":
        if set(ids) != set(metadata["sample_ids"]) or selection["limit"] is not None:
            raise ValueError
    elif ids:
        raise ValueError
    if selection["method"] == "limit":
        if not _integer(selection["limit"], 1) or len(predictions) > selection["limit"]:
            raise ValueError
    elif selection["limit"] is not None:
        raise ValueError
    if metadata["sample_ids"] != sorted(metadata["sample_ids"]):
        raise ValueError
    pending = sum(row["status"] == "not_executed" for row in predictions)
    if metadata["run_status"] not in {"completed", "incomplete"} or metadata["stop_reason"] not in STOP_REASONS:
        raise ValueError
    if not _integer(metadata["completed_sample_count"]) or metadata["completed_sample_count"] != len(predictions) - pending:
        raise ValueError
    if not _integer(metadata["not_executed_count"]) or metadata["not_executed_count"] != pending:
        raise ValueError
    if metadata["run_status"] == "completed" and pending:
        raise ValueError
    if metadata["run_status"] == "incomplete" and metadata["stop_reason"] is None:
        raise ValueError
    if (metadata["run_status"] == "completed") != (pending == 0 and metadata["stop_reason"] is None):
        raise ValueError
    for key in ("request_attempts_actual", "request_attempts_hard_limit", "cumulative_request_attempts", "cumulative_request_limit"):
        if not _integer(metadata[key]):
            raise ValueError
    attempts = sum(row["request_attempts"] for row in predictions)
    if metadata["request_attempts_actual"] != attempts or attempts > metadata["request_attempts_hard_limit"] or attempts > metadata["request_attempts_upper_bound"]:
        raise ValueError
    if metadata["cumulative_request_limit"] != 180 or not attempts <= metadata["cumulative_request_attempts"] <= 180:
        raise ValueError
    if not (1 <= metadata["request_attempts_hard_limit"] <= 180 if mode == "live" else metadata["request_attempts_hard_limit"] == 0):
        raise ValueError
    if metadata["journal_path"] is not None and not isinstance(metadata["journal_path"], str):
        raise ValueError
    for row in predictions:
        if not _integer(row["request_attempts"]) or row["request_attempts"] > metadata["application_max_retries"] + 1:
            raise ValueError
        if row["failure_type"] not in {None, "authentication", "connection", "timeout", "service", "structure", "unknown"}:
            raise ValueError
        usage = row["token_usage"]
        if not isinstance(usage, list) or len(usage) != row["request_attempts"]:
            raise ValueError
        for entry in usage:
            if entry == "不可用":
                continue
            if not isinstance(entry, dict) or not entry or not set(entry) <= USAGE_KEYS:
                raise ValueError
            for key, value in entry.items():
                if key.endswith("_details"):
                    if not isinstance(value, dict) or not set(value) <= USAGE_DETAIL_KEYS or any(not _integer(v) for v in value.values()):
                        raise ValueError
                elif not _integer(value):
                    raise ValueError
        if row["status"] == "success":
            CustomerFeedbackAnalysis.model_validate({
                field: row[f"predicted_{field}"] for field in LABELS
            } | {"summary": row["summary"], "actionable_items": row["actionable_items"]})
            if row["failure_type"] is not None:
                raise ValueError
        elif row["actionable_items"] is not None:
            raise ValueError
        if row["status"] == "not_executed" and (row["request_attempts"] or row["failure_type"] is not None or row["latency_ms"] != 0):
            raise ValueError
        if mode == "demo" and row["request_attempts"]:
            raise ValueError


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _number(value: Any, minimum: float = 0.0, *, strictly_greater: bool = False) -> bool:
    return type(value) in {int, float} and isfinite(value) and (value > minimum if strictly_greater else value >= minimum)


def _hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_request_settings(settings: Any) -> None:
    if not isinstance(settings, dict) or set(settings) != REQUEST_SETTING_KEYS:
        raise ValueError
    if any(isinstance(value, (dict, list, tuple, set)) for value in settings.values()):
        raise ValueError
    if settings["provider"] not in {"openai", "openai_compatible", "openai-compatible"}:
        raise ValueError
    if not isinstance(settings["model"], str) or not settings["model"].strip():
        raise ValueError
    if settings["api_mode"] not in {"responses", "chat_completions"}:
        raise ValueError
    if not _hash(settings["endpoint_sha256"]) or not _hash(settings["output_schema_sha256"]):
        raise ValueError
    if not _number(settings["timeout_seconds"], strictly_greater=True) or not _integer(settings["max_output_tokens"], 1):
        raise ValueError
    if not _integer(settings["max_retries"]) or not _number(settings["retry_backoff_seconds"]):
        raise ValueError
    if type(settings["sdk_max_retries"]) is not int or settings["sdk_max_retries"] != 0 or settings["store"] is not False:
        raise ValueError
    if settings["reasoning_effort"] not in {None, "none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError
    if settings["api_mode"] == "responses":
        if settings["token_parameter"] != "max_output_tokens" or settings["output_mode"] != "json_schema" or settings["chat_json_instruction_sha256"] is not None:
            raise ValueError
    elif settings["token_parameter"] not in {"max_completion_tokens", "max_tokens"} or settings["output_mode"] not in {"json_schema", "json_object", "text"} or not _hash(settings["chat_json_instruction_sha256"]):
        raise ValueError


def validate_report(report: dict[str, Any]) -> dict:
    """Validate loaded/downloaded reports and reject mixed mock/live rows."""
    try:
        if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report.get("schema_version") not in {1, 2}:
            raise ValueError
        controlled = report["schema_version"] == 2
        if set(report) != {"schema_version", "metadata", "metrics", "predictions", "errors"}:
            raise ValueError
        metadata = report["metadata"]
        predictions = report["predictions"]
        if not isinstance(metadata, dict) or not isinstance(predictions, list) or not predictions:
            raise ValueError
        required_metadata = {
            "run_id", "started_at", "completed_at", "mode", "display_label", "model", "provider", "api_mode",
            "configured_model", "configured_api_mode", "prompt_version", "prompt_hash", "dataset_hash",
            "selected_samples_hash", "split", "sample_count", "sample_ids", "max_workers", "request_settings",
            "application_max_retries", "sdk_max_retries", "request_attempts_upper_bound", "review_status_counts",
            "all_labels_reviewed", "result_status", "interpretation", "wall_duration_ms",
        }
        if controlled:
            required_metadata |= CONTROL_METADATA_KEYS
        if set(metadata) not in (required_metadata, required_metadata | {"review_provenance"}):
            raise ValueError
        mode = metadata["mode"]
        if mode not in {"demo", "live"} or metadata["split"] not in {"dev", "test"}:
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{32}", metadata["run_id"]):
            raise ValueError
        for key in ("prompt_hash", "dataset_hash", "selected_samples_hash"):
            if not _hash(metadata[key]):
                raise ValueError
        dates = []
        for key in ("started_at", "completed_at"):
            parsed = datetime.fromisoformat(metadata[key])
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError
            dates.append(parsed)
        if dates[0] > dates[1] or not _number(metadata["wall_duration_ms"]):
            raise ValueError
        for key in ("prompt_version", "provider", "model", "api_mode", "configured_model", "configured_api_mode"):
            if not isinstance(metadata[key], str) or not metadata[key]:
                raise ValueError
        settings = metadata["request_settings"]
        _validate_request_settings(settings)
        # Ensure settings are JSON-safe, including disallowing NaN/Infinity.
        json.dumps(settings, allow_nan=False)
        if not _integer(metadata["sample_count"], 1) or metadata["sample_count"] != len(predictions):
            raise ValueError
        if not _integer(metadata["max_workers"], 1):
            raise ValueError
        if metadata["configured_model"] != settings["model"] or metadata["configured_api_mode"] != settings["api_mode"]:
            raise ValueError
        if not _integer(metadata["application_max_retries"]) or metadata["application_max_retries"] != (settings["max_retries"] if mode == "live" else 0):
            raise ValueError
        if not _integer(metadata["sdk_max_retries"]) or metadata["sdk_max_retries"] != settings["sdk_max_retries"]:
            raise ValueError
        upper_bound = len(predictions) * (metadata["application_max_retries"] + 1) if mode == "live" else 0
        if not _integer(metadata["request_attempts_upper_bound"]) or metadata["request_attempts_upper_bound"] != upper_bound:
            raise ValueError
        if mode == "live" and any(metadata[key] != settings[key] for key in ("provider", "model", "api_mode")):
            raise ValueError
        keys = PREDICTION_KEYS | CONTROL_PREDICTION_KEYS if controlled else PREDICTION_KEYS
        if any(not isinstance(row, dict) or set(row) != keys for row in predictions):
            raise ValueError
        if controlled:
            _validate_control_fields(metadata, predictions, mode)
        if metadata["sample_ids"] != [row["sample_id"] for row in predictions]:
            raise ValueError
        reconstructed = validate_samples([
            {key: row[key] for key in (
                "sample_id", "text", "gold_sentiment", "gold_category", "gold_priority", "split", "review_status",
            )} for row in predictions
        ])
        if any(item.split != metadata["split"] for item in reconstructed):
            raise ValueError
        if dataset_hash(reconstructed) != metadata["selected_samples_hash"]:
            raise ValueError
        if any(row["prediction_source"] != mode for row in predictions):
            raise ValueError
        if mode == "demo" and (metadata["model"] != "deterministic-mock-v1" or metadata["api_mode"] != "mock" or metadata["provider"] != "mock"):
            raise ValueError
        review_metadata = review_status_metadata(reconstructed)
        counts = review_metadata["review_status_counts"]
        if metadata["review_status_counts"] != counts or any(not _integer(value) for value in metadata["review_status_counts"].values()):
            raise ValueError
        reviewed = review_metadata["all_labels_reviewed"]
        has_ai_review = "ai_reviewed" in counts
        if ((has_ai_review and metadata.get("review_provenance") != "ai_assisted")
                or (not has_ai_review and "review_provenance" in metadata)):
            raise ValueError
        if type(metadata["all_labels_reviewed"]) is not bool or metadata["all_labels_reviewed"] != reviewed or metadata["result_status"] != review_metadata["result_status"]:
            raise ValueError
        if metadata["display_label"] != ("DEMO 合成离线演示" if mode == "demo" else "LIVE 真实模型调用"):
            raise ValueError
        expected_interpretation = report_interpretation(
            mode, all_labels_reviewed=reviewed, ai_reviewed=has_ai_review,
            incomplete=controlled and metadata["run_status"] == "incomplete",
        )
        if metadata["interpretation"] != expected_interpretation:
            raise ValueError
        from evaluation.runner import _error_fields
        for row in predictions:
            if row["error_fields"] != _error_fields(row):
                raise ValueError
            if row["status"] == "success":
                if row.get("error_message") is not None or not isinstance(row.get("summary"), str) or not row["summary"].strip():
                    raise ValueError
            elif row["status"] == "not_executed":
                if not controlled or row["error_message"] != "本次运行中止，尚未执行请求。" or row["summary"] is not None:
                    raise ValueError
            elif not isinstance(row.get("error_message"), str) or not row["error_message"] or row.get("summary") is not None:
                raise ValueError
            elif mode == "demo":
                expected_error = "DEMO：模拟调用失败。" if row["status"] == "failed" else "DEMO：模拟完整输出模型校验失败。"
                if row["error_message"] != expected_error:
                    raise ValueError
            elif row["status"] == "validation_error":
                if row["error_message"] != "结构化输出校验失败：响应不符合完整输出模型。":
                    raise ValueError
            elif row["error_message"] not in {
                "API 身份验证失败，请检查本地密钥配置。", "模型请求超时。",
                "无法连接模型服务，请检查网络和地址配置。", "模型调用失败；异常原文已隐藏。",
            } and not re.fullmatch(r"模型服务返回 HTTP [1-5][0-9]{2}。", row["error_message"]):
                raise ValueError
        if report["metrics"] != compute_metrics(predictions, mode=mode,
                                               run_complete=metadata["run_status"] == "completed" if controlled else None):
            raise ValueError
        if report["errors"] != [row for row in predictions if row["error_fields"]]:
            raise ValueError
        return report
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError("评测报告缺少有效追溯信息，存在不一致指标或混合运行模式。") from None


def _csv_safe(value: Any) -> str:
    """Keep text visible while preventing spreadsheet formula execution."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def predictions_csv(report: dict) -> bytes:
    report = validate_report(report)
    metadata = report["metadata"]
    columns = [
        "run_id", "mode", "prompt_version", "sample_id", "text", "split", "review_status",
        "gold_sentiment", "gold_category", "gold_priority",
        "predicted_sentiment", "predicted_category", "predicted_priority",
        "summary", "status", "latency_ms", "error_fields", "error_message", "prediction_source",
    ]
    if report["schema_version"] == 2:
        columns += ["actionable_items", "failure_type", "request_attempts", "token_usage"]
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for prediction in report["predictions"]:
        combined = {**prediction, "run_id": metadata["run_id"], "mode": metadata["mode"], "prompt_version": metadata["prompt_version"]}
        writer.writerow({key: _csv_safe(combined[key]) for key in columns})
    return stream.getvalue().encode("utf-8-sig")


def metrics_json(report: dict) -> bytes:
    report = validate_report(report)
    return json.dumps(
        {"schema_version": report["schema_version"], "metadata": report["metadata"], "metrics": report["metrics"]},
        ensure_ascii=False, indent=2, allow_nan=False,
    ).encode("utf-8")


def _md(value: Any) -> str:
    if value is None:
        return "无有效预测"
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def report_markdown(report: dict) -> str:
    report = validate_report(report)
    meta, metrics = report["metadata"], report["metrics"]
    mode = "DEMO" if meta["mode"] == "demo" else "LIVE"
    lines = [
        f"# {mode} 自动化评测报告", "", meta["interpretation"], "",
        "评测数据包含合成示例；标签复核状态见下表。DEMO 与真实响应不会混入同一次报告。", "",
        "draft 标签为待人工复核草稿；其指标为临时结果，不能作为模型准确率结论。", "",
        "## 追溯信息", "", "| 字段 | 值 |", "| --- | --- |",
    ]
    trace_keys = (
        "run_id", "started_at", "completed_at", "mode", "model", "provider", "api_mode",
        "configured_model", "configured_api_mode", "prompt_version", "prompt_hash",
        "dataset_hash", "selected_samples_hash", "split", "sample_count", "max_workers",
        "application_max_retries", "sdk_max_retries", "request_attempts_upper_bound",
        "review_status_counts", "all_labels_reviewed", "result_status",
    )
    if "review_provenance" in meta:
        trace_keys += ("review_provenance",)
    if report["schema_version"] == 2:
        trace_keys += tuple(sorted(CONTROL_METADATA_KEYS))
    lines.extend(f"| {key} | {_md(meta[key])} |" for key in trace_keys)
    lines.extend(["", "请求设置（地址仅存哈希，不含密钥）：", "", "```json", json.dumps(meta["request_settings"], ensure_ascii=False, indent=2), "```", "", "## 指标口径", ""])
    lines.extend(f"- {value}" for value in metrics["metric_policy"].values())
    lines.extend([
        "", "## 汇总", "", "| 项目 | 值 |", "| --- | --- |",
        f"| 已执行评测样本 | {metrics['sample_count']} |",
        f"| 结构化成功率 | {metrics['structured_success_rate']:.4f} |",
        f"| 失败数 | {metrics['failed_count']} |",
        f"| 调用失败 | {metrics['failures_by_status']['failed']} |",
        f"| 结构校验失败 | {metrics['failures_by_status']['validation_error']} |",
        f"| 平均耗时（ms） | {metrics['latency']['average_ms']:.3f} |",
        f"| P95 耗时（ms） | {metrics['latency']['p95_ms']:.3f} |", "",
        metrics["latency"]["measurement"], "",
        "P95 使用排序后的线性插值；平均/P95 均覆盖成功和失败样本。", "",
        "| 字段 | Accuracy | Macro-F1 |", "| --- | ---: | ---: |",
    ])
    if report["schema_version"] == 2:
        lines += [f"计划样本 {metrics['planned_sample_count']}；尚未执行 {metrics['not_executed_count']}。",
                  "token usage 逐次保存在 JSON／预测 CSV；缺失用量记录为“不可用”，未估算费用。", ""]
    for field in LABELS:
        result = metrics[field]
        lines.append(f"| {field} | {result['accuracy']:.4f} | {result['macro_f1']:.4f} |")
    for field in LABELS:
        result = metrics[field]
        lines.extend(["", f"## {field} 逐类指标", "", "| 类别 | precision | recall | F1 | support |", "| --- | ---: | ---: | ---: | ---: |"])
        for label, scores in result["per_class"].items():
            lines.append(f"| {label} | {scores['precision']:.4f} | {scores['recall']:.4f} | {scores['f1']:.4f} | {scores['support']} |")
        matrix = result["confusion_matrix"]
        lines.extend(["", "混淆矩阵（行 = gold，列 = 预测；__failure__ = 无有效预测）：", "", "| gold / prediction | " + " | ".join(matrix["prediction_labels"]) + " |", "| --- | " + " | ".join("---:" for _ in matrix["prediction_labels"]) + " |"])
        for label, values in zip(matrix["gold_labels"], matrix["values"]):
            lines.append(f"| {label} | " + " | ".join(str(value) for value in values) + " |")
    lines.extend(["", "## 可核验错误明细", "", "错误字段只描述 gold 与预测的差异或调用/校验失败，不推断模型错误根因。", "", "| ID | 原文 | gold（情感 / 类别 / 优先级） | 预测（情感 / 类别 / 优先级） | 错误字段 | 安全失败信息 |", "| --- | --- | --- | --- | --- | --- |"])
    for row in report["errors"]:
        gold = " / ".join(row[f"gold_{field}"] for field in LABELS)
        predicted = " / ".join(row[f"predicted_{field}"] or "无有效预测" for field in LABELS)
        lines.append("| " + " | ".join(_md(value) for value in (row["sample_id"], row["text"], gold, predicted, "、".join(row["error_fields"]), row["error_message"] or "")) + " |")
    if not report["errors"]:
        lines.append("| — | 无错误明细 | — | — | — | — |")
    pending = [row["sample_id"] for row in report["predictions"] if row["status"] == "not_executed"]
    if pending:
        lines += ["", "## 尚未执行", "", "这些记录没有模型预测，不计作请求失败：" + "、".join(pending)]
    return "\n".join(lines) + "\n"


def save_report(report: dict, output_dir: str | Path = DEFAULT_REPORT_DIR) -> dict[str, Path]:
    report = validate_report(report)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    meta = report["metadata"]
    stem = f"{meta['mode']}_{meta['split']}_{meta['prompt_version']}_{meta['run_id']}"
    # Prompt version is used as a filename only after a strict safe-character check.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", meta["prompt_version"]):
        raise ValueError("Prompt 版本不能用于安全的报告文件名。")
    paths = {
        "report": directory / f"{stem}.report.json",
        "metrics": directory / f"{stem}.metrics.json",
        "predictions": directory / f"{stem}.predictions.csv",
        "markdown": directory / f"{stem}.md",
    }
    payloads = {
        "report": json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"),
        "metrics": metrics_json(report), "predictions": predictions_csv(report),
        "markdown": report_markdown(report).encode("utf-8"),
    }
    for kind, path in paths.items():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payloads[kind])
        temporary.replace(path)
    return paths


def load_report(path: str | Path) -> dict:
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("无法读取有效的本地评测报告 JSON。") from None
    return validate_report(loaded)
