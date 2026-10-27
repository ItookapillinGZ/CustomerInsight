"""Small, explicit classification metrics with failed requests in the denominator."""

from collections import Counter
from math import isfinite
from typing import Any

from models.enums import ActionItemPriorityType, SentimentType, TopicType

LABELS = {
    "sentiment": [item.value for item in SentimentType],
    "category": [item.value for item in TopicType],
    "priority": [item.value for item in ActionItemPriorityType],
}
FAILURE_LABEL = "__failure__"
STATUSES = {"success", "failed", "validation_error"}


def _percentile(values: list[float], probability: float) -> float:
    """Linear interpolation between adjacent sorted observations (Pandas default)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def classification_metrics(predictions: list[dict[str, Any]], field: str) -> dict:
    """Use the full enum even when a class is absent from the selected split."""
    if field not in LABELS:
        raise ValueError("未知评测字段。")
    labels = LABELS[field]
    columns = [*labels, FAILURE_LABEL]
    matrix = [[0 for _ in columns] for _ in labels]
    correct = 0
    for row in predictions:
        gold = row.get(f"gold_{field}")
        predicted = row.get(f"predicted_{field}")
        if gold not in labels:
            raise ValueError(f"非法 gold_{field} 标签。")
        if row.get("status") not in STATUSES:
            raise ValueError("非法预测状态。")
        if row["status"] == "success":
            if predicted not in labels:
                raise ValueError("成功结果必须包含全部合法预测标签。")
            correct += int(gold == predicted)
        else:
            if predicted is not None:
                raise ValueError("失败结果不能包含预测标签。")
            predicted = FAILURE_LABEL
        matrix[labels.index(gold)][columns.index(predicted)] += 1

    per_class = {}
    for index, label in enumerate(labels):
        true_positive = matrix[index][index]
        support = sum(matrix[index])
        predicted_count = sum(row[index] for row in matrix)
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": precision, "recall": recall, "f1": f1, "support": support,
        }
    return {
        "accuracy": correct / len(predictions) if predictions else 0.0,
        "macro_f1": sum(item["f1"] for item in per_class.values()) / len(labels),
        "labels": labels.copy(),
        "per_class": per_class,
        "confusion_matrix": {
            "gold_labels": labels.copy(), "prediction_labels": columns, "values": matrix,
        },
    }


def compute_metrics(predictions: list[dict[str, Any]], *, mode: str, run_complete: bool | None = None) -> dict:
    if mode not in {"demo", "live"}:
        raise ValueError("评测模式必须为 demo 或 live。")
    planned_count = len(predictions)
    controlled = any("request_attempts" in row for row in predictions)
    pending = [row for row in predictions if row.get("status") == "not_executed"]
    for row in pending:
        if any(row.get(f"predicted_{field}") is not None for field in LABELS) or row.get("latency_ms") != 0:
            raise ValueError("尚未执行的记录不能有预测或请求耗时。")
    # An aborted run has evidence metrics only, never a full-dataset score.
    predictions = [row for row in predictions if row.get("status") != "not_executed"]
    timings = []
    for row in predictions:
        value = row.get("latency_ms")
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value) or value < 0:
            raise ValueError("样本耗时必须为有限非负数。")
        timings.append(float(value))
    tasks = {field: classification_metrics(predictions, field) for field in LABELS}
    success_count = sum(row["status"] == "success" for row in predictions)
    failures = Counter(row["status"] for row in predictions if row["status"] != "success")
    result = {
        "sample_count": len(predictions),
        "success_count": success_count,
        "failed_count": len(predictions) - success_count,
        "structured_success_rate": success_count / len(predictions) if predictions else 0.0,
        "failures_by_status": {status: failures[status] for status in ("failed", "validation_error")},
        "latency": {
            "average_ms": sum(timings) / len(timings) if timings else 0.0,
            "p95_ms": _percentile(timings, 0.95),
            "percentile_method": "linear interpolation",
            "includes_failures": True,
            "includes_application_retries_and_backoff": mode == "live",
            "includes_thread_queue_wait": False,
            "is_mock": mode == "demo",
            "measurement": (
                "确定性 Mock 数值，仅演示指标计算，不能代表真实接口性能。"
                if mode == "demo" else
                "每个工作线程开始调用至结果校验完成；含应用重试、退避和失败耗时，"
                "不含线程排队及共享客户端初始化。"
            ),
        },
        "metric_policy": {
            "denominator": "全部有效评测样本；调用或结构校验失败均计为错误。",
            "macro_f1": "完整固定枚举标签集合；零分母取 0；逐类展示 support。",
            "failure_prediction": "无有效预测；混淆矩阵使用独立 __failure__ 列。",
        },
        **tasks,
    }
    if controlled or pending:
        result.update(planned_sample_count=planned_count, not_executed_count=len(pending),
                      assessment_complete=not pending and run_complete is not False)
        if pending or run_complete is False:
            result["metric_policy"]["denominator"] = "运行未完成；仅对已执行样本计算证据指标。尚未执行不计调用失败，不构成完整数据集评测。"
    return result
