"""Portable, script-free briefs from explicitly selected, scoped answers."""

from __future__ import annotations

from html import escape
import re

import pandas as pd

from core.agent_charts import AgentChart, AgentChartRequest, build_agent_charts
from core.analysis_agent import AnalysisAgentResult, ReportAgentContext, report_fingerprint


_STYLE = """+:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f2f5f7;color:#203748;font-family:system-ui,"Microsoft YaHei",sans-serif;line-height:1.7}main{max-width:1040px;margin:32px auto;padding:40px;background:white;border-radius:16px}header{border-bottom:2px solid #dfe9ec;padding-bottom:24px}.brand{font-size:13px;letter-spacing:.12em;color:#2c7c86}h1{font-size:30px;line-height:1.3;margin:12px 0}h2{font-size:21px;margin:0 0 16px}h3{font-size:17px;margin:20px 0 10px}.note{color:#5d7180;font-size:13px}.scope{background:#edf6f5;border-left:4px solid #287b83;padding:12px 16px;margin:16px 0}.answer{border-bottom:1px solid #dfe9ec;padding:28px 0}.badge{display:inline-block;background:#e5f2f4;color:#255c68;font-size:12px;border-radius:20px;padding:3px 10px}.predefined{background:#fff3dc;color:#82611f}.prose{white-space:pre-wrap;overflow-wrap:anywhere}.metrics{background:#f4f7fa;padding:12px 20px;border-radius:8px}.chart{border:1px solid #e0e8ec;border-radius:10px;padding:18px;margin:16px 0;break-inside:avoid}.chart svg{display:block;width:100%;height:auto}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #dfe9ec;padding:10px}th{background:#f4f7fa}td{white-space:pre-wrap;overflow-wrap:anywhere}.row{white-space:nowrap;width:90px}.source{color:#5d7180;font-size:12px}footer{margin-top:24px;font-size:12px;color:#5d7180}@media(max-width:650px){main{margin:0;padding:22px;border-radius:0}h1{font-size:26px}.chart{padding:10px}}@media print{body{background:white}main{margin:0;padding:0;max-width:none}h2,h3{break-after:avoid}.chart{break-inside:avoid}a{color:inherit;text-decoration:none}}
"""


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\x00", " ")
    try:
        if pd.isna(value):
            return ""
    except (ValueError, TypeError):
        pass
    return str(value).replace("\x00", " ")


def _period(value) -> str:
    if not isinstance(value, dict):
        return "导入批次（未指定评价日期）"
    start, end = value.get("start"), value.get("end")
    if start and end:
        count = value.get("valid_rows")
        suffix = f"；可识别日期 {count} 条" if type(count) is int and count >= 0 else ""
        return f"{_text(start)} 至 {_text(end)}{suffix}"
    return _text(value.get("label")) or "导入批次（未指定评价日期）"


def _source_label(report: dict, answer: AnalysisAgentResult) -> tuple[str, str]:
    provenance = report.get("provenance", {})
    synthetic = report.get("data_origin") == "synthetic" or isinstance(provenance, dict) and provenance.get("data_origin") == "synthetic"
    if answer.answer_origin == "llm" and answer.model_calls > 0:
        return "LLM 生成回答" + (" · 合成示例数据" if synthetic else ""), "badge"
    if (answer.answer_origin == "fallback" and answer.status == "completed"
            and answer.model_calls == 0 and synthetic
            and "离线预置" in (answer.fallback_reason or "")):
        return "离线预置示例 · 未调用模型", "badge predefined"
    if answer.answer_origin == "legacy_template":
        return "规则模板 · 非 LLM 回答", "badge predefined"
    return "未完成的回答 · 请勿作为经营结论", "badge predefined"


def _svg(chart: AgentChart, identifier: str) -> str:
    """Only this fixed template supplies SVG markup, coordinates, and colors."""
    height = 54 * len(chart.data) + 18
    description = "；".join(f"{point.series}{point.label}：{point.numerator}/{point.denominator} 条" for point in chart.data)
    output = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 {height}" role="img" aria-labelledby="{identifier}-title {identifier}-description">',
              f'<title id="{identifier}-title">{escape(chart.title)}</title>',
              f'<desc id="{identifier}-description">{escape(description)}</desc>']
    # Count charts use a common valid-review denominator; rate charts use 100%.
    maximum = 100 if chart.unit == "%" else max(point.denominator for point in chart.data)
    for index, point in enumerate(chart.data):
        y, width = index * 54 + 20, 480 * min(1, point.value / maximum)
        label = (f"{point.series} · " if point.series else "") + point.label
        display = label if len(label) <= 32 else label[:31] + "…"
        number = f"{point.value:.1f}% · {point.numerator}/{point.denominator} 条" if chart.unit == "%" else f"{point.numerator} 条 · {point.numerator / point.denominator:.1%}"
        color = "#8ca6b5" if point.series == "上批" else "#287c86"
        output.extend([f'<text x="0" y="{y}" fill="#203748" font-size="14">{escape(display)}</text>',
                       f'<rect x="0" y="{y + 10}" width="480" height="11" rx="3" fill="#e8eff2"/>',
                       f'<rect x="0" y="{y + 10}" width="{width:.3f}" height="11" rx="3" fill="{color}"/>',
                       f'<text x="755" y="{y + 20}" text-anchor="end" fill="#5d7180" font-size="13">{escape(number)}</text>'])
    output.append("</svg>")
    return "".join(output)


def _references(answer: AnalysisAgentResult, context: ReportAgentContext):
    names, detail = context.report["detail_columns"], context.report["detail"]
    if names.get("source_row") not in detail.columns or context.report.get("text_column") not in detail.columns:
        raise ValueError("报告缺少原始行与评价内容。")
    by_row = {}
    for _, row in detail.iterrows():
        number = row[names["source_row"]]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not float(number).is_integer() or number < 2 or int(number) in by_row:
            raise ValueError("报告原始行号无效或重复。")
        by_row[int(number)] = row
    selected_rows = set(answer.evidence_rows)
    for point in answer.findings + answer.actions:
        selected_rows.update(point.evidence_rows)
    if len(selected_rows) > 60 or selected_rows - set(by_row):
        raise ValueError("回答引用了本批不存在或过多的原始行。")
    selected = context._selected_history()
    history_rows = []
    previous = {item["source_row"]: item for item in selected["evidence"]} if selected else {}
    if len(answer.historical_evidence) > 20:
        raise ValueError("上批引用过多。")
    seen = set()
    for item in answer.historical_evidence:
        if not isinstance(item, dict) or selected is None:
            raise ValueError("回答没有可访问的选定上批。")
        number = item.get("source_row")
        if type(number) is not int or number not in previous or item.get("batch_id") != selected["batch"]["id"]:
            raise ValueError("回答引用了未选择批次或不存在的上批行。")
        if item.get("filename") != selected["batch"]["filename"]:
            raise ValueError("回答中的上批文件名与选定快照不一致。")
        excerpt = item.get("text", "")
        if not isinstance(excerpt, str) or excerpt not in previous[number]["text"]:
            raise ValueError("回答携带的上批引文不属于选定快照。")
        if number not in seen:
            history_rows.append(previous[number])
            seen.add(number)
    return [by_row[number] for number in sorted(selected_rows)], history_rows, selected


def render_agent_brief(report: dict, answers: list[dict | AnalysisAgentResult]) -> bytes:
    """Export 1-10 selected answers and their checked evidence, without I/O.

    Persistent chart labels/values are never used: accepted chart sources are
    rematerialized against this report. Source-row references are checked again.
    This does not certify the semantics of model-written prose or advice.
    """
    if not isinstance(answers, list) or not 1 <= len(answers) <= 10:
        raise ValueError("请选择 1 到 10 条回答生成经营简报。")
    context = ReportAgentContext(report)
    fingerprint = report_fingerprint(report)
    normalized = [AnalysisAgentResult.model_validate(answer.model_dump(mode="json") if isinstance(answer, AnalysisAgentResult) else answer)
                  for answer in answers]
    sections = []
    for index, answer in enumerate(normalized, 1):
        if answer.report_fingerprint != fingerprint:
            raise ValueError("选中的回答不属于本批及当前选定上批，请重新选择。")
        if re.search(r"\[\[(?:metric|row|history_row):", answer.conclusion):
            raise ValueError("回答仍包含未解析的来源引用。")
        rows, history_rows, selected = _references(answer, context)
        source, css = _source_label(report, answer)
        section = [f'<section class="answer"><p><span class="{css}">{escape(source)}</span></p>',
                   f'<h2>{index}. {escape(answer.question)}</h2>',
                   f'<div class="prose">{escape(answer.conclusion)}</div>']
        for title, points in (("主要发现", answer.findings), ("建议行动", answer.actions)):
            if points:
                section.append(f'<h3>{title}</h3>')
                for point in points:
                    section.append(f'<p><strong>{escape(point.title)}</strong><br><span class="prose">{escape(point.narrative)}</span></p>')
                    if point.metrics:
                        section.append('<ul class="metrics">' + "".join(f'<li>{escape(metric)}</li>' for metric in point.metrics) + '</ul>')
        if answer.conclusion_metrics:
            section.append('<ul class="metrics">' + "".join(f'<li>{escape(metric)}</li>' for metric in answer.conclusion_metrics) + '</ul>')
        specs = [AgentChartRequest(source=chart.source, limit=min(10, max(1, len(chart.data)))) for chart in answer.charts]
        charts = build_agent_charts(context.report, specs) if specs else []
        for chart_index, chart in enumerate(charts, 1):
            section.extend(['<figure class="chart">', f'<h3>{escape(chart.title)}</h3>',
                            _svg(chart, f"answer-{index}-chart-{chart_index}"),
                            f'<figcaption class="note">{escape(chart.denominator_note)}</figcaption></figure>'])
        if specs and not charts:
            section.append('<p class="note">当前快照缺少可核对的图表数据，未导出缓存图表。</p>')
        if rows:
            section.append('<h3>本批原文依据</h3><div class="table-wrap"><table><thead><tr><th>原始行</th><th>评价原文</th><th>分析标签</th></tr></thead><tbody>')
            for row in rows:
                number = int(row[context.report["detail_columns"]["source_row"]])
                labels = " / ".join(_text(row.get(context.report["detail_columns"].get(key))) for key in ("sentiment", "category", "priority"))
                section.append(f'<tr><td class="row">第 {number} 行</td><td>{escape(_text(row[context.report["text_column"]]))}</td><td>{escape(labels)}</td></tr>')
            section.append('</tbody></table></div>')
        if history_rows:
            section.append(f'<h3>选定上批原文依据：《{escape(selected["batch"]["filename"]) }》</h3><div class="table-wrap"><table><thead><tr><th>上批原始行</th><th>评价原文</th></tr></thead><tbody>')
            for row in history_rows:
                section.append(f'<tr><td class="row">第 {row["source_row"]} 行</td><td>{escape(row["text"])}</td></tr>')
            section.append('</tbody></table></div>')
        if not rows and not history_rows:
            section.append('<p class="note">这条回答未引用具体原文行；请结合统计口径理解结论。</p>')
        section.append(f'<p class="source">{escape(answer.scope_note)}</p></section>')
        sections.append("".join(section))
    selected = context._selected_history()
    current_period = report.get("period")
    comparison = selected.get("comparison", {}) if selected else {}
    if not current_period and isinstance(comparison, dict):
        current_period = comparison.get("current_period")
    scope = [f'本批日期范围：{escape(_period(current_period))}。']
    if selected:
        old_period = comparison.get("previous_period") if isinstance(comparison, dict) else None
        scope.append(f'已选上批：《{escape(selected["batch"]["filename"])}》；日期范围：{escape(_period(old_period))}。')
    else:
        scope.append('未选择上批；本文件只涵盖当前导入批次。')
    if report.get("data_origin") == "synthetic":
        scope.append('数据与初始标签为离线合成示例，不代表真实商户或模型准确率。')
    content = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
               '<meta name="viewport" content="width=device-width,initial-scale=1">',
               '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'none\'; connect-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">',
               '<title>经营分析简报 | CustomerInsight</title><style>', _STYLE, '</style></head><body><main><header>',
               '<div class="brand">CUSTOMERINSIGHT</div><h1>经营分析简报</h1>',
               f'<p>汇集你选中的 {len(normalized)} 条回答、图表与原文，供核查与行动讨论。</p>',
               '<div class="scope">' + '<br>'.join(scope) + '</div>',
               '<p class="note">统计与图表按当前授权快照重新核对。模型解释、原因推测和改进建议仍需业务核实；两个导入批次的差异不证明措施效果。</p></header>',
               *sections,
               '<footer>离线文件，不运行脚本、不加载外部资源。可通过浏览器打印或另存为 PDF。本批和上批原始行号相互独立。</footer></main></body></html>']
    return "".join(content).encode("utf-8")
