"""Small, auditable chart choices over an already authorized report snapshot.

The model selects a chart source, never its values, columns, code, or batch ID.
Python derives every point from validated labels or the caller-selected history.
No additional model request, file lookup, or arbitrary expression is possible.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from core.customer_report import CATEGORY_LABELS, PRIORITY_LABELS, SENTIMENT_LABELS


ChartSource = Literal["sentiment", "category", "group_negative_rate", "topics", "history_sentiment"]


class AgentChartRequest(BaseModel):
    """Only a bounded source choice is accepted from the language model."""

    model_config = ConfigDict(extra="forbid")

    source: ChartSource
    limit: StrictInt = Field(default=5, ge=1, le=10)


class AgentChartPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    value: float = Field(ge=0, allow_inf_nan=False)
    numerator: StrictInt = Field(ge=0)
    denominator: StrictInt = Field(gt=0)
    series: str = Field(default="", max_length=12)


class AgentChart(BaseModel):
    """Serializable checked data; renderers need no model-generated code."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["bar"] = "bar"
    source: ChartSource
    title: str
    unit: Literal["条", "%"]
    denominator_note: str
    scope: Literal["current", "selected_history"] = "current"
    data: list[AgentChartPoint] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def checked_values(self):
        for point in self.data:
            expected = point.numerator / point.denominator * 100 if self.unit == "%" else point.numerator
            if point.numerator > point.denominator or not math.isclose(point.value, expected, abs_tol=0.000001):
                raise ValueError("图表数值必须与明确分母下的计数一致。")
        return self


def _label(value) -> str:
    # Display as literal text. In particular, never interpolate into HTML/code.
    return " ".join(str(value).replace("\x00", " ").split())[:160] or "（未填写）"


def _effective_rows(report: dict) -> pd.DataFrame:
    frame, names = report.get("detail"), report.get("detail_columns")
    if not isinstance(frame, pd.DataFrame) or not isinstance(names, dict) or frame.columns.duplicated().any():
        return pd.DataFrame()
    required = ("source_row", "sentiment", "category", "priority")
    if any(names.get(key) not in frame.columns for key in required):
        return pd.DataFrame()
    # Failed, pending and unresolvable duplicate rows have blank labels. Require
    # all three valid fixed labels, just as the report's effective denominator.
    valid = (frame[names["sentiment"]].isin(SENTIMENT_LABELS.values())
             & frame[names["category"]].isin(CATEGORY_LABELS.values())
             & frame[names["priority"]].isin(PRIORITY_LABELS.values()))
    if names.get("processing_status") in frame.columns:
        valid &= frame[names["processing_status"]].isin({"分析成功", "重复评价（复用分析）", "success"})
    result = frame.loc[valid].copy()
    rows = result[names["source_row"]].tolist()
    if any(isinstance(row, bool) or not isinstance(row, (int, float)) or not float(row).is_integer() or row < 2 for row in rows):
        return pd.DataFrame()
    if len(set(rows)) != len(rows):
        return pd.DataFrame()
    return result


def _point(label, count: int, denominator: int, *, percent=False, series="") -> AgentChartPoint:
    return AgentChartPoint(label=_label(label), value=count / denominator * 100 if percent else count,
                           numerator=count, denominator=denominator, series=series)


def _inferred_requests(question: str) -> list[AgentChartRequest]:
    """Explicit requests for a chart can work with older model responses too."""
    if not isinstance(question, str) or not re.search(r"图表|可视化|画.{0,20}图|分布图|柱状图|条形图|饼图|chart|plot", question, re.I):
        return []
    # Do not silently turn a request for a time trend or another unsupported
    # dimension into an unrelated sentiment picture.
    if re.search(r"上批|上一批|两批|历史", question) and re.search(r"情感|好评|差评|正面|负面", question):
        source = "history_sentiment"
    elif re.search(r"门店|产品|商品|分组", question) and re.search(r"差评|负面", question):
        source = "group_negative_rate"
    elif re.search(r"主题|具体问题", question):
        source = "topics"
    elif re.search(r"类别|分类|方向", question):
        source = "category"
    elif re.search(r"情感|好评|差评|正面|负面", question):
        source = "sentiment"
    else:
        return []
    return [AgentChartRequest(source=source)]


def _history_chart(report: dict, current: Counter, total: int) -> AgentChart | None:
    selected = report.get("historical_context")
    if not isinstance(selected, dict) or not isinstance(selected.get("batch"), dict):
        return None
    if not isinstance(selected["batch"].get("id"), str) or not selected["batch"]["id"].strip():
        return None
    comparison = selected.get("comparison", {})
    if not isinstance(comparison, dict) or comparison.get("basic_comparable", comparison.get("comparable")) is not True:
        return None
    previous_base, current_base = comparison.get("previous_base"), comparison.get("current_base")
    if not isinstance(previous_base, dict) or not isinstance(current_base, dict):
        return None
    previous_total = previous_base.get("success_rows")
    current_total = current_base.get("success_rows")
    if type(previous_total) is not int or previous_total <= 0 or type(current_total) is not int or current_total != total:
        return None
    distributions = comparison.get("distributions", {})
    records = distributions.get("sentiment") if isinstance(distributions, dict) else None
    if not isinstance(records, list) or len(records) != 3:
        return None
    by_label = {}
    for record in records:
        if not isinstance(record, dict) or record.get("label") not in SENTIMENT_LABELS.values():
            return None
        label, count = record["label"], record.get("previous_count")
        if label in by_label or type(count) is not int or not 0 <= count <= previous_total:
            return None
        if type(record.get("current_count")) is not int or record["current_count"] != current[label]:
            return None
        # Existing comparison payload is locally computed, but verify its
        # shares too before treating it as a chart-worthy historical aggregate.
        for key, expected in (("previous_share", count / previous_total), ("current_share", current[label] / total)):
            value = record.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not math.isclose(value, expected, abs_tol=0.000001):
                return None
        by_label[label] = count
    if sum(by_label.values()) != previous_total:
        return None
    points = []
    for label in SENTIMENT_LABELS.values():
        points.extend([_point(label, current[label], total, percent=True, series="本批"),
                       _point(label, by_label[label], previous_total, percent=True, series="上批")])
    return AgentChart(source="history_sentiment", title="本批与选定上批的情感占比", unit="%",
                      scope="selected_history", denominator_note="分别以各批有效评价为分母；仅比较选定两个导入批次，不代表时间趋势或因果效果。", data=points)


def build_agent_charts(report: dict, requests=None, *, question: str = "") -> list[AgentChart]:
    """Materialize at most two allowed specs; unavailable data produces no chart.

    Requests are validated before use, so model-supplied numbers, code, arbitrary
    columns and report identifiers are rejected instead of becoming graph data.
    """
    if requests is None:
        requests = []
    if not isinstance(requests, list) or len(requests) > 2:
        raise ValueError("最多选择两张受支持的图表。")
    parsed = [item if isinstance(item, AgentChartRequest) else AgentChartRequest.model_validate(item) for item in requests]
    parsed = parsed or _inferred_requests(question)
    frame = _effective_rows(report)
    if frame.empty:
        return []
    total, names, charts, seen = len(frame), report["detail_columns"], [], set()
    for request in parsed:
        if request.source in seen:
            continue
        seen.add(request.source)
        if request.source in ("sentiment", "category"):
            key = request.source
            labels = SENTIMENT_LABELS.values() if key == "sentiment" else CATEGORY_LABELS.values()
            counts = Counter(frame[names[key]])
            ordered = list(labels) if key == "sentiment" else sorted(labels, key=lambda label: -counts[label])[:request.limit]
            points = [_point(label, counts[label], total) for label in ordered]
            charts.append(AgentChart(source=key, title="整批有效评价的情感分布" if key == "sentiment" else "整批有效评价的主类别分布",
                                     unit="条", denominator_note=f"整批 {total} 条有效评价，每条原记录计入一个固定标签；包含已明确复用分析的评价，不限于本轮关注的原文子集。", data=points))
        elif request.source == "group_negative_rate":
            column = report.get("group_column")
            if not isinstance(column, str) or column not in frame.columns or column in names.values():
                continue
            work = frame.copy()
            work["__chart_group"] = work[column].map(lambda value: "（未填写）" if pd.isna(value) or not str(value).strip() else str(value))
            points = []
            for label, group in work.groupby("__chart_group", sort=False):
                denominator = len(group)
                # Match the existing group-comparison lower sample boundary.
                if denominator >= 2:
                    count = int(group[names["sentiment"]].eq("负面").sum())
                    points.append(_point(label, count, denominator, percent=True))
            if len(points) < 2:
                continue
            points = sorted(points, key=lambda point: (-point.value, -point.denominator))[:request.limit]
            charts.append(AgentChart(source=request.source, title="整批各组负面评价占比", unit="%",
                                     denominator_note="以整批中各组有效评价数分别作分母，不限于本轮关注的原文子集；仅展示至少两条有效评价的组，样本量差异不代表业务优劣。", data=points))
        elif request.source == "topics":
            valid_rows = set(frame[names["source_row"]].map(int))
            points = []
            for item in report.get("topics", []) if isinstance(report.get("topics"), list) else []:
                if not isinstance(item, dict) or not isinstance(item.get("source_rows"), list):
                    continue
                rows = {row for row in item["source_rows"] if type(row) is int and row in valid_rows}
                if rows:
                    kind = {"praise": "认可", "issue": "问题", "request": "需求"}.get(item.get("kind"))
                    label = _label(item.get("topic", "主题"))
                    if kind:
                        label = f"{label[:150]} · {kind}"
                    points.append(_point(label, len(rows), total))
            if points:
                points = sorted(points, key=lambda point: -point.numerator)[:request.limit]
                charts.append(AgentChart(source=request.source, title="整批评价具体主题", unit="条",
                                         denominator_note=f"整批 {total} 条有效评价，不限于本轮关注的原文子集；每个主题每条评价最多计一次，一条评价可涉及多个主题，主题并不都表示缺点。", data=points))
        else:
            chart = _history_chart(report, Counter(frame[names["sentiment"]]), total)
            if chart is not None:
                charts.append(chart)
    return charts
