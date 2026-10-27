"""Prediction-free Streamlit controls for a person's explicit label review."""

from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from evaluation.data import DEFAULT_DATASET
from evaluation.review import (
    ANNOTATION_GUIDE, load_review_ledger, review_checklist_csv,
    review_checklist_json, review_path_for, save_review,
)
from models.enums import ActionItemPriorityType, SentimentType, TopicType


def _save(*, dataset_path, review_path, ledger, updates, reviewer, reason, confirmed_ids=()):
    return save_review(
        dataset_path, review_path, updates=updates, reviewer=reviewer, reasons=reason,
        confirm_ids=list(confirmed_ids), human_confirmed=bool(confirmed_ids),
        expected_dataset_hash=ledger["current_dataset_hash"],
    )


def render_review(dataset_path: str | Path = DEFAULT_DATASET, review_path: str | Path | None = None) -> bool:
    """Opening or downloading the checklist never writes or confirms labels."""
    st.subheader("人工复核入口")
    st.caption("先依据标注规则独立判断。此区域不展示模型预测；AI 建议不能替代你的实际确认。")
    if not st.session_state.get("review_open", False):
        if st.button("打开人工复核区与 60 条清单", key="review_open_button"):
            st.session_state["review_open"] = True
            st.rerun()
        return False
    if st.button("返回自动化评测", key="review_close_button"):
        st.session_state["review_open"] = False
        st.rerun()
    source = Path(dataset_path)
    target = review_path_for(source, review_path)
    try:
        ledger = load_review_ledger(source, target)
    except (ValueError, OSError) as error:
        st.error(f"复核数据读取失败：{error}")
        return True
    notice = st.session_state.pop("review_notice", None)
    if notice:
        st.success(notice)
    counts = Counter(record["review_status"] for record in ledger["records"])
    st.write(f"合成示例 {len(ledger['records'])} 条：人工确认 {counts.get('reviewed', 0)} 条，AI 复核 {counts.get('ai_reviewed', 0)} 条，待复核 {counts.get('draft', 0)} 条。")
    st.caption(f"进度文件：{target}。保存后可关闭页面，下次继续；备份放在同目录 backups。")
    st.info("这些记录始终是合成示例。用户委托的 AI 复核单独记录来源，可用于参考标签一致性实验；人工确认仅来自你的明确操作。标签歧义依据规则处理，不能为了匹配预测改 gold。")
    with st.expander("查看完整标注规则", expanded=False):
        st.markdown(ANNOTATION_GUIDE.read_text(encoding="utf-8"))
    csv_col, json_col = st.columns(2)
    with csv_col:
        st.download_button("下载人工复核清单 CSV", review_checklist_csv(ledger),
                           file_name="human_review_checklist.csv", mime="text/csv", key="review_download_csv")
    with json_col:
        st.download_button("下载复核进度与历史 JSON", review_checklist_json(ledger),
                           file_name="human_review_history.json", mime="application/json", key="review_download_json")
    lookup = {record["sample_id"]: record for record in ledger["records"]}
    selected = st.selectbox("逐条查看反馈", list(lookup), key="review_sample_id",
                            format_func=lambda sample_id: f"{sample_id} · {lookup[sample_id]['split']} · {lookup[sample_id]['review_status']}")
    record = lookup[selected]
    st.text_area("待复核反馈原文", record["text"], disabled=True, key=f"review_text_{selected}")
    st.caption(f"本条状态：{record['review_status']}。初始标签永久保留，当前标签为待独立判断的参考。")
    st.dataframe(pd.DataFrame([
        {"标签来源": "初始草稿", **record["initial_labels"]},
        {"标签来源": "当前保存", **record["current_labels"]},
    ]), hide_index=True, width="stretch")
    revision_key = f"{selected}_{ledger['current_dataset_hash'][:12]}"
    label_cols = st.columns(3)
    updates = {}
    for column, field, label, enum in zip(
        label_cols, ("gold_sentiment", "gold_category", "gold_priority"),
        ("参考情感", "参考主类别", "参考优先级"),
        (SentimentType, TopicType, ActionItemPriorityType),
    ):
        options = [member.value for member in enum]
        with column:
            updates[field] = st.selectbox(label, options, index=options.index(record["current_labels"][field]),
                                          key=f"review_{field}_{revision_key}")
    reviewer = st.text_input("复核者姓名或代号", key="review_reviewer")
    reason = st.text_area("修改或确认理由（依据标注规则；未改也请说明）", key=f"review_reason_{selected}")
    valid_identity = bool(reviewer.strip() and reason.strip())
    st.caption("保存草稿会保留你的标签与理由，但仍标 draft；再次保存已确认记录的草稿也会重新变为 draft。")
    if st.button("保存本条草稿（不确认）", key="review_save_draft", disabled=not valid_identity):
        try:
            _save(dataset_path=source, review_path=target, ledger=ledger, updates={selected: updates},
                  reviewer=reviewer, reason=reason)
            st.session_state["review_notice"] = f"已保存 {selected} 的草稿，未标记 reviewed；原始文件已备份。"
            st.rerun()
        except (ValueError, OSError) as error:
            st.error(f"保存失败：{error}")
    st.write(f"本次单条确认明确涉及：**{selected}**")
    single_checked = st.checkbox(f"我已阅读反馈与规则，并实际确认 {selected} 的以上三个标签", key=f"review_single_check_{revision_key}")
    if st.button("确认本条标签并标记 reviewed", key="review_confirm_single", disabled=not(valid_identity and single_checked)):
        try:
            _save(dataset_path=source, review_path=target, ledger=ledger, updates={selected: updates},
                  reviewer=reviewer, reason=reason, confirmed_ids=[selected])
            st.session_state["review_notice"] = f"已记录你对 {selected} 的明确确认，复核证据与文件备份已保存。"
            st.rerun()
        except (ValueError, OSError) as error:
            st.error(f"确认保存失败：{error}")
    with st.expander("明确选择多条并批量确认当前已保存标签", expanded=False):
        st.caption("批量确认使用下表当前已保存标签；单条区域尚未保存的修改不包含在批量确认中。请先逐条判断。")
        ids = st.multiselect("选择本次明确确认的 sample_id", list(lookup), key="review_bulk_ids")
        if ids:
            st.write("本次批量确认明确涉及：" + "、".join(ids))
            st.dataframe(pd.DataFrame([
                {"sample_id": sample_id, "text": lookup[sample_id]["text"],
                 **lookup[sample_id]["current_labels"], "review_status": lookup[sample_id]["review_status"]}
                for sample_id in ids
            ]), hide_index=True, width="stretch")
        bulk_reason = st.text_area("批量确认理由（用于以上每条记录）", key="review_bulk_reason")
        bulk_checked = st.checkbox("我已逐条独立复核上表列出的所有反馈和标签，并明确确认这些记录", key=f"review_bulk_check_{ledger['current_dataset_hash'][:12]}_{'_'.join(ids)}")
        if st.button("确认列出的记录并标记 reviewed", key="review_confirm_bulk",
                     disabled=not(ids and bulk_checked and reviewer.strip() and bulk_reason.strip())):
            try:
                _save(dataset_path=source, review_path=target, ledger=ledger,
                      updates={sample_id: lookup[sample_id]["current_labels"] for sample_id in ids},
                      reviewer=reviewer, reason=bulk_reason, confirmed_ids=ids)
                st.session_state["review_notice"] = f"已记录你对以下 {len(ids)} 条的明确确认：" + "、".join(ids)
                st.rerun()
            except (ValueError, OSError) as error:
                st.error(f"批量确认保存失败：{error}")
    with st.expander("查看本条复核历史"):
        if record["history"]:
            st.json(record["history"])
        else:
            st.write("尚未保存或实际确认本条标签。")
    return True
