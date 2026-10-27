"""Local drill-down and suggested questions; never calls a model or reads storage."""

import pandas as pd


FOCUS_LABELS = {"sentiment": "情感", "category": "问题类别", "priority": "优先级", "group": "产品或门店", "topic": "具体主题"}


def focus_rows(report: dict, focus: dict | None) -> pd.DataFrame:
    detail = report["detail"]
    if not focus:
        return detail.copy(deep=True)
    field, value = focus.get("field"), focus.get("value")
    if field not in FOCUS_LABELS or not isinstance(value, str):
        return detail.iloc[:0].copy()
    names = report.get("detail_columns", {})
    if field == "topic":
        topic = next((item for item in report.get("topics", []) if item.get("key") == value), None)
        if topic is None or names.get("source_row") not in detail.columns:
            return detail.iloc[:0].copy()
        shown = detail[detail[names["source_row"]].isin(topic.get("source_rows", []))]
    else:
        column = report.get("group_column") if field == "group" else names.get(field)
        if column not in detail.columns:
            return detail.iloc[:0].copy()
        shown = detail[detail[column].fillna("（未填写）").map(str).eq(value)]
    if focus.get("negative_only"):
        column = names.get("sentiment")
        shown = shown[shown[column].eq("负面")] if column in shown.columns else shown.iloc[:0]
    return shown.copy(deep=True)


def focus_label(report: dict, focus: dict | None) -> str:
    if not focus:
        return "全部评价"
    field, value = focus.get("field"), focus.get("value", "")
    if field == "topic":
        topic = next((item for item in report.get("topics", []) if item.get("key") == value), {})
        value = topic.get("topic", "未知主题")
    label = f"{FOCUS_LABELS.get(field, '选择')}：{value}"
    return label + (" · 仅差评" if focus.get("negative_only") else "")


def focus_question(report: dict, focus: dict | None) -> str:
    """State the focus in the actual question, without changing whole-batch metrics."""
    shown = focus_rows(report, focus)
    if shown.empty:
        return ""
    label = focus_label(report, focus)[:100]
    row_column = report.get("detail_columns", {}).get("source_row")
    rows = "、".join(str(int(value)) for value in shown[row_column].head(6)) if row_column in shown else ""
    return (f"请关注「{label}」。当前查看 {len(shown)} 条评价，例如原表第 {rows} 行。"
            "请查询原文，说明具体反馈和可尝试的改进；需要比较时可结合整批统计，并明确每项统计的范围。")[:300]


def recommended_questions(report: dict) -> list[str]:
    """Offer at most three questions supported by the current report's fields."""
    questions = []
    topics = report.get("top_topics", report.get("topics", [])[:3])
    if topics:
        name = str(topics[0].get("topic", ""))[:70]
        questions.append(f"关于「{name}」，哪些评价最能体现这类反馈？请给出原文和可尝试的下一步。")
    else:
        questions.append("这批评价最值得关注什么？请结合统计和原文说明。")
    grouped = report.get("grouped")
    if report.get("group_column") and isinstance(grouped, pd.DataFrame) and len(grouped) > 1:
        questions.append("各产品或门店的差评占比有什么不同？请画图，并注明各组样本量。")
    if report.get("historical_context", {}).get("batch"):
        questions.append("上一批也有刚才提到的问题吗？请查两批原文，并说明可比较的范围。")
    if len(questions) < 3:
        questions.append("请画出整批评价的情感分布，并解释有哪些需要注意的地方。")
    if len(questions) < 3:
        questions.append("高优先级评价有哪些？请引用原文，并建议先核对什么。")
    return questions[:3]


def chart_focus(event, field: str, column: str, *, negative_only=False) -> dict | None:
    """Read only a bounded selection value, not arbitrary data from the browser."""
    if field not in FOCUS_LABELS or not hasattr(event, "get"):
        return None
    selection = event.get("selection", {})
    if not hasattr(selection, "get"):
        return None
    points = selection.get("ci_pick", [])
    if not isinstance(points, list) or not points or not isinstance(points[0], dict):
        return None
    value = points[0].get(column)
    if not isinstance(value, str) or not value or len(value) > 1000:
        return None
    return {"field": field, "value": value, "negative_only": bool(negative_only)}


def update_chart_focus(key: str, selected: dict | None, current: dict | None, seen: dict) -> dict | None:
    """Honor the latest chart event, including clear and reselect of one value."""
    previous = seen.get(key)
    if selected:
        seen[key] = selected
        return selected if previous != selected else current
    seen.pop(key, None)
    return None if previous and current == previous else current
