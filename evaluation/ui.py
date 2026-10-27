"""A local evaluation dashboard; model calls happen only on an explicit click."""

from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from ai.batch import describe_error
from ai.prompts import available_prompt_versions
from core.config import Config
from evaluation.data import DEFAULT_DATASET, load_dataset, select_split
from evaluation.reports import (
    DEFAULT_REPORT_DIR, load_report, metrics_json, predictions_csv,
    report_markdown, save_report,
)
from evaluation.runner import compare_reports, run_evaluation
from evaluation.control import RunControl
from evaluation.review import load_review_ledger
from evaluation.review_ui import render_review


FIELD_LABELS = {"sentiment": "情感", "category": "类别", "priority": "优先级"}


def saved_report_files(directory: Path | None = None) -> list[Path]:
    """Offer validated report names only inside the configured local directory."""
    root = Path(DEFAULT_REPORT_DIR if directory is None else directory).resolve()
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.rglob("*.report.json")
         if path.is_file() and path.resolve().is_relative_to(root)),
        key=lambda path: path.name,
        reverse=True,
    )


def load_saved_report(filename: str, directory: Path | None = None) -> dict:
    """Do not accept an arbitrary path from a widget or stored session state."""
    root = Path(DEFAULT_REPORT_DIR if directory is None else directory).resolve()
    available = {path.relative_to(root).as_posix(): path for path in saved_report_files(directory)}
    if filename not in available:
        raise ValueError("请选择报告目录内有效的已保存报告。")
    return load_report(available[filename])


def request_scale(sample_count: int, version_count: int, *, mode: str, max_retries: int = 0) -> int:
    """Maximum application attempts, including its bounded retries."""
    return 0 if mode == "demo" else sample_count * version_count * (max_retries + 1)


def _render_report(report: dict) -> None:
    metadata = report["metadata"]
    metrics = report["metrics"]
    mode = metadata["mode"]
    if mode == "demo":
        st.info("DEMO：确定性 Mock 合成离线演示。预测故意包含错误和失败；耗时不代表真实模型性能。")
    else:
        st.info("LIVE：真实接口评测；本报告的请求设置与运行时间见追溯信息。")
    if not metadata.get("all_labels_reviewed", False):
        st.warning("临时结果：金标包含待人工复核的合成草稿，不能作为模型准确率结论。")
    if metadata.get("review_status_counts", {}).get("ai_reviewed", 0):
        st.info("参考标签包含 AI 复核；报告仅衡量与这些参考标签的一致性，不是全人工金标上的准确率。")
    if metadata.get("run_status") == "incomplete":
        st.error("运行未完成：下方仅为已执行样本的证据指标，不能当作完整数据集评测。")
        st.write(f"尚未执行 {metadata['not_executed_count']} 条；停止原因：{metadata['stop_reason']}。")
    if "request_attempts_actual" in metadata:
        st.caption(f"本报告实际请求尝试 {metadata['request_attempts_actual']}；阶段累计 {metadata['cumulative_request_attempts']} / 180。Token usage 见逐样本记录；不可用时不估算费用。")
    st.caption(
        f"运行 {metadata['run_id']} · Prompt {metadata['prompt_version']} · "
        f"数据集划分 {metadata['split']} · 模型 {metadata['model']}"
    )
    total, success, failure, rate = st.columns(4)
    total.metric("评测样本", metrics["sample_count"])
    success.metric("结构化成功", metrics["success_count"])
    failure.metric("无有效预测", metrics["failed_count"])
    rate.metric("结构化成功率", f"{metrics['structured_success_rate']:.1%}")
    latency = metrics["latency"]
    average, p95 = st.columns(2)
    average.metric("平均耗时（ms）", f"{latency['average_ms']:.2f}")
    p95.metric("P95 耗时（ms）", f"{latency['p95_ms']:.2f}")
    st.caption(latency["measurement"])
    st.caption(
        "主指标覆盖全部有效样本：调用或结构校验失败均计入分母并算错误。"
        "Macro-F1 使用枚举的完整固定标签集合；零分母取 0。失败列表示无有效预测。"
    )
    st.subheader("失败统计")
    failure_rows = [
        {"状态": status, "数量": count}
        for status, count in metrics["failures_by_status"].items()
    ]
    if failure_rows:
        st.dataframe(pd.DataFrame(failure_rows), hide_index=True, width="stretch")
    else:
        st.write("没有调用失败或结构校验失败。")
    for field, label in FIELD_LABELS.items():
        field_metrics = metrics[field]
        with st.expander(f"{label}：分类指标与混淆矩阵", expanded=field == "sentiment"):
            accuracy_col, f1_col = st.columns(2)
            accuracy_col.metric(f"{label} Accuracy", f"{field_metrics['accuracy']:.3f}")
            f1_col.metric(f"{label} Macro-F1", f"{field_metrics['macro_f1']:.3f}")
            per_class = pd.DataFrame.from_dict(field_metrics["per_class"], orient="index")
            per_class.index.name = "标签"
            st.dataframe(per_class, width="stretch")
            confusion = field_metrics["confusion_matrix"]
            frame = pd.DataFrame(
                confusion["values"], index=confusion["gold_labels"],
                columns=confusion["prediction_labels"],
            )
            frame.index.name = "Gold ↓ / 预测 →"
            st.dataframe(frame, width="stretch")
    st.subheader("可核验的错误明细")
    st.caption("错误类型仅标识情感、类别、优先级错误或调用／结构校验失败，不推测根因。")
    errors = report.get("errors", [])
    if errors:
        st.dataframe(pd.DataFrame(errors), hide_index=True, width="stretch")
    else:
        st.write("本次没有错误样本。")
    with st.expander("逐样本预测与本地金标"):
        st.dataframe(pd.DataFrame(report["predictions"]), hide_index=True, width="stretch")
    with st.expander("运行追溯信息（不包含密钥）"):
        st.json(metadata)
    run_id = metadata["run_id"]
    md_col, json_col, csv_col = st.columns(3)
    with md_col:
        st.download_button(
            "下载评测 Markdown 报告", report_markdown(report),
            file_name=f"{run_id}.md", mime="text/markdown", key=f"eval_download_md_{run_id}",
        )
    with json_col:
        st.download_button(
            "下载评测指标 JSON", metrics_json(report),
            file_name=f"{run_id}.metrics.json", mime="application/json", key=f"eval_download_json_{run_id}",
        )
    with csv_col:
        st.download_button(
            "下载逐样本预测 CSV", predictions_csv(report),
            file_name=f"{run_id}.predictions.csv", mime="text/csv", key=f"eval_download_csv_{run_id}",
        )


def render_reports(reports: list[dict]) -> None:
    """Render retained reports; this function never invokes the analyzer."""
    if not reports:
        return
    if len(reports) > 1:
        st.subheader("Prompt 指标对比")
        try:
            comparison = compare_reports(reports)
            st.caption(comparison["interpretation"])
            st.dataframe(pd.DataFrame(comparison["rows"]), hide_index=True, width="stretch")
            st.caption("仅在数据集、划分、模型、模式及请求设置相同的运行间比较；v2 允许持平或下降。")
        except ValueError as error:
            st.error(f"这些报告不能比较：{error}")
    else:
        st.subheader("评测指标")
    choices = list(range(len(reports)))
    selected = st.selectbox(
        "查看运行详情", choices,
        format_func=lambda index: (
            f"{reports[index]['metadata']['prompt_version']} · "
            f"{reports[index]['metadata']['mode'].upper()} · "
            f"{reports[index]['metadata']['run_id']}"
        ),
        key="eval_report_detail",
    )
    _render_report(reports[selected])


def _render_saved_reports() -> None:
    """Reading past results is independent of a usable current dataset or Key."""
    st.subheader("读取本地已保存报告")
    paths = saved_report_files()
    if paths:
        names = [path.relative_to(Path(DEFAULT_REPORT_DIR).resolve()).as_posix() for path in paths]
        selected_names = st.multiselect(
            "选择本地报告（最多两个）", names, max_selections=2, key="eval_saved_reports",
        )
        if st.button("读取选定报告", key="eval_load_reports", disabled=not selected_names):
            try:
                st.session_state["evaluation_reports"] = [load_saved_report(name) for name in selected_names]
                st.session_state.pop("eval_report_detail", None)
                st.success("已读取本地报告；没有调用模型。")
            except (ValueError, OSError) as error:
                st.error(f"报告读取失败：{error}")
    else:
        st.info("报告目录中暂无已保存报告。可先运行 DEMO 离线演示。")
    render_reports(st.session_state.get("evaluation_reports", []))


def render_evaluation() -> None:
    st.subheader("自动化评测")
    if render_review(DEFAULT_DATASET):
        return
    st.caption(
        "内置数据是 60 条合成示例，固定为 40 条开发集、20 条测试集；复核来源和数量见下方实际状态统计。"
        "Prompt 调整仅使用开发集；测试集及近似改写不得用于 few-shot。金标、split 和 review_status 仅本地使用。"
    )
    try:
        samples = load_dataset(DEFAULT_DATASET)
    except (ValueError, OSError) as error:
        st.error(f"评测数据校验失败：{error}")
        _render_saved_reports()
        return
    split = st.selectbox(
        "评测数据划分", ["dev", "test"],
        format_func=lambda value: "开发集（dev）" if value == "dev" else "测试集（test）",
        key="eval_split",
    )
    version_choice = st.selectbox(
        "评测 Prompt 版本", [*available_prompt_versions(), "v1 与 v2 对比"], key="eval_prompt_version",
    )
    mode_label = st.radio("运行模式", ["DEMO 离线演示", "LIVE 真实接口"], key="eval_mode")
    mode = "demo" if mode_label.startswith("DEMO") else "live"
    concurrency = st.slider(
        "评测并发数", 1, Config.MAX_WORKERS, 2, key="eval_workers",
    )
    split_samples = sorted(select_split(samples, split), key=lambda row: row.sample_id)
    selection_mode = st.selectbox("样本选择", ["全部", "按 sample_id", "限制样本数"], key="eval_selection_mode")
    sample_ids, limit = None, None
    if selection_mode == "按 sample_id":
        sample_ids = st.multiselect("本次 sample_id（按 ID 稳定排序）", [row.sample_id for row in split_samples], key="eval_sample_ids")
        selected_samples = [row for row in split_samples if row.sample_id in sample_ids]
    elif selection_mode == "限制样本数":
        limit = st.number_input("样本数（取 ID 排序的前 N 条）", 1, len(split_samples), min(5, len(split_samples)), key="eval_limit")
        selected_samples = split_samples[:limit]
    else:
        selected_samples = split_samples
    retries = st.number_input("本次应用重试次数（不修改 .env）", 0, 5, 0, key="eval_retries")
    hard_limit = st.number_input("本次真实请求尝试硬上限（所有版本合计）", 1, 180, 10, key="eval_max_attempts")
    versions = ["v1", "v2"] if version_choice == "v1 与 v2 对比" else [version_choice]
    statuses = Counter(sample.review_status for sample in selected_samples)
    st.write(
        f"本次 {len(selected_samples)} 条样本 × {len(versions)} 个 Prompt 版本 = "
        f"{len(selected_samples) * len(versions)} 次分析；"
        f"待复核 {statuses.get('draft', 0)} 条，人工确认 {statuses.get('reviewed', 0)} 条，AI 复核 {statuses.get('ai_reviewed', 0)} 条。"
    )
    scale = request_scale(len(selected_samples), len(versions), mode=mode, max_retries=retries)
    if mode == "demo":
        st.caption("DEMO 不调用真实接口，真实请求规模为 0；固定 Mock 预测验证失败、指标和导出流程。")
        button_label = "运行 DEMO 离线评测"
    else:
        st.caption(
            f"理论最多 {scale} 次；实际本次硬上限 {hard_limit} 次，阶段累计上限 180 次（含重试），"
            f"模型 {Config.OPENAI_MODEL}，接口 {getattr(Config, 'API_MODE', 'responses')}。点击下方按钮会产生 API 请求和费用。"
        )
        if not Config.OPENAI_API_KEY:
            st.caption("请先配置 OPENAI_API_KEY；无需 Key 即可读取下方已保存报告。")
        if split == "test":
            st.warning("测试集真实运行须先完成人工或 AI 复核并冻结实验清单，请使用 README 的最终评测命令。")
        button_label = "确认并运行 LIVE 真实评测（调用 API）"
    if st.button(button_label, key="eval_run", disabled=not selected_samples or (mode == "live" and (not Config.OPENAI_API_KEY or split == "test"))):
        with st.spinner("正在运行评测并保存报告…"):
            reports = []
            st.session_state.pop("eval_report_detail", None)
            try:
                control = RunControl(hard_limit) if mode == "live" else None
                for version in versions:
                    report = run_evaluation(
                        samples, split=split, prompt_version=version,
                        mode=mode, max_workers=concurrency, sample_ids=sample_ids,
                        limit=limit, max_retries=retries, max_attempts=hard_limit,
                        control=control,
                        review_ledger=load_review_ledger(DEFAULT_DATASET) if mode == "live" else None,
                    )
                    reports.append(report)
                    st.session_state["evaluation_reports"] = reports.copy()
                    save_report(report)
                    if report["metadata"].get("run_status") == "incomplete" or (control is not None and control.stopped):
                        break
                if any(report["metadata"].get("run_status") == "incomplete" for report in reports):
                    st.warning("运行中止，已保存逐条证据和未执行清单。")
                else:
                    st.success("评测完成，报告已保存。切换选项或页面刷新不会重复调用模型。")
            except Exception as error:
                st.error(f"评测未完成：{describe_error(error)}")

    _render_saved_reports()
