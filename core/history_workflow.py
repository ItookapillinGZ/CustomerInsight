"""Local field suggestions and explicit, bounded historical comparison context."""
from __future__ import annotations

import copy
from datetime import date, datetime
import re

import pandas as pd


def suggest_columns(columns: list[str], saved: dict | None = None) -> dict:
    """Conservative header matching; the visible selectors remain editable."""
    aliases = {
        "text_column": ("评价", "评价内容", "评论", "评论内容", "顾客评价", "客户评价", "反馈", "反馈内容", "feedback", "review", "reviewtext", "review_text", "comment", "text"),
        "group_column": ("商品", "产品", "商品名称", "产品名称", "门店", "门店名称", "product", "store", "product_name", "source"),
        "date_column": ("评价日期", "评论日期", "评价时间", "评论时间", "日期", "时间", "review_date", "date", "created_at"),
    }
    normalized = {re.sub(r"\s+", "", str(column)).lower(): column for column in columns}
    choices = {}
    for field, names in aliases.items():
        prior = (saved or {}).get(field)
        choices[field] = prior if prior in columns else next((normalized[name] for name in names if name in normalized), None)
    if choices["text_column"] is None and columns:
        choices["text_column"] = columns[0]
    if choices["group_column"] == choices["text_column"]:
        choices["group_column"] = None
    if choices["date_column"] in (choices["text_column"], choices["group_column"]):
        choices["date_column"] = None
    return choices


def date_scope(frame: pd.DataFrame, column: str | None) -> dict:
    """Only explicit calendar values are dates; never guess numeric serials."""
    if not column or column not in frame.columns:
        return {"date_column": None, "start": None, "end": None, "valid_rows": 0,
                "invalid_rows": 0, "label": "导入批次（未指定评价日期）"}
    parsed = []
    for value in frame[column].tolist():
        if isinstance(value, (datetime, date, pd.Timestamp)) and not pd.isna(value):
            parsed.append(value.date() if isinstance(value, (datetime, pd.Timestamp)) else value)
        elif isinstance(value, str) and re.match(r"^\s*\d{4}[-/年]\d{1,2}[-/月]\d{1,2}", value):
            cleaned = value.strip().replace("年", "-").replace("月", "-").replace("日", "")
            stamp = pd.to_datetime(cleaned, errors="coerce")
            if not pd.isna(stamp):
                parsed.append(stamp.date())
    start = min(parsed).isoformat() if parsed else None
    end = max(parsed).isoformat() if parsed else None
    return {"date_column": column, "start": start, "end": end, "valid_rows": len(parsed),
            "invalid_rows": len(frame) - len(parsed),
            "label": f"{start} 至 {end}" if start else "导入批次（日期无法识别）"}


def attach_historical_context(report: dict, previous: dict | None) -> dict:
    """Add only the explicitly selected local report, not a store or file path."""
    from core.review_topics import compare_report_batches

    current = copy.deepcopy(report)
    current.pop("historical_context", None)
    if not previous:
        return current
    old = previous["report"]
    comparison = compare_report_batches(current, old)
    comparison["current_period"] = current.get("period", {})
    comparison["previous_period"] = old.get("period", {})
    warnings = list(comparison.get("warnings", []))
    if min(current["metrics"]["success_rows"], old["metrics"]["success_rows"]) < 5:
        warnings.append("至少一批有效评价不足 5 条，变化仅作线索，不能判断需求普遍性。")
    current_period, old_period = current.get("period", {}), old.get("period", {})
    for label, scope in (("当前批次", current_period), ("上批", old_period)):
        if scope.get("date_column") and scope.get("invalid_rows", 0):
            warnings.append(f"{label}有 {scope['invalid_rows']} 条日期无法识别或缺失，显示的日期范围只覆盖可识别记录。")
    if current_period.get("start") and old_period.get("start"):
        if max(current_period["start"], old_period["start"]) <= min(current_period["end"], old_period["end"]):
            warnings.append("两批评价日期有重叠，可能包含同一条顾客评价，不能当作独立时段趋势。")
    else:
        warnings.append("日期范围不完整；这里只比较两次导入，不推断周度或月度趋势。")
    comparison["warnings"] = warnings
    names, detail = old.get("detail_columns", {}), old.get("detail")
    evidence = []
    if isinstance(detail, pd.DataFrame) and names.get("source_row") in detail.columns:
        for _, row in detail.head(100).iterrows():
            text = row.get(old.get("text_column"))
            if not isinstance(text, str) or not text.strip():
                continue
            group = row.get(old.get("group_column"), "") if old.get("group_column") else ""
            evidence.append({"source_row": int(row[names["source_row"]]), "text": text,
                             "group": "" if pd.isna(group) else str(group)})
    current["historical_context"] = {
        "batch": {"id": previous["id"], "filename": previous["filename"],
                  "created_at": previous["created_at"]},
        "comparison": comparison, "evidence": evidence,
    }
    return current
