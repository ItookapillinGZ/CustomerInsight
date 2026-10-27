"""Grounded multi-aspect review topics and comparable import-batch summaries.

The historical single-review classifier is unchanged. These product topics are
separate observations grounded in a continuous quote from each original review.
"""

from collections import defaultdict
import json
import math
from numbers import Real
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from core.topic_taxonomy import TAXONOMY_VERSION, normalize_topic

TOPICS_VERSION = "aspects_v1"
TOPIC_ALIASES = {
    "减少塑料包装": "包装减塑", "减少塑料": "包装减塑", "塑料包装减量": "包装减塑",
    "物流进度提醒": "订单进度提醒", "订单状态提醒": "订单进度提醒", "发货进度提醒": "订单进度提醒",
    "保温性能": "保温效果", "保温时长": "保温效果", "保温能力": "保温效果",
    "密封性": "密封漏水", "杯盖漏水": "密封漏水", "密封防漏": "密封漏水",
    "包装破损": "包装完整", "包装完好": "包装完整", "包装完整性": "包装完整",
    "客服解释": "客服解答", "客服答疑": "客服解答", "客服讲解": "客服解答",
    "配送速度": "配送时效", "送货速度": "配送时效",
}
_BROAD_TOPICS = {"产品质量", "客户服务", "价格", "配送物流", "使用体验", "功能需求", "程序故障", "改进建议", "其他", "好评", "差评", "中性评价"}


class ReviewAspect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: StrictStr = Field(min_length=1, max_length=40)
    kind: Literal["praise", "issue", "request"]
    sentiment: Literal["positive", "negative", "neutral"]
    evidence: StrictStr = Field(min_length=1, max_length=240)

    @field_validator("topic")
    @classmethod
    def specific_topic(cls, value: str) -> str:
        value = value.strip()
        if not value or value in _BROAD_TOPICS or any(ord(char) < 32 for char in value):
            raise ValueError("主题须为具体、可复用的体验名称，不能仅填写大类。")
        return value

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("主题必须包含本条评价的连续原文。")
        return value


def validate_aspects(value, original_text: str) -> list[ReviewAspect]:
    """Validate identity/quotes locally, without pretending to verify semantics."""
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError("每条评价最多包含 5 个方面。")
    aspects = [ReviewAspect.model_validate(item) for item in value]
    if any(aspect.evidence not in original_text for aspect in aspects):
        raise ValueError("主题依据必须是本条评价原文的连续片段。")
    return aspects


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
        raise ValueError("主题记录的原始行号无效。")
    return int(value)


def _aspects_json(value):
    if value is None or isinstance(value, float) and pd.isna(value):
        return []
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else []
        except (ValueError, TypeError) as exc:
            raise ValueError("主题记录不是有效 JSON。") from exc
    return value


def refresh_report_topics(report: dict) -> dict:
    """Recompute topics from traceable aspect records, including reused rows.

    History round trips may restore the records or the escaped detail column.
    Neither a main category nor a missing aspect field is invented as a topic.
    Fresh reports use the versioned alias taxonomy. Previously saved reports
    without a taxonomy keep their historical aggregation on explicit refresh;
    loading history never calls this function or upgrades stored results.
    """
    legacy = "topics_version" in report and "taxonomy_version" not in report
    taxonomy_version = None if legacy else report.get("taxonomy_version", TAXONOMY_VERSION)
    if not legacy and taxonomy_version != TAXONOMY_VERSION:
        raise ValueError("主题归一版本不受支持，不能重算或覆盖已保存的主题。")
    detail, names = report.get("detail"), report.get("detail_columns", {})
    text_column, group_column = report.get("text_column"), report.get("group_column")
    if not isinstance(detail, pd.DataFrame) or text_column not in detail or names.get("source_row") not in detail:
        raise ValueError("报告缺少可追溯的评价原文和原始行号。")
    originals = {}
    valid_rows = set()
    for raw in detail.to_dict(orient="records"):
        number = _int(raw[names["source_row"]])
        if number in originals:
            raise ValueError("报告存在重复原始行号。")
        text = raw.get(text_column)
        text = text if isinstance(text, str) else ""
        group = raw.get(group_column) if group_column else None
        if group is not None:
            try:
                group = None if pd.isna(group) else str(group)
            except (TypeError, ValueError):
                group = str(group)
        originals[number] = {"text": text, "group": group, "raw": raw}
        status = raw.get(names.get("processing_status"))
        if status in {"分析成功", "重复评价（复用分析）", "success"}:
            valid_rows.add(number)
    records = report.get("aspects_by_row")
    if records is None:
        aspect_column = names.get("aspects")
        records = [{"source_row": number, "aspects": _aspects_json(row["raw"].get(aspect_column))}
                   for number, row in originals.items()]
    if not isinstance(records, list):
        raise ValueError("报告主题依据必须为逐行记录。")
    by_row = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("报告主题依据格式无效。")
        number = _int(record.get("source_row"))
        if number not in originals or number in by_row:
            raise ValueError("报告主题依据行号未知或重复。")
        aspects = validate_aspects(_aspects_json(record.get("aspects")), originals[number]["text"])
        if aspects and number not in valid_rows:
            raise ValueError("无有效分析的评价不能计入主题。")
        by_row[number] = aspects
    buckets = defaultdict(dict)
    with_aspects = 0
    normalized_records = []
    for number, original in originals.items():
        aspects = by_row.get(number, []) if number in valid_rows else []
        normalized_records.append({"source_row": number, "aspects": [item.model_dump() for item in aspects]})
        if aspects:
            with_aspects += 1
        for aspect in aspects:
            topic = TOPIC_ALIASES.get(aspect.topic, aspect.topic) if legacy else normalize_topic(aspect.topic)
            key = f"{aspect.kind}:{topic}"
            # Repeated mentions within one original record have one weight.
            row = buckets[key].setdefault(number, {"topic": topic, "kind": aspect.kind,
                                                   "text": aspect.evidence, "group": original["group"],
                                                   "raw_topics": set()})
            row["raw_topics"].add(aspect.topic)
    base = len(valid_rows)
    topics = []
    for key, rows in buckets.items():
        first = next(iter(rows.values()))
        item = {"key": key, "topic": first["topic"], "kind": first["kind"],
                "review_count": len(rows), "share": len(rows) / base if base else 0.0,
                "source_rows": sorted(rows),
                "examples": [{"source_row": number, "text": data["text"], "group": data["group"],
                              **({"raw_topics": sorted(data["raw_topics"])} if not legacy else {})}
                             for number, data in sorted(rows.items())][:5]}
        if not legacy:
            item["raw_topics"] = sorted({raw for row in rows.values() for raw in row["raw_topics"]})
            item["taxonomy_version"] = taxonomy_version
        topics.append(item)
    topics.sort(key=lambda item: (0 if item["kind"] in {"issue", "request"} else 1,
                                  -item["review_count"], item["topic"], item["kind"]))
    report.update(topics=topics, topics_version=TOPICS_VERSION, aspects_by_row=normalized_records,
                  top_topics=topics[:3], topic_coverage={
                      "success_rows": base, "with_aspects_rows": with_aspects,
                      "without_aspects_rows": base - with_aspects,
                      "rate": with_aspects / base if base else 0.0,
                      "status": "no_valid_reviews" if not base else "unavailable" if not with_aspects else "complete" if with_aspects == base else "partial",
                  })
    if not legacy:
        report["taxonomy_version"] = taxonomy_version
    return report


def _basic_snapshot(report: dict) -> dict:
    """Count the shared fixed labels independently of optional aspect coverage.

    Old reports can have no aspects while still containing valid classifications.
    Read the effective detail rows, including explicitly reused classifications,
    rather than treating a missing aggregate or a missing topic as a zero.
    """
    from core.customer_report import CATEGORY_LABELS, PRIORITY_LABELS, SENTIMENT_LABELS

    detail, names, metrics = report.get("detail"), report.get("detail_columns"), report.get("metrics")
    if not isinstance(detail, pd.DataFrame) or not isinstance(names, dict) or not isinstance(metrics, dict):
        raise ValueError("缺少可核对的逐行分类记录。")
    columns = [names.get(key) for key in ("source_row", "processing_status", "sentiment", "category", "priority")]
    if detail.columns.duplicated().any() or any(not isinstance(column, str) or column not in detail for column in columns) or len(set(columns)) != len(columns):
        raise ValueError("缺少可核对的逐行分类字段。")
    source_rows = [_int(value) for value in detail[names["source_row"]].tolist()]
    if any(number < 2 for number in source_rows) or len(set(source_rows)) != len(source_rows):
        raise ValueError("分类记录的原始行号无法核对。")
    valid = detail.loc[detail[names["processing_status"]].isin({"分析成功", "重复评价（复用分析）", "success"})]
    total, success = len(detail), len(valid)
    for key, expected in (("total_rows", total), ("success_rows", success)):
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value != expected:
            raise ValueError("汇总样本数与逐行分类记录不一致。")
    if not total or not success:
        raise ValueError("没有可比较的有效分类评价。")
    counts = {}
    for dimension, labels in (("sentiment", SENTIMENT_LABELS), ("category", CATEGORY_LABELS), ("priority", PRIORITY_LABELS)):
        normalized = valid[names[dimension]].map({**labels, **{label: label for label in labels.values()}})
        if normalized.isna().any():
            raise ValueError("有效评价存在缺失或不兼容的固定分类标签。")
        values = normalized.value_counts()
        counts[dimension] = {label: int(values.get(label, 0)) for label in labels.values()}
    return {"total_rows": total, "success_rows": success, "counts": counts}


def compare_report_batches(current: dict, previous: dict) -> dict:
    """Compare fixed classifications even when optional topics are incomplete.

    ``comparable`` keeps its historical topic-only meaning. Basic statistics and
    topics have separate guards so a missing aspect never hides valid labels or
    becomes an invented zero for a topic.
    """
    reasons, basic_reasons, warnings = [], [], ["按导入批次比较；评价变化不能证明改进措施的因果效果。"]
    for label, key in [("评价字段", "text_column"), ("分组字段", "group_column")]:
        if current.get(key) != previous.get(key):
            reasons.append(f"两批{label}不一致。")
            basic_reasons.append(f"两批{label}不一致。")
    current_prompt = current.get("analysis_meta", {}).get("prompt_version")
    previous_prompt = previous.get("analysis_meta", {}).get("prompt_version")
    if not current_prompt or not previous_prompt or current_prompt != previous_prompt:
        reasons.append("两批分析规则版本不同或未记录，不能直接比较主题。")
        warnings.append("两批分析规则版本不同或未记录；基础比较仅反映已保存的固定标签统计差异，不代表业务改善。")
    if current.get("topics_version") != TOPICS_VERSION or previous.get("topics_version") != TOPICS_VERSION:
        reasons.append("主题提取版本不同或缺失。")
    current_taxonomy, previous_taxonomy = current.get("taxonomy_version"), previous.get("taxonomy_version")
    if current_taxonomy != previous_taxonomy:
        reasons.append("两批主题归一版本不同；历史主题保留原口径，不能直接比较主题增减。")
    elif current_taxonomy not in (None, TAXONOMY_VERSION):
        reasons.append("主题归一版本未受支持，不能直接比较主题增减。")
    bases = []
    for label, report in [("本批", current), ("上批", previous)]:
        metrics, coverage = report.get("metrics", {}), report.get("topic_coverage", {})
        base = {"total_rows": int(metrics.get("total_rows", 0)), "success_rows": int(metrics.get("success_rows", 0)),
                "topic_rows": int(coverage.get("with_aspects_rows", 0)), "topic_coverage": float(coverage.get("rate", 0))}
        bases.append(base)
        if not base["success_rows"]:
            reasons.append(f"{label}没有有效分析评价。")
        elif coverage.get("status") != "complete" or base["topic_rows"] != base["success_rows"]:
            reasons.append(f"{label}主题覆盖不完整，不能把缺失主题当作未出现。")
        if base["success_rows"] < 5:
            warnings.append(f"{label}有效评价不足 5 条，仅供查看样本变化。")
    snapshots = []
    for label, report in (("本批", current), ("上批", previous)):
        try:
            snapshots.append(_basic_snapshot(report))
        except (ValueError, TypeError, OverflowError) as exc:
            snapshots.append(None)
            basic_reasons.append(f"{label}{exc}")
    output = {"comparable": not reasons, "reasons": reasons,
              "topics_comparable": not reasons, "topic_reasons": list(reasons),
              "basic_comparable": not basic_reasons, "basic_reasons": basic_reasons,
              "warnings": warnings, "comparison_type": "batch",
              "current_base": bases[0], "previous_base": bases[1], "metrics": {},
              "distributions": {"sentiment": [], "category": [], "priority": []}, "topics": []}
    if not basic_reasons:
        now, before = snapshots
        for dimension in output["distributions"]:
            for label in now["counts"][dimension]:
                now_count, before_count = now["counts"][dimension][label], before["counts"][dimension][label]
                now_share, before_share = now_count / now["success_rows"], before_count / before["success_rows"]
                output["distributions"][dimension].append({
                    "label": label, "current_count": now_count, "previous_count": before_count,
                    "current_share": now_share, "previous_share": before_share,
                    "change_pp": round((now_share - before_share) * 100, 6),
                })
        for name, label, dimension, category in (
            ("success_rate", "有效覆盖率", None, None),
            ("positive_rate", "正面占比", "sentiment", "正面"),
            ("neutral_rate", "中性占比", "sentiment", "中性"),
            ("negative_rate", "负面占比", "sentiment", "负面"),
            ("high_priority_rate", "高优先级占比", "priority", "高"),
        ):
            denominator = "total_rows" if dimension is None else "success_rows"
            now_count = now["success_rows"] if dimension is None else now["counts"][dimension][category]
            before_count = before["success_rows"] if dimension is None else before["counts"][dimension][category]
            now_value, before_value = now_count / now[denominator], before_count / before[denominator]
            output["metrics"][name] = {
                "label": label, "current": now_value, "previous": before_value,
                "current_count": now_count, "previous_count": before_count,
                "current_denominator": now[denominator], "previous_denominator": before[denominator],
                "denominator": denominator, "change_pp": round((now_value - before_value) * 100, 6),
            }
    if reasons:
        return output
    current_topics = {item["key"]: item for item in current.get("topics", [])}
    previous_topics = {item["key"]: item for item in previous.get("topics", [])}
    for key in sorted(current_topics.keys() | previous_topics.keys()):
        now, before = current_topics.get(key, {}), previous_topics.get(key, {})
        descriptor = now or before
        now_count, before_count = now.get("review_count", 0), before.get("review_count", 0)
        now_share, before_share = now_count / bases[0]["success_rows"], before_count / bases[1]["success_rows"]
        change = "new" if not before_count else "not_seen" if not now_count else "increased" if now_share > before_share else "decreased" if now_share < before_share else "unchanged"
        output["topics"].append({"key": key, "topic": descriptor["topic"], "kind": descriptor["kind"],
                                  "current_count": now_count, "previous_count": before_count,
                                  "current_share": now_share, "previous_share": before_share,
                                  "change_pp": round((now_share - before_share) * 100, 6), "change": change})
    output["topics"].sort(key=lambda item: (-abs(item["change_pp"]), -item["current_count"], item["key"]))
    return output
