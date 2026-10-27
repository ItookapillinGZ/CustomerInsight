"""Evidence-based customer summaries and portable, escaped report downloads."""

import json
import pandas as pd

from core.csv_io import _validate_columns, export_results_csv
from core.report_visuals import build_report_insights, build_report_summary, render_customer_html
from core.review_topics import refresh_report_topics, validate_aspects, _aspects_json

SENTIMENT_LABELS = {"positive": "正面", "negative": "负面", "neutral": "中性"}
CATEGORY_LABELS = {
    "product_quality": "产品质量", "customer_service": "客户服务", "pricing": "价格",
    "delivery": "配送物流", "user_experience": "使用体验", "features": "功能需求",
    "bugs": "程序故障", "suggestions": "改进建议", "other": "其他",
}
PRIORITY_LABELS = {"high": "高", "medium": "中", "low": "低"}
STATUS_LABELS = {
    "pending": "待处理", "success": "分析成功", "failed": "调用失败",
    "validation_error": "输出校验失败", "skipped_empty": "空白已跳过",
    "skipped_duplicate": "重复已跳过", "skipped_invalid": "无效已跳过",
}


def _plain(value):
    if hasattr(value, "value"):
        return value.value
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (ValueError, TypeError):
        pass
    return value


def _physical_rows(frame: pd.DataFrame) -> list[int]:
    rows = frame.attrs.get("source_rows", list(range(2, len(frame) + 2)))
    if len(rows) != len(frame) or any(type(row) is not int or row < 2 for row in rows) or len(set(rows)) != len(rows):
        raise ValueError("原始表格行号无效，请重新读取数据。")
    return rows


def _count_table(values: pd.Series, labels: dict, title: str, denominator: int) -> pd.DataFrame:
    counts = values.value_counts()
    result = pd.DataFrame([
        {title: label, "数量": int(counts.get(value, 0)), "占成功样本比例": int(counts.get(value, 0)) / denominator if denominator else 0.0}
        for value, label in labels.items()
    ])
    return result.sort_values("数量", ascending=False, kind="stable").reset_index(drop=True)


def build_customer_report(
    source_frame: pd.DataFrame,
    results_frame: pd.DataFrame,
    text_column: str,
    group_column: str | None = None,
    *,
    analysis_meta: dict | None = None,
) -> dict:
    """Join by physical row only; missing results remain pending, never neutral.

    Rates are fractions in [0, 1]. Every original review has one statistical
    weight, including duplicates whose first successful analysis can be reused.
    Reused labels are report-only; source results and model-call counts stay intact.
    """
    if not isinstance(source_frame, pd.DataFrame) or not isinstance(results_frame, pd.DataFrame):
        raise ValueError("原始记录和分析结果必须为数据表。")
    if analysis_meta is not None and not isinstance(analysis_meta, dict):
        raise ValueError("分析记录必须为字典。")
    _validate_columns(list(source_frame.columns))
    if text_column not in source_frame.columns:
        raise ValueError("评价文本列不存在。")
    if group_column is not None and group_column not in source_frame.columns:
        raise ValueError("产品或门店分组列不存在。")
    physical_rows = _physical_rows(source_frame)
    original_text_by_row = dict(zip(physical_rows, source_frame[text_column].tolist()))
    if not results_frame.empty and not {"source_row", "processing_status"}.issubset(results_frame.columns):
        raise ValueError("分析结果缺少原始行号或处理状态。")
    if results_frame.columns.duplicated().any():
        raise ValueError("分析结果包含重复字段。")
    allowed_rows = set(physical_rows)
    by_row = {}
    for raw in results_frame.to_dict(orient="records"):
        row = {key: _plain(value) for key, value in raw.items()}
        number = row.get("source_row")
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not float(number).is_integer() or int(number) not in allowed_rows:
            raise ValueError("分析结果行号无法关联原始记录。")
        number = int(number)
        if number in by_row:
            raise ValueError("同一原始行存在多份分析结果，不能生成统计。")
        status = row.get("processing_status")
        if status not in STATUS_LABELS:
            raise ValueError("分析结果包含未知处理状态。")
        if status == "success":
            if row.get("sentiment") not in SENTIMENT_LABELS or row.get("category") not in CATEGORY_LABELS or row.get("priority") not in PRIORITY_LABELS or not isinstance(row.get("summary"), str) or not row["summary"].strip():
                raise ValueError("成功记录缺少有效标签或摘要，不能计入统计。")
            source_text = original_text_by_row[number]
            source_text = source_text if isinstance(source_text, str) else ""
            row["aspects"] = [aspect.model_dump() for aspect in validate_aspects(_aspects_json(row.get("aspects")), source_text)]
        else:
            row["aspects"] = []
        by_row[number] = row

    aligned = pd.DataFrame([
        {"source_row": number, **by_row.get(number, {"processing_status": "pending"})}
        for number in physical_rows
    ], columns=[
        "source_row", "processing_status", "sentiment", "category", "priority",
        "summary", "error_message", "duplicate_of", "latency_ms", "aspects",
    ])

    normalized_text = {}
    first_row_for_text = {}
    for number, value in zip(physical_rows, source_frame[text_column].tolist()):
        text = value.strip() if isinstance(value, str) else ""
        normalized_text[number] = text
        if text:
            first_row_for_text.setdefault(text, number)
    reference_rows = {}
    for number, row in by_row.items():
        reference = row.get("feedback_id") or f"row-{number}"
        if isinstance(reference, str):
            reference_rows.setdefault(reference, []).append(number)

    effective = aligned.copy(deep=True)
    reused_from = {}
    for index, number in enumerate(physical_rows):
        if aligned.at[index, "processing_status"] != "skipped_duplicate":
            continue
        reference = _plain(aligned.at[index, "duplicate_of"])
        targets = reference_rows.get(reference, []) if isinstance(reference, str) else []
        text = normalized_text[number]
        if len(targets) != 1 or not text:
            continue
        target = targets[0]
        # Only a direct, exact reference to this text's first original record
        # may supply labels. Never follow duplicate chains or copy failed rows.
        if target == number or target != first_row_for_text.get(text) or normalized_text.get(target) != text or by_row[target]["processing_status"] != "success":
            continue
        for field in ("sentiment", "category", "priority", "summary", "aspects"):
            effective.at[index, field] = by_row[target][field]
        reused_from[index] = target
    model_success_mask = aligned["processing_status"].eq("success")
    reused_mask = pd.Series([index in reused_from for index in range(len(aligned))], index=aligned.index, dtype=bool)
    success_mask = model_success_mask | reused_mask
    success = effective.loc[success_mask]
    total, completed = len(source_frame), int(success_mask.sum())
    status_counts = aligned["processing_status"].value_counts()
    negative = int(success["sentiment"].eq("negative").sum())
    neutral = int(success["sentiment"].eq("neutral").sum())
    positive = int(success["sentiment"].eq("positive").sum())
    high = int(success["priority"].eq("high").sum())
    metrics = {
        "total_rows": total, "success_rows": completed,
        "model_success_rows": int(model_success_mask.sum()),
        "reused_rows": int(reused_mask.sum()),
        "unresolved_duplicate_rows": int(status_counts.get("skipped_duplicate", 0)) - int(reused_mask.sum()),
        "failed_rows": int(status_counts.get("failed", 0) + status_counts.get("validation_error", 0)),
        "pending_rows": int(status_counts.get("pending", 0)),
        "skipped_empty_rows": int(status_counts.get("skipped_empty", 0)),
        "skipped_duplicate_rows": int(status_counts.get("skipped_duplicate", 0)),
        "skipped_invalid_rows": int(status_counts.get("skipped_invalid", 0)),
        "positive_rows": positive, "negative_rows": negative, "neutral_rows": neutral,
        "high_priority_rows": high,
        "success_rate": completed / total if total else 0.0,
        "negative_rate": negative / completed if completed else 0.0,
        "neutral_rate": neutral / completed if completed else 0.0,
        "high_priority_rate": high / completed if completed else 0.0,
    }

    # The source stays byte-for-byte at the cell-value level; generated columns
    # receive a suffix when a customer already used the same Chinese heading.
    detail = source_frame.copy(deep=True).reset_index(drop=True)
    detail.attrs = {}
    names = {}

    def add(key: str, preferred: str, values) -> None:
        candidate = preferred
        suffix = 1
        while candidate in detail.columns:
            candidate = f"{preferred}（分析{suffix}）"
            suffix += 1
        names[key] = candidate
        detail[candidate] = list(values)

    add("source_row", "原始行号", physical_rows)
    display_status = aligned["processing_status"].map(STATUS_LABELS)
    display_status.loc[reused_mask] = "重复评价（复用分析）"
    add("processing_status", "分析状态", display_status)
    add("original_processing_status", "原始处理状态", aligned["processing_status"])
    add("result_source", "结果来源", [
        f"复用原始第 {reused_from[index]} 行，未新增模型请求" if index in reused_from
        else "独立模型分析" if model_success_mask.iloc[index] else "无有效分析"
        for index in range(len(aligned))
    ])
    for field, title, labels in [
        ("sentiment", "情感", SENTIMENT_LABELS),
        ("category", "问题类别", CATEGORY_LABELS),
        ("priority", "优先级", PRIORITY_LABELS),
    ]:
        # Ignore any stale labels present on a failed result.
        add(field, title, effective[field].where(success_mask).map(labels).fillna(""))
    add("summary", "评价摘要", effective["summary"].where(success_mask).fillna(""))
    aspect_records = [
        {"source_row": number, "aspects": _aspects_json(effective.at[index, "aspects"]) if success_mask.iloc[index] else []}
        for index, number in enumerate(physical_rows)
    ]
    add("aspects", "具体体验方面", [json.dumps(record["aspects"], ensure_ascii=False) for record in aspect_records])
    reasons = aligned["error_message"].fillna("")
    for index, target in reused_from.items():
        reasons.at[index] = f"与原始第 {target} 行评价文本相同，复用该行标签和摘要；未新增模型请求。"
    add("error_message", "处理说明", reasons)
    add("duplicate_of", "重复来源", aligned["duplicate_of"].fillna(""))
    add("latency_ms", "分析耗时(毫秒)", aligned["latency_ms"].fillna(0.0))

    negative_mask = success_mask & effective["sentiment"].eq("negative")
    negative_examples = detail.loc[negative_mask].copy()
    if not negative_examples.empty:
        ranks = {"高": 0, "中": 1, "低": 2}
        order = negative_examples[names["priority"]].map(ranks).sort_values(kind="stable").index
        negative_examples = negative_examples.loc[order]
    negative_examples = negative_examples.reset_index(drop=True)

    groups = []
    if group_column is not None:
        # No parsing as links, HTML, dates or numbers; original categories remain
        # literal display values, and missing grouping values are explicit.
        group_values = source_frame[group_column].map(lambda value: "（未填写）" if _plain(value) is None or not str(value).strip() else str(value)).reset_index(drop=True)
        for label in group_values.drop_duplicates():
            mask = group_values.eq(label)
            group_success = mask & success_mask
            count = int(group_success.sum())
            group_negative = int((group_success & effective["sentiment"].eq("negative")).sum())
            group_high = int((group_success & effective["priority"].eq("high")).sum())
            groups.append({
                "分组": label, "原始记录数": int(mask.sum()), "成功分析数": count,
                "负面评价数": group_negative,
                "中性评价数": int((group_success & effective["sentiment"].eq("neutral")).sum()),
                "高优先级数": group_high,
                "负面占比": group_negative / count if count else 0.0,
                "高优先级占比": group_high / count if count else 0.0,
            })
    grouped = pd.DataFrame(groups, columns=["分组", "原始记录数", "成功分析数", "负面评价数", "中性评价数", "高优先级数", "负面占比", "高优先级占比"])
    if not grouped.empty:
        grouped = grouped.sort_values(["负面评价数", "成功分析数"], ascending=False, kind="stable").reset_index(drop=True)

    notes = [
        f"原始记录 {total} 条，有效覆盖评价 {completed} 条（独立模型分析 {metrics['model_success_rows']} 条，复用分析 {metrics['reused_rows']} 条）；情感、类别和优先级分布以有效覆盖评价数为分母。",
        "统计按每条原记录赋予相同权重。同文跨产品或门店分别计入各自分组，不因文字相同删除顾客评价。",
        "评价按去除首尾空白后的完整文本仅分析首条；重复项只在精确指向同文首条且首条成功时复用标签和摘要，不代表独立模型调用。",
        "复用结果假设相同文本有相同含义，未结合顾客、产品或门店上下文重新判断；如语境不同，应人工核对。首条失败、引用不明或文本不同的重复项不计有效分母。",
        "空白、无效、无法复用的重复、失败和待处理记录均不归入中性或其他类别。有效覆盖率以全部原始记录为分母。分组中的成功分析数包含已明确标记的复用记录。",
        "差评证据展示有效覆盖为负面的原文，包含标明来源的复用评价；问题排序按负面评价主类别计数，每条原记录只计一个主类别。",
        "分类与优先级由模型生成，供筛查和人工复核，不代表已核实的产品故障或因果结论。",
        "具体主题由评价中的多个方面提取，每个方面保留连续原文依据；认可、问题和需求分别统计。每个主题每条原记录最多计一次，主题占比以有效覆盖评价数为分母，各主题占比之和可以超过100%。旧报告未提取主题时不按大类补造主题。",
    ]
    report = {
        "metrics": metrics,
        "sentiment_counts": _count_table(success["sentiment"], SENTIMENT_LABELS, "情感", completed),
        "category_counts": _count_table(success["category"], CATEGORY_LABELS, "问题类别", completed),
        "priority_counts": _count_table(success["priority"], PRIORITY_LABELS, "优先级", completed),
        "problem_ranking": _count_table(effective.loc[negative_mask, "category"], CATEGORY_LABELS, "问题类别", negative).rename(columns={"占成功样本比例": "占负面评价比例"}),
        "grouped": grouped, "negative_examples": negative_examples, "detail": detail,
        "detail_columns": names, "notes": notes,
        "text_column": text_column, "group_column": group_column,
        "analysis_meta": dict(analysis_meta or {}),
        "aspects_by_row": aspect_records,
    }
    refresh_report_topics(report)
    report["insights"] = build_report_insights(report)
    report["executive_summary"] = build_report_summary(report)
    return report


def export_customer_csv(report: dict) -> bytes:
    """UTF-8 BOM for Excel; spreadsheet formulas are escaped on export only."""
    frame = report["detail"].copy(deep=True)
    # The existing exporter guards cells; uploaded column names need the same
    # protection because Excel also treats the header row as spreadsheet cells.
    from core.csv_io import _escape_csv_cell
    frame.columns = [_escape_csv_cell(column) for column in frame.columns]
    return export_results_csv(frame)


def export_customer_html(report: dict) -> bytes:
    """Standalone, printable business report; safe text, SVG and no network."""
    return render_customer_html(report)
