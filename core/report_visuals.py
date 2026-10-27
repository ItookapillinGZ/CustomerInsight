"""Offline, accessible report visuals built only from validated report data."""

from html import escape
import math
from numbers import Real

import pandas as pd


INK = "#15334B"
TEAL = "#087F8C"
POSITIVE = "#2D826D"
NEGATIVE = "#C15D4F"
NEUTRAL = "#7D8D9F"
TRACK = "#E8F1F5"


def _text(value) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value) -> int:
    value = _number(value)
    return int(value) if value is not None else 0


def _row_numbers(frame, report, limit=3) -> list[int]:
    column = report.get("detail_columns", {}).get("source_row")
    if column not in frame.columns:
        return []
    return [int(value) for value in frame[column].head(limit).tolist()]


def build_report_summary(report: dict) -> str:
    """One evidence-based conclusion; no additional model call or business inference."""
    metrics = report["metrics"]
    total, covered = _count(metrics["total_rows"]), _count(metrics["success_rows"])
    if not total:
        return "本次表格没有评价记录，暂无法生成评价结论。"
    if not covered:
        return f"本次 {total} 条记录暂无有效分析结果，请先查看评价明细中的处理状态，暂无法判断顾客反馈倾向。"

    positive, negative = _count(metrics.get("positive_rows", 0)), _count(metrics.get("negative_rows", 0))
    clauses = [
        f"本次有效覆盖 {covered}/{total} 条评价",
        f"其中正面占 {positive / covered:.1%}、负面占 {negative / covered:.1%}",
    ]
    ranking = report["problem_ranking"]
    if negative and not ranking.empty:
        maximum = _count(ranking["数量"].max())
        leaders = ranking.loc[ranking["数量"].eq(maximum), "问题类别"].astype(str).tolist()
        if maximum and leaders:
            label = "、".join(leaders[:2])
            if len(leaders) > 2:
                label += f"等 {len(leaders)} 类"
            clauses.append(
                f"差评中{label}{'并列最多（各 ' if len(leaders) > 1 else '最多（'}{maximum} 条）"
            )
    elif not negative:
        clauses.append("有效评价中暂无负面结果")
    high = _count(metrics.get("high_priority_rows", 0))
    clauses.append(f"建议优先复核 {high} 条高优先级评价" if high else "暂未识别出高优先级评价")
    if covered < total:
        clauses.append("结论仅反映已覆盖部分")
    return "，".join(clauses) + "。"


def build_report_insights(report: dict) -> list[dict]:
    """Deterministic reading guidance, with original rows for every claim."""
    metrics = report["metrics"]
    total = _count(metrics["total_rows"])
    covered = _count(metrics["success_rows"])
    reused = _count(metrics.get("reused_rows", 0))
    independent = _count(metrics.get("model_success_rows", covered - reused))
    failed = _count(metrics.get("failed_rows", 0))
    pending = _count(metrics.get("pending_rows", 0))
    if not covered:
        return [{
            "title": "暂无可统计的有效评价",
            "body": f"原始记录 {total} 条，调用或输出失败 {failed} 条，待处理 {pending} 条。请先查看处理说明；没有有效结果时不计算好评、差评或优先级占比。",
            "evidence_rows": [],
        }]

    insights = [{
        "title": f"本次覆盖 {covered} / {total} 条评价",
        "body": f"有效覆盖率 {covered / total:.1%}；独立模型分析 {independent} 条，同文复用 {reused} 条。另有 {failed} 条调用或输出失败、{pending} 条待处理；未覆盖记录不进入分布比例。",
        "evidence_rows": [],
    }]
    negative = _count(metrics.get("negative_rows", 0))
    negative_examples = report["negative_examples"]
    insights.append({
        "title": f"识别到 {negative} 条负面评价" if negative else "有效评价中暂无负面结果",
        "body": f"负面评价占有效评价的 {negative / covered:.1%}。该比例包含明确标记的同文复用记录，表示本次表格的原记录分布，不代表全部顾客或业务整体表现。",
        "evidence_rows": _row_numbers(negative_examples, report),
    })

    ranking = report["problem_ranking"] if negative else report["category_counts"]
    if not ranking.empty and _count(ranking["数量"].max()):
        maximum = _count(ranking["数量"].max())
        leaders = ranking.loc[ranking["数量"].eq(maximum), "问题类别"].astype(str).tolist()
        label = "、".join(leaders)
        category_column = report.get("detail_columns", {}).get("category")
        evidence = negative_examples if negative else report["detail"]
        if category_column in evidence.columns:
            evidence = evidence.loc[evidence[category_column].isin(leaders)]
        insights.append({
            "title": ("差评主要涉及：" if negative else "评价主类别以这些最多：") + label,
            "body": f"{'这些类别各有' if len(leaders) > 1 else '该类别有'} {maximum} 条{'负面' if negative else '有效'}评价。这里按固定类别计数，每条原记录只归入一个主类别；不是自动主题聚类，也不代表已核实的故障原因。",
            "evidence_rows": _row_numbers(evidence, report),
        })

    high = _count(metrics.get("high_priority_rows", 0))
    priority_column = report.get("detail_columns", {}).get("priority")
    evidence = report["detail"]
    if priority_column in evidence.columns:
        evidence = evidence.loc[evidence[priority_column].eq("高")]
    insights.append({
        "title": f"先复核 {high} 条高优先级评价" if high else "有效评价中暂无高优先级结果",
        "body": "先回看原文，核对影响范围、是否阻断使用以及是否有可行绕行，再决定处理顺序。优先级是自动分析线索，不能替代业务核实。",
        "evidence_rows": _row_numbers(evidence, report) if high else [],
    })
    return insights


def _svg_start(identifier: str, title: str, description: str, width: int, height: int) -> str:
    # Identifiers and dimensions are supplied only by this module; user text
    # enters escaped title/description/text nodes, never SVG markup or styles.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="{identifier}-title {identifier}-description">'
        f'<title id="{identifier}-title">{escape(title)}</title>'
        f'<desc id="{identifier}-description">{escape(description)}</desc>'
    )


def sentiment_donut_svg(metrics: dict) -> str:
    total = _count(metrics["success_rows"])
    values = [
        ("正面", _count(metrics["positive_rows"]), POSITIVE),
        ("负面", _count(metrics["negative_rows"]), NEGATIVE),
        ("中性", _count(metrics["neutral_rows"]), NEUTRAL),
    ]
    description = "；".join(f"{label}{count}条" for label, count, _ in values)
    content = [_svg_start("sentiment", "顾客情感分布", description, 280, 250)]
    circumference = 2 * math.pi * 82
    content.append(f'<circle cx="140" cy="118" r="82" fill="none" stroke="{TRACK}" stroke-width="24"/>')
    offset = 0.0
    if total:
        for _, count, color in values:
            length = circumference * count / total
            if count:
                content.append(
                    f'<circle cx="140" cy="118" r="82" fill="none" stroke="{color}" stroke-width="24" '
                    f'stroke-dasharray="{length:.4f} {circumference - length:.4f}" '
                    f'stroke-dashoffset="{-offset:.4f}" transform="rotate(-90 140 118)"/>'
                )
            offset += length
    value = f"{_count(metrics['negative_rows']) / total:.1%}" if total else "—"
    label = "负面评价占比" if total else "暂无有效评价"
    content.extend([
        f'<text x="140" y="119" text-anchor="middle" fill="{INK}" font-size="31" font-weight="700">{value}</text>',
        f'<text x="140" y="144" text-anchor="middle" fill="#526779" font-size="13">{label}</text>',
        f'<text x="140" y="239" text-anchor="middle" fill="#526779" font-size="12">有效分母 {total} 条 · 包含已标明的复用记录</text>',
        "</svg>",
    ])
    return "".join(content)


def horizontal_bars_svg(rows, *, identifier, title, denominator, color=TEAL) -> str:
    rows = [(str(label), _count(count)) for label, count in rows]
    denominator = _count(denominator)
    height = max(74, 46 * len(rows) + 12)
    description = "；".join(f"{label}{count}条" for label, count in rows)
    result = [_svg_start(identifier, title, description or "暂无可统计的评价", 600, height)]
    if not rows:
        result.append('<text x="0" y="32" fill="#526779" font-size="15">暂无可统计的评价</text>')
    for index, (label, count) in enumerate(rows):
        y = index * 46 + 18
        label_display = label if len(label) <= 26 else label[:25] + "…"
        fraction = min(1.0, count / denominator) if denominator else 0.0
        result.extend([
            f'<text x="0" y="{y}" fill="{INK}" font-size="14">{escape(label_display)}</text>',
            f'<rect x="0" y="{y + 9}" width="470" height="10" rx="3" fill="{TRACK}"/>',
        ])
        if count and denominator:
            result.append(f'<rect x="0" y="{y + 9}" width="{470 * fraction:.3f}" height="10" rx="3" fill="{color}"/>')
        value = f"{count} 条 · {fraction:.1%}" if denominator else "0 条 · 暂无分母"
        result.append(f'<text x="597" y="{y + 18}" text-anchor="end" fill="#526779" font-size="13">{value}</text>')
    result.append("</svg>")
    return "".join(result)


def group_comparison_svg(frame) -> str:
    shown = frame.head(8)
    height = max(74, len(shown) * 52 + 12)
    description = "；".join(
        f"{_text(row['分组'])}：负面{_count(row['负面评价数'])}条，有效{_count(row['成功分析数'])}条"
        for row in shown.to_dict(orient="records")
    )
    result = [_svg_start("groups", "产品或门店负面评价对比", description, 660, height)]
    for index, row in enumerate(shown.to_dict(orient="records")):
        label = _text(row["分组"])
        visible_label = label if len(label) <= 32 else label[:31] + "…"
        covered, negative = _count(row["成功分析数"]), _count(row["负面评价数"])
        rate = min(1.0, negative / covered) if covered else 0.0
        y = index * 52 + 18
        value = f"{rate:.1%} · {negative}/{covered} 条" if covered else "暂无有效评价"
        result.extend([
            f'<text x="0" y="{y}" fill="{INK}" font-size="14">{escape(visible_label)}</text>',
            f'<rect x="0" y="{y + 10}" width="485" height="11" rx="3" fill="{TRACK}"/>',
            f'<rect x="0" y="{y + 10}" width="{485 * rate:.3f}" height="11" rx="3" fill="{NEGATIVE}"/>',
            f'<text x="657" y="{y + 20}" text-anchor="end" fill="#526779" font-size="13">{value}</text>',
        ])
    result.append("</svg>")
    return "".join(result)


def _table(frame, *, ratio_columns=(), row_column=None, anchors=False) -> str:
    if frame.empty:
        return '<p class="empty-state">暂无符合条件的记录。</p>'
    head = "".join(f'<th scope="col">{escape(str(column))}</th>' for column in frame.columns)
    body = []
    for row in frame.to_dict(orient="records"):
        identifier = f' id="review-row-{int(row[row_column])}"' if anchors and row_column in row else ""
        cells = []
        for column in frame.columns:
            value = row[column]
            if column in ratio_columns:
                number = _number(value)
                text = f"{number:.1%}" if number is not None else "—"
            else:
                text = _text(value)
            cells.append(f"<td>{escape(text)}</td>")
        body.append(f"<tr{identifier}>" + "".join(cells) + "</tr>")
    return '<div class="table-wrap"><table><thead><tr>' + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _evidence_links(rows) -> str:
    return "".join(f'<a href="#review-row-{int(row)}">原始第 {int(row)} 行</a>' for row in rows)


def _metadata_html(report: dict) -> str:
    metadata = report.get("analysis_meta")
    if not isinstance(metadata, dict) or not metadata:
        return '<p class="section-note">未附本次请求记录；不根据有效评价数推算实际模型调用或用量。</p>'
    rows = []
    fields = [
        ("planned_requests", "计划请求数", " 次"), ("actual_requests", "实际请求数", " 次"),
        ("batch_size", "每次合并评价上限", " 条"), ("unique_pending_rows", "本次待分析的不同文本", " 条"),
        ("max_characters", "每次合并文本字符上限", " 字符"),
        ("total_tokens", "服务返回总用量", " token"),
        ("usage_reported_requests", "返回用量记录的请求", " 次"),
    ]
    for key, label, unit in fields:
        value = _number(metadata.get(key))
        if value is not None:
            rows.append((label, f"{int(value):,}{unit}"))
    elapsed = _number(metadata.get("elapsed_seconds"))
    if elapsed is not None:
        rows.append(("本轮处理耗时", f"{elapsed:.1f} 秒"))
    version = metadata.get("prompt_version")
    if isinstance(version, str) and version.strip():
        rows.append(("分析规则版本", version.strip()))
    if not rows:
        return '<p class="section-note">未提供可展示的请求记录。</p>'
    items = "".join(f"<div><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>" for label, value in rows)
    return '<dl class="run-metadata">' + items + '</dl><p class="section-note">请求数与有效评价数口径不同；同文复用不新增请求。服务用量不是费用，也不是分析准确率。</p>'


_TOPIC_KINDS = {"praise": "顾客认可", "issue": "具体问题", "request": "改进需求"}


def _topics_html(report: dict) -> str:
    topics = report.get("topics")
    if not isinstance(topics, list) or not topics:
        return ""
    base = _count(report.get("metrics", {}).get("success_rows"))
    detail, names = report.get("detail"), report.get("detail_columns", {})
    existing_rows = set()
    if isinstance(detail, pd.DataFrame) and names.get("source_row") in detail:
        existing_rows = {int(value) for value in detail[names["source_row"]].tolist()}
    content = ['<section id="review-topics"><h2>最值得关注的具体体验</h2>',
               '<p class="section-note">按问题、需求优先展示最多三个主题。每个主题每条原记录最多计一次，'
               '同文复用仍按原记录计数；一条评价可涉及多个方面，各主题占比之和可以超过 100%。'
               '认可、问题与需求分开统计，主题名称不代表已核实的故障或原因。</p><div class="evidence-grid">']
    ordered = sorted((item for item in topics if isinstance(item, dict)), key=lambda item: (
        0 if item.get("kind") in {"issue", "request"} else 1, -_count(item.get("review_count")), _text(item.get("topic"))))
    for item in ordered[:3]:
        count = _count(item.get("review_count"))
        label = _TOPIC_KINDS.get(item.get("kind"), "具体体验")
        content.append('<article class="evidence-item"><div class="evidence-meta">' + label + '</div><h3>'
                       + escape(_text(item.get("topic"))) + '</h3><p>'
                       + (f'{count} 条原记录 · 占有效评价 {count / base:.1%}（分母 {base} 条）' if base else '暂无有效评价分母')
                       + '</p>')
        for example in item.get("examples", [])[:3]:
            if not isinstance(example, dict):
                continue
            row = example.get("source_row")
            content.append('<blockquote>' + escape(_text(example.get("text"))) + '</blockquote>')
            if _text(example.get("group")):
                content.append('<p class="evidence-meta">分组：' + escape(_text(example.get("group"))) + '</p>')
            if type(row) is int and row in existing_rows:
                content.append('<div class="evidence-links">' + _evidence_links([row]) + '</div>')
        content.append('</article>')
    content.append('</div>')
    coverage = report.get("topic_coverage", {})
    if coverage.get("status") == "partial":
        content.append('<p class="section-note">主题覆盖 ' + str(_count(coverage.get("with_aspects_rows")))
                       + f' / {base} 条有效评价；其余记录没有可用的具体方面，不按大类补造主题。</p>')
    content.append('</section>')
    return ''.join(content)


def _historical_comparison_html(report: dict) -> str:
    context = report.get("historical_context")
    if not isinstance(context, dict) or not isinstance(context.get("comparison"), dict):
        return ""
    comparison = context["comparison"]
    batch = context.get("batch") if isinstance(context.get("batch"), dict) else {}
    filename = _text(batch.get("filename")) or "未命名批次"
    content = ['<section id="batch-comparison"><h2>与选定上批比较</h2><p>上批：<strong>'
               + escape(filename) + '</strong></p>',
               '<p class="section-note">这里比较两次导入批次；涉及上批的原始行均属于上批文件，不对应本报告明细中的同号行。</p>']
    bases = []
    for label, key in [("本批", "current_base"), ("上批", "previous_base")]:
        base = comparison.get(key) if isinstance(comparison.get(key), dict) else {}
        success = _count(base.get("success_rows"))
        bases.append(success)
        content.append('<p class="section-note">' + label + f'：原始记录 {_count(base.get("total_rows"))} 条，'
                       + f'有效评价 {success} 条，主题覆盖 {_count(base.get("topic_rows"))} 条。</p>')
    basic_comparable = comparison.get("basic_comparable") is True and all(bases)
    if basic_comparable:
        content.append('<h3>样本与覆盖</h3>')
        totals = [_count(comparison.get(key, {}).get("total_rows")) for key in ("current_base", "previous_base")]
        sample_rows = [
            {"指标": "原始记录", "本批": str(totals[0]), "上批": str(totals[1]), "占比变化（百分点）": "—"},
            {"指标": "有效评价", "本批": str(bases[0]), "上批": str(bases[1]), "占比变化（百分点）": "—"},
        ]
        if all(totals):
            shares = [base / total for base, total in zip(bases, totals)]
            sample_rows.append({"指标": "有效覆盖率", "本批": f"{shares[0]:.1%}", "上批": f"{shares[1]:.1%}",
                                "占比变化（百分点）": f"{(shares[0] - shares[1]) * 100:+.1f}"})
        content.append(_table(pd.DataFrame(sample_rows)))
        for key, label in (("sentiment", "情感分布"), ("category", "评价类别"), ("priority", "优先级")):
            rows = []
            values = comparison.get("distributions", {}).get(key, [])
            for item in values:
                if not isinstance(item, dict):
                    continue
                now, before = _count(item.get("current_count")), _count(item.get("previous_count"))
                now_share, before_share = now / bases[0], before / bases[1]
                rows.append({"项目": _text(item.get("label")), "本批条数": now, "上批条数": before,
                             "本批占比": now_share, "上批占比": before_share,
                             "占比变化（百分点）": f"{(now_share - before_share) * 100:+.1f}"})
            if rows:
                content.append('<h3>' + label + '</h3>')
                content.append(_table(pd.DataFrame(rows), ratio_columns=("本批占比", "上批占比")))
        content.append('<p class="section-note">情感、类别和优先级占比分别以各批有效评价为分母；覆盖率以各批原始记录为分母。</p>')
    else:
        content.append('<p class="status-note">两批基础统计暂不能直接比较。</p>')
        for reason in comparison.get("basic_reasons", []):
            content.append('<p class="section-note">' + escape(_text(reason)) + '</p>')
    content.append('<h3>具体主题</h3>')
    topics_comparable = comparison.get("topics_comparable", comparison.get("comparable")) is True and all(bases)
    if not topics_comparable:
        content.append('<p class="status-note">具体主题不能直接比较，不生成主题涨跌结论。'
                       + ('基础统计已在上面独立展示。' if basic_comparable else '') + '</p>')
        for reason in comparison.get("topic_reasons", comparison.get("reasons", [])):
            content.append('<p class="section-note">' + escape(_text(reason)) + '</p>')
    else:
        changes = []
        for item in comparison.get("topics", []):
            if not isinstance(item, dict):
                continue
            now, before = _count(item.get("current_count")), _count(item.get("previous_count"))
            now_share, before_share = now / bases[0], before / bases[1]
            delta = (now_share - before_share) * 100
            changes.append({"具体主题": _text(item.get("topic")), "方面": _TOPIC_KINDS.get(item.get("kind"), "具体体验"),
                            "本批原记录数": now, "上批原记录数": before,
                            "本批占有效评价": now_share, "上批占有效评价": before_share,
                            "占比变化（百分点）": f"{delta:+.1f}" if abs(delta) >= 0.05 else "0.0"})
        if changes:
            content.append(_table(pd.DataFrame(changes), ratio_columns=("本批占有效评价", "上批占有效评价")))
        else:
            content.append('<p class="empty-state">没有可展示的主题变化记录。</p>')
    for warning in comparison.get("warnings", []):
        content.append('<p class="section-note">' + escape(_text(warning)) + '</p>')
    content.append('<p class="section-note">这里比较两次导入批次的差异，不代表时间趋势。评价中的变化仅是观察线索，不能直接认定问题已解决，也不能证明某项经营措施的效果。</p>')
    historical_examples = [item for item in context.get("evidence", []) if isinstance(item, dict)][:3]
    if historical_examples:
        content.append('<details><summary>核对选定上批的原文示例</summary><div class="evidence-grid">')
        for item in historical_examples:
            row = item.get("source_row")
            row_label = f'原始第 {row} 行' if type(row) is int and row >= 2 else '原始行号未记录'
            content.append('<article class="evidence-item"><p class="evidence-meta">上批《' + escape(filename)
                           + '》' + row_label + '</p><blockquote>' + escape(_text(item.get("text"))) + '</blockquote>')
            if _text(item.get("group")):
                content.append('<p class="evidence-meta">分组：' + escape(_text(item.get("group"))) + '</p>')
            content.append('</article>')
        content.append('</div></details>')
    content.append('</section>')
    return ''.join(content)


REPORT_STYLE = """
:root{--ink:#15334B;--teal:#087F8C;--muted:#526779;--line:#DDE6EE;--paper:#FFF;--back:#F4F7FB;--negative:#C15D4F}
*{box-sizing:border-box}html{scroll-behavior:auto}body{margin:0;background:var(--back);color:var(--ink);font:15px/1.7 'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif}main{max-width:1180px;margin:28px auto;padding:44px 48px;background:var(--paper);border:1px solid var(--line)}a{color:var(--teal);text-decoration-thickness:1px;text-underline-offset:3px}a:focus-visible{outline:3px solid var(--teal);outline-offset:4px}h1{font-size:34px;line-height:1.3;letter-spacing:-.025em;margin:12px 0 14px}h2{font-size:23px;line-height:1.4;margin:0 0 12px}h3{font-size:17px;line-height:1.5;margin:0 0 8px}p{margin:0 0 12px;max-width:76ch}.brand{font-size:14px;font-weight:700;color:var(--teal)}.lead{color:var(--muted);font-size:16px}.masthead{padding-bottom:25px;border-bottom:3px solid var(--ink)}.report-nav{display:flex;gap:10px 24px;flex-wrap:wrap;padding:16px 0;border-bottom:1px solid var(--line);margin-bottom:30px}.report-nav a{text-decoration:none;font-size:14px}section{margin-top:36px;scroll-margin-top:16px}.section-note{font-size:13px;color:var(--muted);max-width:86ch}.overview-grid{display:grid;grid-template-columns:1fr 1fr;column-gap:32px;row-gap:20px}.insight{padding:0 0 16px;border-bottom:1px solid var(--line)}.insight h3{font-size:17px}.insight p{font-size:14px}.evidence-links{display:flex;gap:8px 16px;flex-wrap:wrap;font-size:12px}.coverage-strip{display:flex;flex-wrap:wrap;gap:8px 28px;margin-top:24px;padding:20px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.coverage-strip>div{min-width:90px}.coverage-strip dt{font-size:12px;color:var(--muted)}.coverage-strip dd{margin:1px 0 0;font-size:25px;font-weight:700;font-variant-numeric:tabular-nums}.coverage-strip .muted-value{color:var(--muted)}.status-note{padding:13px 16px;background:#F9EEEA;border-left:3px solid var(--negative);font-size:14px}.chart-grid{display:grid;grid-template-columns:minmax(270px,.8fr) minmax(360px,1.2fr);gap:32px;align-items:start}.chart-panel{padding-top:18px;border-top:2px solid var(--line)}.chart-panel svg{display:block;width:100%;height:auto;font-family:'Microsoft YaHei','PingFang SC',sans-serif}.sentiment-chart svg{max-width:320px;margin:0 auto}.chart-legend{list-style:none;padding:0;margin:10px 0;display:flex;gap:12px 20px;justify-content:center;flex-wrap:wrap;font-size:13px}.chart-legend li{display:flex;align-items:center;gap:7px}.legend-dot{width:10px;height:10px;border-radius:50%;display:inline-block}.priority-panel{margin-top:20px;max-width:720px}.narrow-chart{max-width:760px}.empty-state{padding:18px 0;color:var(--muted);font-size:14px}.table-wrap{max-width:100%;overflow:auto;border:1px solid var(--line);border-radius:5px;margin:14px 0}table{border-collapse:collapse;width:100%;font-size:12px;line-height:1.6}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere;min-width:78px;max-width:420px}th{background:#F0F5F8;font-weight:600}tbody tr:last-child td{border-bottom:0}tbody tr:nth-child(even){background:#FAFCFD}tr:target{outline:3px solid var(--teal);outline-offset:-3px;background:#E8F5F4}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.evidence-item{border-left:3px solid var(--line);padding-left:16px;min-width:0}.evidence-item blockquote{font-size:15px;line-height:1.8;margin:10px 0;white-space:pre-wrap;overflow-wrap:anywhere}.evidence-item .evidence-meta{font-size:12px;color:var(--muted);margin-bottom:8px}.evidence-summary{font-size:13px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere}.run-metadata{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 24px;margin:18px 0}.run-metadata dt{font-size:12px;color:var(--muted)}.run-metadata dd{margin:2px 0 0;font-weight:600;overflow-wrap:anywhere}.methodology{padding-top:24px;border-top:2px solid var(--line)}.methodology li{font-size:13px;margin-bottom:9px;max-width:94ch}.report-footer{font-size:12px;color:var(--muted);border-top:1px solid var(--line);margin-top:32px;padding-top:14px}.appendix table{font-size:11px}.appendix th,.appendix td{min-width:90px}.print-records{display:none}.record dl{display:grid;grid-template-columns:1fr 2fr;gap:5px 12px}.record dt{font-weight:600}.record dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.executive-summary{padding:18px 22px;margin:0 0 26px;border-left:4px solid var(--teal);background:#EDF6F6;break-inside:avoid-page}.executive-summary h3{color:var(--teal);margin-bottom:8px}.executive-summary p{font-size:16px;line-height:1.9;margin:0;max-width:none}
@media(max-width:760px){main{margin:0;padding:26px 20px;border:0}h1{font-size:27px}h2{font-size:21px}.overview-grid,.chart-grid,.evidence-grid{grid-template-columns:1fr}.chart-grid{gap:24px}.coverage-strip{gap:12px 20px}.run-metadata{grid-template-columns:1fr 1fr}.report-nav{gap:9px 18px}.chart-panel{min-width:0}}
@page{size:A4;margin:15mm}@page appendix{size:A4 landscape;margin:12mm}
@media print{body{background:#FFF;color:#15334B;font-size:10pt}main{max-width:none;border:0;margin:0;padding:0}.report-nav{display:none}h1{font-size:25pt}h2{font-size:16pt;break-after:avoid}h3{font-size:12pt;break-after:avoid}.lead{font-size:11pt}section{margin-top:20pt}.chart-grid{grid-template-columns:1fr 1.2fr;gap:20pt}.overview-grid,.evidence-grid{gap:12pt 20pt}.chart-panel,.insight,.evidence-item{break-inside:avoid-page}svg{max-height:210mm}.table-wrap{overflow:visible;border-radius:0}table{table-layout:fixed;font-size:8pt;width:100%}th,td{min-width:0!important;max-width:none;padding:5pt 4pt;word-break:break-word}thead{display:table-header-group}tr{break-inside:auto}a{color:inherit;text-decoration:none}.appendix{page:appendix;break-before:page}.appendix table{font-size:7.5pt}.appendix.wide .table-wrap{display:none}.appendix.wide .print-records{display:block}.record{padding:12pt 0;border-top:1px solid #DDE6EE}.record dl{font-size:9pt}.report-footer{font-size:8pt}.section-note{font-size:9pt}.run-metadata{grid-template-columns:repeat(3,1fr)}.status-note,.chart-legend,.coverage-strip,th{print-color-adjust:exact;-webkit-print-color-adjust:exact}}
"""


def render_customer_html(report: dict) -> bytes:
    """Render a portable Chinese business report without network or scripts."""
    metrics = report["metrics"]
    synthetic = report.get("data_origin") == "synthetic" or report.get("provenance", {}).get("data_origin") == "synthetic"
    total, covered = _count(metrics["total_rows"]), _count(metrics["success_rows"])
    reused = _count(metrics.get("reused_rows", 0))
    independent = _count(metrics.get("model_success_rows", covered - reused))
    negative, high = _count(metrics.get("negative_rows", 0)), _count(metrics.get("high_priority_rows", 0))
    failed, pending = _count(metrics.get("failed_rows", 0)), _count(metrics.get("pending_rows", 0))
    navigation = [
        ("overview", "本次结论"), ("distribution", "评价分布"), ("issues", "差评问题"),
        ("groups", "产品或门店"), ("evidence", "原文证据"), ("methodology", "口径与记录"), ("details", "全部明细"),
    ]
    topic_html = _topics_html(report)
    historical_html = _historical_comparison_html(report)
    if historical_html:
        navigation.insert(1, ("batch-comparison", "上批比较"))
    if topic_html:
        navigation.insert(1, ("review-topics", "具体体验"))
    content = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'none\'; connect-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'; form-action \'none\'">',
        '<title>顾客评价分析报告 | CustomerInsight</title><style>' + REPORT_STYLE + '</style></head><body><main>',
        '<header class="masthead"><div class="brand">CustomerInsight</div><h1>顾客评价分析报告</h1>',
        f'<p class="lead">从 {total} 条原始评价出发，查看顾客感受、需要优先复核的问题，以及每项判断对应的原文。</p>',
        '<p class="section-note">本文件可以离线打开，也可使用浏览器打印或另存为 PDF。自动分析用于筛查，重要事项请回到原文核实。</p></header>',
        '<nav class="report-nav" aria-label="报告目录">' + ''.join(f'<a href="#{identifier}">{title}</a>' for identifier, title in navigation) + '</nav>',
        '<section id="overview"><h2>先看本次结论</h2>',
        '<aside class="executive-summary" aria-label="一句话总结"><h3>一句话总结</h3><p>'
        + escape(build_report_summary(report)) + '</p></aside><div class="overview-grid">',
    ]
    if synthetic:
        content.insert(-1, '<p class="status-note">合成示例数据：评价及分析标签由开发者预置，统计由程序计算；未对这些标签调用模型，不能代表真实店铺或模型准确率。</p>')
    for insight in build_report_insights(report):
        content.append('<article class="insight"><h3>' + escape(insight["title"]) + '</h3><p>' + escape(insight["body"]) + '</p><div class="evidence-links">' + _evidence_links(insight["evidence_rows"]) + '</div></article>')
    content.append('</div><dl class="coverage-strip">')
    for label, value in [
        ("原始评价", total), ("有效覆盖", covered), ("预置标签记录" if synthetic else "独立模型分析", independent),
        ("同文复用", reused), ("未覆盖", max(0, total - covered)), ("调用或输出失败", failed),
    ]:
        content.append(f'<div><dt>{label}</dt><dd>{value:,}<small> 条</small></dd></div>')
    content.append('</dl>')
    if failed or pending:
        content.append(f'<p class="status-note">本报告有 {failed} 条调用或输出失败、{pending} 条待处理。分布仅反映有效覆盖的 {covered} 条评价，未处理记录不算作好评、中性或其他类别。</p>')
    if not covered:
        content.append('<p class="empty-state">暂无符合条件的有效分析结果。图中保留空分布，不把失败记为 0% 差评；请在完整明细中查看处理说明。</p>')
    content.append('</section>' + topic_html + historical_html + '<section id="distribution"><h2>顾客如何评价</h2><p class="section-note">情感、类别与优先级以有效覆盖评价为分母，同文复用按原记录分别计数。类别为固定标签统计，包含表扬与建议，不是自动聚类。</p><div class="chart-grid">')
    content.append('<article class="chart-panel sentiment-chart"><h3>情感分布</h3>' + sentiment_donut_svg(metrics) + '<ul class="chart-legend">')
    for label, key, color in [("正面", "positive_rows", POSITIVE), ("负面", "negative_rows", NEGATIVE), ("中性", "neutral_rows", NEUTRAL)]:
        content.append(f'<li><span class="legend-dot" style="background:{color}"></span>{label} {_count(metrics.get(key, 0))} 条</li>')
    content.append('</ul></article><article class="chart-panel"><h3>评价主类别</h3>')
    content.append(horizontal_bars_svg(zip(report["category_counts"]["问题类别"], report["category_counts"]["数量"]), identifier="categories", title="评价主类别分布", denominator=covered))
    content.append('</article></div><article class="chart-panel priority-panel"><h3>处理优先级</h3>')
    content.append(horizontal_bars_svg(zip(report["priority_counts"]["优先级"], report["priority_counts"]["数量"]), identifier="priorities", title="处理优先级分布", denominator=covered, color=INK))
    content.append('</article></section><section id="issues"><h2>差评主要涉及哪些问题</h2><p class="section-note">仅统计负面评价的主类别，每条原记录计一次。优先看出现数量，再结合原文判断；类别名称不表示已确认的原因。</p>')
    problems = report["problem_ranking"].loc[report["problem_ranking"]["数量"].gt(0)]
    if negative:
        content.append('<div class="chart-panel narrow-chart">' + horizontal_bars_svg(zip(problems["问题类别"], problems["数量"]), identifier="issues-chart", title="负面评价问题类别", denominator=negative, color=NEGATIVE) + '</div>')
        content.append(_table(problems, ratio_columns=("占负面评价比例",)))
    else:
        content.append('<p class="empty-state">有效评价中暂无负面结果，暂不生成差评问题排序。</p>' if covered else '<p class="empty-state">暂无有效评价，暂不生成差评问题排序。</p>')
    content.append('</section><section id="groups"><h2>产品或门店对比</h2>')
    grouped = report["grouped"]
    if grouped.empty:
        content.append('<p class="empty-state">本次没有可展示的产品或门店分组；上传分析时可选择分组列。</p>')
    else:
        content.append('<p class="section-note">按负面评价数量排序，图中展示前 8 组，完整分组保留在表格中。横条为组内负面占比；分母是该组有效覆盖数，包含标明的复用记录。不同组样本量不同时不宜直接判断服务优劣。</p><div class="chart-panel narrow-chart">' + group_comparison_svg(grouped) + '</div>')
        content.append(_table(grouped, ratio_columns=("负面占比", "高优先级占比")))
    content.append('</section><section id="evidence"><h2>回到差评原文</h2>')
    examples = report["negative_examples"]
    names = report.get("detail_columns", {})
    if examples.empty:
        content.append('<p class="empty-state">暂无符合条件的记录。所有原文和处理状态仍保留在完整明细。</p>')
    else:
        content.append(f'<p class="section-note">按优先级先展示 {min(6, len(examples))} / {len(examples)} 条负面评价。摘要是自动整理的阅读线索，原文才是复核依据；全部评价见完整明细。</p><div class="evidence-grid">')
        for row in examples.head(6).to_dict(orient="records"):
            number = int(row[names["source_row"]])
            source_note = _text(row.get(names.get("result_source"), ""))
            category = _text(row.get(names.get("category"), ""))
            priority = _text(row.get(names.get("priority"), ""))
            summary = _text(row.get(names.get("summary"), ""))
            content.extend([
                '<article class="evidence-item"><div class="evidence-meta">' + escape(f"{category} · {priority}优先级") + '</div>',
                '<blockquote>' + escape(_text(row.get(report["text_column"], ""))) + '</blockquote>',
                '<p class="evidence-summary">分析摘要：' + escape(summary) + '</p>',
                '<p class="evidence-meta">' + escape(source_note) + '</p>',
                '<div class="evidence-links">' + _evidence_links([number]) + '</div></article>',
            ])
        content.append('</div>')
    notes = ''.join('<li>' + escape(str(note)) + '</li>' for note in report.get("notes", []))
    content.append('</section><section id="methodology" class="methodology"><h2>统计口径与分析记录</h2><ul>' + notes + '</ul><h3>本次分析记录</h3>' + _metadata_html(report) + '</section>')
    detail = report["detail"]
    wide = len(detail.columns) > 18
    content.append('<section id="details" class="appendix' + (' wide' if wide else '') + '"><h2>完整评价明细</h2><p class="section-note">保留上传的全部字段、原始行号、独立分析或复用来源、处理状态、摘要及失败原因。宽表可横向滚动；CSV 明细适合在表格软件中继续筛选。</p>')
    content.append(_table(detail, row_column=names.get("source_row"), anchors=True))
    if wide:
        content.append('<div class="print-records">')
        for row in detail.to_dict(orient="records"):
            number = int(row[names["source_row"]])
            content.append(f'<article class="record"><h3>原始第 {number} 行</h3><dl>')
            for column, value in row.items():
                content.append('<dt>' + escape(str(column)) + '</dt><dd>' + escape(_text(value)) + '</dd>')
            content.append('</dl></article>')
        content.append('</div>')
    scope = '本报告使用本次表格和明确选定的上批分析结果。' if historical_html else '本报告仅使用本次表格的分析结果。'
    content.append('</section><footer class="report-footer">CustomerInsight · ' + scope + '无外部资源、无脚本，支持离线阅读和打印。完整来源与开源许可证见项目说明。</footer></main></body></html>')
    return ''.join(content).encode("utf-8")
