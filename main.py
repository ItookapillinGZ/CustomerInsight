import streamlit as st
from collections import Counter
import hashlib
import logging
import pandas as pd

from ai.analyzer import analyze_feedback
from ai.prompts import available_prompt_versions
from ai.batch import analyze_batch, describe_error
from core.config import Config
from core.csv_io import (
    CsvInputError, export_results_csv, prepare_feedback_rows,
    read_feedback_csv, results_to_dataframe,
)
from models.enums import ProcessingStatus
from evaluation.ui import render_evaluation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_footer() -> None:
    st.markdown("---")
    st.markdown(
        """
        <div style='
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: #ccc;
            font-size: 1rem;
            padding: 1.8rem 0;
            background-color: #181c28;
            border-radius: 8px;
        '>
            <p style='margin: 0.3rem 0; color: #aaa;'>Built with ❤️ by <strong>Y. Siva Sai Krishna</strong> (@ysskrishna)</p>
            <div style='margin-top: 0.6rem; display: flex; gap: 1.2rem;'>
                <a href="https://github.com/ysskrishna" target="_blank" style="color: #ccc; text-decoration: none; display: flex; align-items: center; gap: 0.3rem;">
                    <img src="https://img.icons8.com/material-rounded/20/ffffff/github.png"/> GitHub
                </a>
                <a href="https://linkedin.com/in/ysskrishna" target="_blank" style="color: #ccc; text-decoration: none; display: flex; align-items: center; gap: 0.3rem;">
                    <img src="https://img.icons8.com/material-rounded/20/ffffff/linkedin.png"/> LinkedIn
                </a>
                <a href="https://github.com/ysskrishna/ai-customer-feedback-analyzer" target="_blank" style="color: #ccc; text-decoration: none; display: flex; align-items: center; gap: 0.3rem;">
                    <img src="https://img.icons8.com/material-rounded/20/ffffff/source-code.png"/> Repository
                </a>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )


def clear_single_feedback() -> None:
    st.session_state["single_text"] = ""
    st.session_state.pop("single_result", None)


def render_single_feedback() -> None:
    feedback_text = st.text_area(
        "输入客户反馈", key="single_text",
        help=f"最多 {Config.MAX_TEXT_LENGTH} 个字符。",
    )
    prompt_version = st.selectbox("单条分析 Prompt 版本", list(available_prompt_versions()), key="single_prompt_version")
    analyze_col, clear_col = st.columns([1, 5])
    with analyze_col:
        if st.button("分析单条反馈", key="analyze_single"):
            st.session_state.pop("single_result", None)
            if not feedback_text.strip():
                st.warning("请输入反馈文本。")
            else:
                with st.spinner("正在分析反馈…"):
                    try:
                        result = analyze_feedback(feedback_text, prompt_version=prompt_version)
                        st.session_state["single_result"] = result.model_dump(mode="json")
                        st.success("分析完成。")
                    except Exception as error:
                        st.error(describe_error(error))
    with clear_col:
        st.button("清空单条反馈", on_click=clear_single_feedback)
    if "single_result" in st.session_state:
        st.subheader("结构化分析结果")
        st.json(st.session_state["single_result"])


def render_batch_results(frame: pd.DataFrame) -> None:
    successful = frame[frame["processing_status"] == ProcessingStatus.SUCCESS.value]
    failures = frame["processing_status"].isin([
        ProcessingStatus.FAILED.value, ProcessingStatus.VALIDATION_ERROR.value,
    ]).sum()
    high_priority = int((successful["priority"] == "high").sum())
    total_col, success_col, failed_col, high_col = st.columns(4)
    total_col.metric("原始记录", len(frame))
    success_col.metric("分析成功", len(successful))
    failed_col.metric("分析失败", int(failures))
    high_col.metric("高优先级反馈", high_priority)
    skipped = len(frame) - len(successful) - int(failures)
    st.caption(f"跳过 {skipped} 条；以下分布图仅统计分析成功的反馈，每条反馈计一次。")
    st.subheader("结果表格")
    st.dataframe(frame, hide_index=True, width="stretch")
    if not successful.empty:
        sentiment_col, category_col = st.columns(2)
        with sentiment_col:
            st.subheader("情感分类分布")
            st.bar_chart(successful["sentiment"].value_counts().rename("数量"))
        with category_col:
            st.subheader("问题类别分布")
            st.bar_chart(successful["category"].value_counts().rename("数量"))
        if high_priority:
            with st.expander("查看高优先级反馈"):
                st.dataframe(
                    successful[successful["priority"] == "high"],
                    hide_index=True, width="stretch",
                )
    else:
        st.info("没有成功结果，请查看 processing_status 和 error_message。")
    st.download_button(
        "下载分析结果 CSV", data=export_results_csv(frame),
        file_name="customerinsight_results.csv", mime="text/csv",
        key="download_batch_results",
    )


def render_batch_feedback() -> None:
    st.caption(
        f"上传含表头的 CSV，最多 {Config.MAX_BATCH_ROWS} 条记录、5 MB；"
        f"每条反馈最多 {Config.MAX_TEXT_LENGTH} 个字符。空值、重复项与无效文本保留并标记为跳过。"
    )
    uploaded = st.file_uploader("上传客户反馈 CSV", type=["csv"], key="feedback_csv")
    if uploaded is None:
        st.session_state.pop("batch_result", None)
        st.session_state.pop("batch_input_key", None)
        st.info("可先使用项目中的 examples/feedback.csv，选择 feedback 列。")
        return
    encoding_label = st.selectbox("CSV 文件编码", ["UTF-8（推荐）", "GB18030 / GBK"])
    encoding = "utf-8-sig" if encoding_label.startswith("UTF-8") else "gb18030"
    data = uploaded.getvalue()
    try:
        frame = read_feedback_csv(data, filename=uploaded.name, encoding=encoding)
        preferred_column = list(frame.columns).index("feedback") if "feedback" in frame.columns else 0
        text_column = st.selectbox("选择反馈文本列", list(frame.columns), index=preferred_column)
        rows = prepare_feedback_rows(frame, text_column)
    except CsvInputError as error:
        st.session_state.pop("batch_result", None)
        st.error(str(error))
        return

    prompt_version = st.selectbox("批量分析 Prompt 版本", list(available_prompt_versions()), key="batch_prompt_version")
    input_key = (hashlib.sha256(data).hexdigest(), encoding, text_column, Config.OPENAI_MODEL, prompt_version)
    if st.session_state.get("batch_input_key") != input_key:
        st.session_state.pop("batch_result", None)
        st.session_state["batch_input_key"] = input_key
    counts = Counter(row.processing_status for row in rows)
    pending = counts[ProcessingStatus.PENDING]
    st.write(
        f"共 {len(rows)} 条记录：待分析 {pending} 条，"
        f"空值 {counts[ProcessingStatus.SKIPPED_EMPTY]} 条，"
        f"重复 {counts[ProcessingStatus.SKIPPED_DUPLICATE]} 条，"
        f"无效 {counts[ProcessingStatus.SKIPPED_INVALID]} 条。"
    )
    st.caption("source_row 是 CSV 逻辑记录序号：表头为 1，首条数据为 2；带引号的多行文本算一条。")
    with st.expander("查看输入预览及跳过原因", expanded=True):
        st.dataframe(results_to_dataframe(rows), hide_index=True, width="stretch")
    concurrency = st.slider("并发请求数", 1, Config.MAX_WORKERS, Config.DEFAULT_WORKERS)
    st.caption(
        f"本次最多分析 {pending} 条不同反馈；每条最多 {Config.MAX_RETRIES + 1} 次请求尝试。"
        "点击按钮会调用 OpenAI API 并产生费用，重新点击会重新分析。"
    )
    if st.button("开始批量分析", key="analyze_batch"):
        st.session_state.pop("batch_result", None)
        progress = st.progress(0.0, text="准备处理…")

        def update_progress(completed: int, total: int) -> None:
            progress.progress(completed / total if total else 1.0, text=f"已处理 {completed}/{total} 条")

        try:
            results = analyze_batch(
                rows, max_workers=concurrency, progress_callback=update_progress,
                prompt_version=prompt_version,
            )
            st.session_state["batch_result"] = results_to_dataframe(results)
            st.success("批量处理完成，失败和跳过记录已保留在结果中。")
        except Exception as error:
            progress.empty()
            st.error(describe_error(error))
    if "batch_result" in st.session_state:
        render_batch_results(st.session_state["batch_result"])


def main() -> None:
    st.set_page_config(page_title="CustomerInsight", layout="wide")
    st.title("CustomerInsight")
    st.caption("LLM 客户反馈分析与自动化评测平台 · 单条、CSV 批量与自动化评测")
    single_tab, batch_tab, evaluation_tab = st.tabs(["单条反馈分析", "CSV 批量分析", "自动化评测"])
    with single_tab:
        render_single_feedback()
    with batch_tab:
        render_batch_feedback()
    with evaluation_tab:
        render_evaluation()
    create_footer()


if __name__ == "__main__":
    main()
