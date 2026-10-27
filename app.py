"""Customer review analysis with local history and traceable batch comparison."""

from collections import Counter
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pandas as pd
import altair as alt
import streamlit as st

# Seed deployment-only settings before Config loads. A local .env retains its
# existing override behavior; no secret value is displayed or written to disk.
_SECRET_KEYS = {
    "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
    "OPENAI_TIMEOUT_SECONDS", "OPENAI_MAX_OUTPUT_TOKENS", "OPENAI_REASONING_EFFORT",
    "API_MODE", "CHAT_OUTPUT_MODE", "CHAT_TOKEN_PARAMETER", "LLM_MAX_RETRIES",
    "LLM_RETRY_BACKOFF_SECONDS", "APP_ACCESS_PASSWORD", "APP_PUBLIC_MODE",
    "APP_DAILY_REQUEST_LIMIT", "APP_USAGE_DB_PATH",
    "APP_LLM_BATCH_SIZE", "APP_LLM_BATCH_MAX_CHARACTERS",
    "APP_DATA_DIR", "APP_HISTORY_DB_PATH",
    "APP_ANALYSIS_MODE", "APP_API_URL", "APP_API_TOKEN", "APP_UI_MODE",
}
try:
    for _secret_key in _SECRET_KEYS:
        if _secret_key in st.secrets:
            os.environ.setdefault(_secret_key, str(st.secrets[_secret_key]))
except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
    pass

from ai.batch import describe_error
from core.config import Config
from core.csv_io import (
    CsvInputError, prepare_feedback_rows, read_feedback_csv, results_to_dataframe,
)
from core.excel_io import (
    ExcelInputError, inspect_workbook, prepare_excel_feedback_rows, read_feedback_xlsx,
)
from core.customer_report import (
    build_customer_report, export_customer_csv, export_customer_html,
)
from core.analysis_agent import (
    AnalysisAgentResult, DEFAULT_AGENT_QUESTION, DEFAULT_MAX_MODEL_CALLS,
    DEFAULT_MAX_TOOL_CALLS, MAX_QUESTION_CHARACTERS, TOOL_LABELS,
    MAX_CHAT_TURNS, MAX_HISTORY_TURNS, report_fingerprint,
)
from core.report_visuals import build_report_summary
from core.report_interactions import chart_focus, focus_label, focus_question, focus_rows, recommended_questions, update_chart_focus
from core.history_store import HistoryStore
from core.history_workflow import attach_historical_context, date_scope, suggest_columns
from core.product_service import (
    ProductLimitError, analyze_customer_rows, estimate_customer_requests,
    get_service_status, requires_access_password,
    run_customer_report_agent, service_access_password,
)
from models.enums import ProcessingStatus
from models.models import FeedbackResult
from backend.client import JobApiClient, JobClientError, queued_analysis_enabled, api_ui_enabled
from backend.ui_history import RemoteHistory, decode_view


ROOT = Path(__file__).resolve().parent
STATE_PREFIX = "customer_"

STYLE = """
<style>
:root { --ci-ink: #15334b; --ci-accent: #087f8c; --ci-line: #dce6ee; }
.stApp { background: #f4f7fb; color: #334155; }
.stApp, input, textarea, button { font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; }
.block-container { max-width: 1390px; padding-top: 1.7rem; padding-bottom: 2rem; }
h1, h2, h3 { color: var(--ci-ink); letter-spacing: -.025em; }
h1 { font-size: 1.8rem !important; line-height: 1.4 !important; padding-bottom: .2rem !important; }
h2 { font-size: 1.3rem !important; padding-top: .7rem !important; }
h3 { font-size: 1.03rem !important; padding-top: .4rem !important; }
p { max-width: 76ch; }
.ci-steps { display: flex; gap: 24px; padding: 15px 0 22px; color: #6a7f91; font-size: .87rem; flex-wrap: wrap; }
.ci-steps span { display: flex; gap: 8px; align-items: center; }
.ci-steps b { display: inline-flex; align-items: center; justify-content: center; width: 23px; height: 23px; border-radius: 50%; background: #e4ecf3; color: #527087; font-size: .75rem; }
.ci-steps .current { color: #087f8c; font-weight: 600; }
.ci-steps .current b { color: white; background: #087f8c; }
.ci-brand { color: #15334b; font-weight: 750; font-size: 1.26rem; letter-spacing: -.04em; }
.ci-brand small { display: block; font-size: .79rem; letter-spacing: 0; font-weight: 400; color: #64788b; margin-top: 4px; }
.ci-nav a { display: block; border-left: 3px solid #b9ccd9; margin: 13px 0; padding: 4px 0 4px 12px; text-decoration: none; color: #36566d; font-size: .9rem; }
.ci-nav a:first-child { border-color: #087f8c; color: #087f8c; font-weight: 600; }
.ci-empty { border: 1px dashed #c5d6e2; padding: 28px; margin-top: 15px; color: #5a7186; background: #fff; border-radius: 6px; }
.ci-empty h3 { margin-top: 0; }
.ci-section { height: 8px; }
.ci-agent-primer { border-left: 4px solid var(--ci-accent); background: #eef7f7; padding: 14px 18px; margin: 4px 0 18px; color: #36566d; }
.ci-agent-primer strong { display: block; color: var(--ci-ink); font-size: 1rem; margin-bottom: 3px; }
.ci-agent-status { display: inline-block; border: 1px solid #b9d8d9; color: #176b72; background: #f2fafa; border-radius: 999px; padding: 3px 10px; margin: 2px 0 10px; font-size: .78rem; font-weight: 650; }
.ci-agent-status.fallback { border-color: #e3cfaa; color: #7b5a20; background: #fff9ed; }
.ci-agent-divider { border-top: 1px solid var(--ci-line); margin: 8px 0 15px; }
.ci-demo-note { border-left: 4px solid #087f8c; padding: 12px 18px; background: #eef8f8; margin: 6px 0 18px; }
.ci-demo-note strong { display: block; color: #15334b; margin-bottom: 4px; }
.ci-focus { border-left: 4px solid #c69852; padding: 10px 16px; margin: 8px 0; background: #fffcf4; }
[data-testid='stSidebar'] { background: #edf3f8; border-right: 1px solid #dce6ee; }
[data-testid='stSidebar'] .stElementContainer p { max-width: 30ch; }
[data-testid='stMetric'] { padding: .4rem 0; }
[data-testid='stMetricValue'] { color: var(--ci-ink); }
[data-testid='stMetricLabel'] { color: #657b8d; }
[data-testid='stFileUploaderDropzone'] { background: #eef5fa; border: 1px dashed #b8cddc; border-radius: 6px; }
[data-testid='stExpander'] { background: white; border-radius: 6px; }
[data-testid='stVerticalBlockBorderWrapper'] > div { border-radius: 6px; }
[data-testid='stTabs'] { background: white; padding: 4px 19px 18px; border: 1px solid #dce6ee; border-radius: 6px; }
[data-testid='stTabs'] [role='tab'] { padding: 14px 10px; font-size: .94rem; }
[data-testid='stTabs'] [role='tablist'] { gap: 20px; }
[data-testid='stDataFrame'] { border-radius: 4px; }
button[kind='primary'] { background: #087f8c; border-color: #087f8c; }
button:focus-visible, input:focus-visible, textarea:focus-visible { outline: 3px solid #087f8c !important; outline-offset: 3px; }
.ci-footer { border-top: 1px solid var(--ci-line); margin-top: 2rem; padding-top: .8rem; color: #64748b; font-size: .74rem; }
.ci-footer a { color: #365b75; }
@media (max-width: 640px) { .block-container { padding-top: 1rem; } h1 { font-size: 1.5rem !important; } .ci-steps { gap: 10px; } .ci-agent-primer { padding: 12px 14px; } [data-testid='stTabs'] { padding: 0 10px 10px; } [data-testid='stTabs'] [role='tablist'] { gap: 3px; } }
@media (max-width: 640px) {
  [data-testid='stHorizontalBlock']:has([data-testid='stMetric']) { flex-direction: row !important; flex-wrap: wrap !important; gap: .5rem !important; }
  [data-testid='stHorizontalBlock']:has([data-testid='stMetric']) > [data-testid='stColumn'] { flex: 1 1 45% !important; min-width: 0 !important; width: 45% !important; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
</style>
"""


def _clear_agent_state() -> None:
    for key in (
        "customer_agent_result", "customer_agent_report_key", "customer_agent_question_used",
        "customer_agent_question", "customer_agent_consent", "customer_agent_error",
        "customer_agent_history", "customer_agent_pending_question",
        "customer_agent_loaded_scope",
        "customer_agent_brief",
    ):
        st.session_state.pop(key, None)


def _clear_results() -> None:
    _clear_agent_state()
    for key in ("customer_report", "customer_results", "customer_input_key", "customer_consent", "customer_visible_detail", "customer_run_snapshot",
                "customer_active_batch_id", "customer_history_view", "customer_previous_batch_id", "customer_focus", "customer_focus_events",
                "customer_demo_report", "customer_demo_workspace", "customer_demo_replay_count"):
        st.session_state.pop(key, None)
    st.session_state["customer_focus_epoch"] = st.session_state.get("customer_focus_epoch", 0) + 1
    for key in list(st.session_state):
        if key.startswith(("customer_filter_", "customer_insight_", "customer_group_chart_", "customer_compare_")):
            st.session_state.pop(key, None)


def _history_store() -> HistoryStore:
    if api_ui_enabled():
        return RemoteHistory(_job_client())
    return HistoryStore()


def _history_has_related_jobs(store: HistoryStore, record: dict) -> bool:
    """Conservatively protect originals retained in durable job snapshots."""
    if api_ui_enabled():
        return not _job_client().deletion(record["id"])["allowed"]
    if not (store.root_dir / "analysis_jobs.sqlite3").is_file():
        return False
    from backend.job_store import JobStore

    return JobStore(store.root_dir).has_related_jobs(record["id"], record.get("content_hash"))


def _delete_history_if_unreferenced(store: HistoryStore, record: dict) -> bool:
    """Keep the job snapshot check and history deletion under one writer lock."""
    if api_ui_enabled():
        store.delete_batch(record["id"])
    elif queued_analysis_enabled() or (store.root_dir / "analysis_jobs.sqlite3").is_file():
        from backend.job_store import JobStore

        with JobStore(store.root_dir).history_delete_guard(
                record["id"], original_hash=record.get("content_hash")) as allowed:
            if not allowed:
                return False
            store.delete_batch(record["id"])
    else:
        store.delete_batch(record["id"])
    return True


_JOB_LABELS = {
    "queued": "等待分析", "running": "正在分析", "partial": "已保存部分结果",
    "completed": "分析完成", "failed": "分析暂未完成", "cancelled": "已停止",
    "needs_review": "需要核对调用状态",
}


def _job_client() -> JobApiClient:
    if api_ui_enabled():
        return JobApiClient(os.getenv("APP_API_URL", "http://127.0.0.1:8000"),
                            st.session_state.get("ci_account_token", ""))
    return JobApiClient.from_environment()


def _account_principal() -> dict:
    return _job_client().me()


def _forget_account() -> None:
    # Uploaded bytes, reports, chat and widget state all belong to this login.
    for key in list(st.session_state):
        del st.session_state[key]


def _account_access_allowed() -> bool:
    if st.session_state.get("ci_account_token"):
        try:
            principal = _account_principal()
        except JobClientError as error:
            if error.status_code == 401:
                _forget_account()
                st.session_state["ci_account_notice"] = "登录已过期，请重新登录。"
                st.rerun()
            st.error(str(error))
            return False
        if principal["id"] != st.session_state.get("ci_account_user", {}).get("id"):
            _forget_account()
            st.rerun()
        with st.sidebar:
            st.caption(f"当前账户：{principal['username']}")
            if st.button("退出登录", key="ci_account_logout"):
                try:
                    _job_client().logout()
                except JobClientError:
                    st.error("退出未确认，请重试，或等待会话到期。")
                else:
                    _forget_account()
                    st.rerun()
            with st.expander("修改密码"):
                with st.form("ci_password_form"):
                    current = st.text_input("当前密码", type="password")
                    new = st.text_input("新密码（至少 12 位）", type="password")
                    repeated = st.text_input("再次输入新密码", type="password")
                    change = st.form_submit_button("更新密码并退出")
                if change:
                    if new != repeated or len(new) < 12:
                        st.error("请确认新密码至少 12 位，且两次输入一致。")
                    else:
                        try:
                            _job_client().change_password(current, new)
                        except JobClientError as error:
                            st.error(str(error))
                        else:
                            _forget_account()
                            st.rerun()
        return True
    if st.session_state.get("ci_account_notice"):
        st.info(st.session_state.pop("ci_account_notice"))
    st.info("登录后上传表格。每个账户仅能访问自己的评价、报告和对话。")
    st.caption("账号由部署者创建，请使用已分配的用户名和密码。")
    with st.form("ci_login_form"):
        username = st.text_input("用户名", max_chars=64)
        password = st.text_input("密码", type="password", max_chars=256)
        submitted = st.form_submit_button("登录", type="primary")
    if submitted:
        try:
            client = JobApiClient(os.getenv("APP_API_URL", "http://127.0.0.1:8000"), "login-unused")
            result = client.login(username, password)
        except JobClientError as error:
            st.error(str(error))
        else:
            _forget_account()
            st.session_state["ci_account_token"] = result["access_token"]
            st.session_state["ci_account_user"] = result["user"]
            st.rerun()
    return False


def _open_job_report(job: dict) -> bool:
    batch_id = job.get("batch_id")
    if not batch_id:
        return False
    try:
        _restore_batch(_history_store().load_batch(batch_id), history_view=True)
    except Exception:
        st.warning("后台已保存结果，但暂时无法打开报告，请稍后从历史报告中查看。")
        return False
    st.session_state["customer_last_job_id"] = job["id"]
    return True


@st.fragment(run_every=3.0)
def _render_active_job() -> None:
    job_id = st.session_state.get("customer_active_job_id")
    if not job_id:
        return
    try:
        client = _job_client()
        job = client.get_job(job_id)
    except JobClientError as error:
        st.warning(str(error))
        st.button("刷新任务进度", key="customer_job_refresh")
        return
    status = job.get("status", "queued")
    completed, total = int(job.get("completed_rows", 0)), int(job.get("total_rows", 0))
    with st.container(border=True):
        st.subheader(_JOB_LABELS.get(status, "查看分析进度"))
        st.caption(str(job.get("filename", "评价表格")))
        st.progress(min(1.0, completed / total) if total else 0.0,
                    text=f"已处理 {completed}/{total} 条评价")
        if status in {"queued", "running"}:
            st.caption("可以关闭网页。任务会在后台继续，重新打开后可从任务列表查看。")
            if st.button("停止后续分析", key=f"customer_job_cancel_{job_id}"):
                try:
                    client.cancel_job(job_id)
                    st.info("已请求停止；正在执行的分析可能仍会完成并计费，已完成结果会保留。")
                except JobClientError as error:
                    st.warning(str(error))
                else:
                    st.rerun()
        elif status == "needs_review":
            st.warning("部分请求的实际完成情况尚不明确，系统已停止自动重试，以免重复计费。已保存的结果可以查看，请联系管理员核对。")
        elif status == "cancelled":
            st.info("已停止领取新的分析分组。此前已保存的结果仍可查看。")
        elif status in {"partial", "failed"}:
            st.info("分析尚未全部完成，已收到并校验的结果已保存。")
        if status == "completed" and job.get("batch_id"):
            if _open_job_report(job):
                st.session_state.pop("customer_active_job_id", None)
                st.rerun()
        elif job.get("batch_id"):
            if st.button("查看已保存结果", key=f"customer_job_result_{job_id}"):
                if _open_job_report(job):
                    st.session_state.pop("customer_active_job_id", None)
                    st.rerun()
        if job.get("can_resume") and status != "needs_review":
            confirmed = st.checkbox("我确认继续未完成的分析，这可能产生模型费用。", key=f"customer_job_resume_consent_{job_id}")
            if st.button("继续未完成的分析", disabled=not confirmed,
                         key=f"customer_job_resume_{job_id}"):
                try:
                    client.resume_job(job_id, max_requests=int(job["max_requests"]))
                except JobClientError as error:
                    st.warning(str(error))
                else:
                    st.rerun()
        with st.expander("任务信息"):
            st.caption(f"任务编号：{job_id}")
            st.caption(f"已占用请求 {job.get('request_count', 0)} / 本任务上限 {job.get('max_requests', 0)}")
        st.button("刷新任务进度", key="customer_job_refresh")
        if st.button("收起任务，上传其他表格", key="customer_job_dismiss"):
            st.session_state.pop("customer_active_job_id", None)
            st.rerun()


def render_background_jobs() -> None:
    """Reading or reopening a task never submits a model request."""
    if not queued_analysis_enabled():
        return
    with st.expander("后台分析任务", expanded=bool(st.session_state.get("customer_active_job_id"))):
        try:
            items = _job_client().list_jobs().get("items", [])
        except JobClientError as error:
            st.warning(str(error))
            items = []
        if items:
            by_id = {item["id"]: item for item in items}
            options = list(by_id)
            active = st.session_state.get("customer_active_job_id")
            selected = st.selectbox("已提交的分析", options,
                index=options.index(active) if active in options else 0,
                format_func=lambda value: f"{by_id[value].get('filename', '评价表格')} · {_JOB_LABELS.get(by_id[value].get('status'), '查看进度')} · {value[:8]}",
                key="customer_job_selection")
            if st.button("查看任务", key="customer_job_open"):
                _clear_results()
                st.session_state["customer_active_job_id"] = selected
        else:
            st.caption("暂无后台任务。上传评价表并明确点击分析后，任务会保存在这里。")
    if st.session_state.get("customer_active_job_id"):
        _render_active_job()


def _submit_background_job(uploaded, input_key: tuple, mapping: dict, expected_requests: int) -> None:
    """Keep one submission key through uncertain HTTP outcomes and reruns."""
    submission = st.session_state.get("customer_job_submission")
    if not submission or submission.get("input_key") != input_key or submission.get("confirmed"):
        submission = {"input_key": input_key, "key": uuid4().hex, "confirmed": False}
        st.session_state["customer_job_submission"] = submission
    try:
        client = _job_client()
        if not submission.get("upload_id"):
            remote = st.session_state.get("customer_remote_upload", {})
            if api_ui_enabled() and remote.get("digest") == input_key[0]:
                submission["upload_id"] = remote["manifest"]["upload_id"]
            else:
                submission["upload_id"] = client.upload(uploaded.name, uploaded.getvalue(),
                                                        encoding=mapping.get("encoding") or "utf-8-sig")["upload_id"]
        job = client.create_job({
            "upload_id": submission["upload_id"], **mapping,
            "encoding": mapping.get("encoding") or "utf-8-sig",
            "allow_model_call": True, "max_requests": expected_requests, "force": True,
        }, idempotency_key=submission["key"])
    except JobClientError as error:
        st.error(str(error))
        st.caption("请先在上方任务列表核对。再次明确点击分析时会沿用本次提交编号，不会因页面刷新自动重跑。")
        return
    submission["confirmed"] = True
    _clear_agent_state()
    st.session_state.pop("customer_report", None)
    st.session_state.pop("customer_results", None)
    if job.get("cached") and job.get("batch_id"):
        if _open_job_report(job):
            st.rerun()
        return
    st.session_state["customer_active_job_id"] = job["id"]
    st.rerun()


def _restore_batch(record: dict, *, history_view: bool) -> None:
    _clear_results()
    st.session_state["customer_report"] = record["report"]
    st.session_state["customer_active_batch_id"] = record["id"]
    st.session_state["customer_file_name"] = record["filename"]
    st.session_state["customer_history_view"] = history_view
    turns = _history_store().load_chat(record["id"])
    if turns:
        st.session_state["customer_agent_history"] = turns
        previous_id = turns[-1].get("comparison_batch_id")
        if isinstance(previous_id, str):
            st.session_state["customer_previous_batch_id"] = previous_id


def _persist_report(report: dict, uploaded, mapping: dict, frame: pd.DataFrame) -> None:
    report["period"] = date_scope(frame, mapping.get("date_column"))
    try:
        store = _history_store()
        batch_id = store.save_batch(report, filename=uploaded.name, file_bytes=uploaded.getvalue(), mapping=mapping)
        st.session_state["customer_active_batch_id"] = batch_id
        st.session_state["customer_history_view"] = False
        st.session_state.pop("customer_backup_blob", None)
        try:
            store.save_mapping(list(frame.columns), mapping)
        except Exception:
            st.caption("历史报告已保存；本次选列未能记住，下次上传时请重新确认。")
    except Exception:
        st.warning("本次结果已生成，但未能保存历史。请先下载报告，并检查部署机器的数据目录空间和写入权限。")


def _save_current_chat(turns: list[dict]) -> None:
    if api_ui_enabled():
        # POST /chat atomically appends on the API. Never resend or append a
        # paid turn merely because the page rerendered.
        st.session_state.pop("customer_backup_blob", None)
        return
    batch_id = st.session_state.get("customer_active_batch_id")
    if batch_id:
        saved = [{**turn, "comparison_batch_id": st.session_state.get("customer_previous_batch_id")} for turn in turns]
        try:
            history = _history_store()
            if hasattr(history, "append_chat") and saved:
                history.append_chat(batch_id, saved[-1], max_turns=MAX_CHAT_TURNS)
            else:
                history.save_chat(batch_id, saved)
            st.session_state.pop("customer_backup_blob", None)
        except Exception:
            st.warning("本轮对话已显示，但未保存到历史，请先下载对话记录。")


def _clear_saved_chat() -> None:
    if api_ui_enabled():
        batch_id = st.session_state.get("customer_active_batch_id")
        if batch_id:
            try:
                _job_client().clear_chat(batch_id, st.session_state.get("customer_previous_batch_id"))
            except JobClientError as error:
                st.error(str(error))
                return
        st.session_state.pop("customer_backup_blob", None)
        _clear_agent_state()
        return
    _save_current_chat([])
    _clear_agent_state()


def _prefill_question(question: str) -> None:
    st.session_state["customer_agent_question"] = question[:MAX_QUESTION_CHARACTERS]
    st.toast("问题已填入“问 Agent”，打开后即可发送。")


def _prefill_topic(topic: str) -> None:
    _prefill_question(f"关于「{topic}」，请结合原文说明顾客的具体反馈、涉及范围和可以尝试的下一步。")


def _select_focus(field: str, value: str, *, negative_only=False) -> None:
    st.session_state["customer_focus"] = {"field": field, "value": value, "negative_only": negative_only}


def _clear_focus() -> None:
    st.session_state.pop("customer_focus", None)
    st.session_state.pop("customer_focus_events", None)
    st.session_state["customer_focus_epoch"] = st.session_state.get("customer_focus_epoch", 0) + 1


def _linked_chart(chart, *, key: str, field: str, column: str, negative_only=False) -> None:
    pick = alt.selection_point(name="ci_pick", fields=[column], on="click", clear="dblclick", empty=False)
    epoch = st.session_state.get("customer_focus_epoch", 0)
    event = st.altair_chart(_chart_style(chart.add_params(pick)), use_container_width=True,
                           key=f"customer_linked_{key}_{epoch}", on_select="rerun", selection_mode="ci_pick")
    focus = chart_focus(event, field, column, negative_only=negative_only)
    previous = st.session_state.setdefault("customer_focus_events", {})
    current = update_chart_focus(key, focus, st.session_state.get("customer_focus"), previous)
    if current:
        st.session_state["customer_focus"] = current
    else:
        st.session_state.pop("customer_focus", None)


def _render_focused_evidence(report: dict) -> None:
    focus = st.session_state.get("customer_focus")
    if not focus:
        st.caption("点击图表中的一项，或选择主题，查看对应评价并继续追问。上方概览始终统计整批评价。")
        return
    shown = focus_rows(report, focus)
    st.subheader("当前关注的评价")
    st.write(focus_label(report, focus))
    st.caption(f"对应 {len(shown)} 条原始评价；此处用于查原文，上方整批统计保持不变。")
    if shown.empty:
        st.info("当前报告中没有对应评价，请清除选择或点击其他图表项。")
    else:
        _detail_table(shown, report, height=min(350, 80 + len(shown) * 36))
    ask, clear, download = st.columns([1.4, 1, 1.4])
    question = focus_question(report, focus)
    with ask:
        st.button("围绕这些评价提问", key="customer_focus_ask", disabled=not question,
                  on_click=_prefill_question, args=(question,), width="stretch")
    with clear:
        st.button("清除选择", key="customer_focus_clear", on_click=_clear_focus, width="stretch")
    with download:
        st.download_button("下载这些评价", export_customer_csv({**report, "detail": shown}),
                           "当前关注的评价.csv", mime="text/csv", key="customer_focus_download", width="stretch")


def _render_topics(report: dict) -> None:
    topics = report.get("top_topics", report.get("topics", [])[:3])
    st.subheader("这批评价最值得关注的事")
    if not topics:
        st.caption("这份报告尚无具体主题。新版本分析会同时提取顾客的认可、问题与需求；历史报告仍保留原有分类和原文。")
        return
    kind_labels = {"issue": "顾客问题", "request": "改进需求", "praise": "顾客认可"}
    for index, topic in enumerate(topics[:3]):
        with st.container(border=True):
            title, inspect, action = st.columns([3, 1, 1.3])
            with title:
                st.text(topic["topic"])
                st.caption(f"{kind_labels.get(topic['kind'], '评价主题')} · {topic['review_count']} 条 · 占有效评价 {topic['share']:.1%}")
            with inspect:
                st.button("查看相关评价", key=f"customer_topic_focus_{index}",
                          on_click=_select_focus, args=("topic", topic["key"]), width="stretch")
            with action:
                st.button("围绕这件事提问", key=f"customer_topic_ask_{index}",
                          on_click=_prefill_topic, args=(topic["topic"],), width="stretch")
            examples = topic.get("examples", [])
            if examples:
                example = examples[0]
                st.text(example.get("text", ""))
                st.caption(f"原表第 {example['source_row']} 行" + (f" · {example['group']}" if example.get("group") else ""))
            with st.expander("查看这个主题的全部原文"):
                names, detail = report["detail_columns"], report["detail"]
                _detail_table(detail[detail[names["source_row"]].isin(topic.get("source_rows", []))], report)
    coverage = report.get("topic_coverage", {})
    if coverage.get("without_aspects_rows"):
        st.caption(f"另有 {coverage['without_aspects_rows']} 条有效评价未提取到具体主题；主题数量可能重叠，一条评价可涉及多件事。")
    else:
        st.caption("一条评价可涉及多个主题，各主题占比之和可能超过 100%。主题用于发现线索，请结合原文判断。")


def _history_batch_label(record: dict) -> str:
    mapping = record.get("mapping") if isinstance(record.get("mapping"), dict) else {}
    sheet = mapping.get("sheet_name")
    parts = [str(sheet)] if sheet else []
    parts.extend([str(record.get("filename", "未命名批次")), str(record.get("created_at", ""))[:10]])
    return " · ".join(part for part in parts if part)


def _render_history_comparison(report: dict) -> dict:
    try:
        active_id = st.session_state.get("customer_active_batch_id")
        records = [record for record in _history_store().list_batches() if record["id"] != active_id]
    except Exception:
        st.caption("历史对比暂不可用，请检查本地数据目录。")
        return report
    if not records:
        return report
    by_id = {record["id"]: record for record in records}
    options = [None] + list(by_id)
    wanted = st.session_state.get("customer_previous_batch_id")
    with st.expander("与历史批次比较", expanded=wanted in by_id):
        selected = st.selectbox("选择对比批次", options,
            index=options.index(wanted) if wanted in by_id else 0,
            format_func=lambda value: "暂不比较" if value is None else _history_batch_label(by_id[value]),
            key=f"customer_compare_{active_id or report_fingerprint(report)[:12]}")
        st.session_state["customer_previous_batch_id"] = selected
        if selected is None:
            st.caption("选择一份历史报告，比较评价分布和具体主题。")
            return report
        try:
            previous = _history_store().load_batch(selected)
            prepared = (decode_view(_job_client().view_batch(active_id, selected))["report"]
                        if api_ui_enabled() else attach_historical_context(report, previous))
        except Exception:
            st.warning("所选历史报告无法读取，请重新选择或从历史管理检查原件。")
            return report
        comparison = prepared["historical_context"]["comparison"]
        st.caption(f"当前：{report['metrics']['success_rows']} 条有效评价；上批：{previous['report']['metrics']['success_rows']} 条有效评价。")
        for label, scope in (("当前批次", report.get("period", {})), ("上批", previous["report"].get("period", {}))):
            if scope:
                st.caption(f"{label}：{scope.get('label', '导入批次')}；可识别日期 {scope.get('valid_rows', 0)} 条。")
        if comparison.get("basic_comparable") is True:
            st.markdown("**样本与覆盖**")
            bases = [comparison.get(key, {}) for key in ("current_base", "previous_base")]
            coverage = comparison.get("metrics", {}).get("success_rate", {})
            st.dataframe(pd.DataFrame([
                {"指标": "原始记录", "本批": str(bases[0].get("total_rows", 0)),
                 "上批": str(bases[1].get("total_rows", 0)), "变化（百分点）": "—"},
                {"指标": "有效评价", "本批": str(bases[0].get("success_rows", 0)),
                 "上批": str(bases[1].get("success_rows", 0)), "变化（百分点）": "—"},
                {"指标": "有效覆盖率", "本批": f"{coverage['current']:.1%}",
                 "上批": f"{coverage['previous']:.1%}", "变化（百分点）": f"{coverage['change_pp']:+.1f}"},
            ]), hide_index=True, width="stretch")
            for key, label in (("sentiment", "情感分布"), ("category", "评价类别"), ("priority", "优先级")):
                values = comparison.get("distributions", {}).get(key, [])
                if not values:
                    continue
                st.markdown(f"**{label}**")
                st.dataframe(pd.DataFrame([{
                    "项目": item["label"], "本批条数": item["current_count"], "上批条数": item["previous_count"],
                    "本批占比": f"{item['current_share']:.1%}", "上批占比": f"{item['previous_share']:.1%}",
                    "变化（百分点）": f"{item['change_pp']:+.1f}",
                } for item in values]), hide_index=True, width="stretch")
            st.caption("情感、类别和优先级占比分别以各批有效评价为分母；覆盖率以各批原始记录为分母。")
        else:
            st.info("两批基础统计暂不能直接比较。")
            for reason in comparison.get("basic_reasons", []):
                st.caption(str(reason))
        st.markdown("**具体主题**")
        if not comparison.get("topics_comparable", comparison.get("comparable")):
            st.info("具体主题暂不计算增减；上面的基础统计可独立比较。" if comparison.get("basic_comparable") is True
                    else "具体主题暂不计算增减。")
            for reason in comparison.get("topic_reasons", comparison.get("reasons", [])):
                st.caption(str(reason))
        else:
            changes = comparison.get("topics", [])
            if changes:
                table = pd.DataFrame([{
                    "主题": item.get("topic", item.get("key", "")),
                    "本批条数": item["current_count"], "上批条数": item["previous_count"],
                    "本批占比": f"{item['current_share']:.1%}", "上批占比": f"{item['previous_share']:.1%}",
                    "变化（百分点）": round(item["change_pp"], 1),
                } for item in changes])
                st.dataframe(table, hide_index=True, width="stretch")
            else:
                st.caption("两批没有可比较的具体主题。")
        for warning in comparison.get("warnings", []):
            st.caption(str(warning))
        st.caption("这里比较两次导入批次的差异，不代表时间趋势；未剔除跨文件重复评价。变化不证明某项措施的效果。Agent 可读取这个对比批次的原文。")
        return prepared


def clear_customer_data() -> None:
    """Drop only this browser session's uploads/results; keep access authorization."""
    next_nonce = st.session_state.get("customer_upload_nonce", 0) + 1
    for key in list(st.session_state):
        if key.startswith(STATE_PREFIX) and key not in {"customer_authorized", "customer_access_error"}:
            del st.session_state[key]
    st.session_state["customer_upload_nonce"] = next_nonce


def _authenticate_access() -> None:
    expected = service_access_password()
    entered = st.session_state.get("customer_password", "")
    if expected and hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8")):
        st.session_state["customer_authorized"] = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        st.session_state.pop("customer_access_error", None)
    else:
        st.session_state.pop("customer_authorized", None)
        st.session_state["customer_access_error"] = True
    st.session_state["customer_password"] = ""


def _access_allowed() -> bool:
    if api_ui_enabled():
        return _account_access_allowed()
    try:
        needs_password = requires_access_password()
    except ProductLimitError:
        st.session_state.pop("customer_authorized", None)
        st.error("访问配置无效，服务暂未开放。请联系管理员检查部署设置。")
        return False
    if not needs_password:
        return True
    password = service_access_password()
    if not password:
        st.error("服务暂未开放，请联系管理员完成访问设置。")
        return False
    fingerprint = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if st.session_state.get("customer_authorized") == fingerprint:
        return True
    st.info("这是一个受邀使用的分析工具，请输入管理员提供的访问码。")
    with st.form("customer_access_form"):
        st.text_input("访问码", type="password", key="customer_password")
        st.form_submit_button("进入分析", on_click=_authenticate_access)
    if st.session_state.get("customer_access_error"):
        st.error("访问码不正确，请核对后重试。")
    return False


def _safe_service_error(error: Exception) -> str:
    """Keep shared technical failures actionable for a nontechnical audience."""
    if isinstance(error, ProductLimitError):
        return "分析额度或服务设置暂时不满足本次请求，请缩小表格或联系管理员。"
    message = describe_error(error)
    if "身份验证" in message or "OPENAI_" in message or "API_MODE" in message:
        return "分析服务配置暂时不可用，请联系管理员。"
    if "结构化输出" in message or "校验失败" in message:
        return "分析返回的结果不完整，未计入统计，请稍后重试。"
    if "运行日志" in message:
        return "分析暂未完成，请稍后重试；如果持续失败，请联系管理员。"
    return message.replace("LLM", "分析服务").replace("API", "服务")


def render_samples() -> None:
    template = ROOT / "examples" / "customer_reviews_template.xlsx"
    sample = ROOT / "datasets" / "public_reviews" / "customer_reviews_sample.xlsx"
    if template.is_file():
        st.download_button(
            "下载 Excel 模板", template.read_bytes(), "顾客评价模板.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="customer_download_template", width="stretch",
        )
    if sample.is_file():
        st.download_button(
            "下载公开评价样例", sample.read_bytes(), "公开顾客评价样例.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="customer_download_sample", width="stretch",
        )
        st.caption("公开样例为真实评价数据的节选，具体来源、许可与抽样方式见工作簿中的来源说明。下载样例不会发起分析。")


def _default_text_column(columns: list[str]) -> int:
    exact = {"评价", "评价内容", "顾客评价", "客户评价", "评论", "评论内容", "反馈", "review", "review_text", "feedback", "text", "content"}
    for index, column in enumerate(columns):
        if column.strip().lower() in exact:
            return index
    for index, column in enumerate(columns):
        if any(word in column.lower() for word in ("评价", "评论", "反馈", "review", "feedback")):
            return index
    return 0


def _chart_style(chart):
    return chart.configure_view(stroke=None).configure_axis(
        labelFont="Microsoft YaHei", titleFont="Microsoft YaHei", labelColor="#60788B",
        titleColor="#60788B", gridColor="#E8EEF3", domain=False, tickSize=0,
        labelPadding=8, titlePadding=10,
    ).configure_legend(labelFont="Microsoft YaHei", labelColor="#4C657A", title=None)


def _render_sentiment_chart(report: dict) -> None:
    frame = report["sentiment_counts"]
    active = alt.selection_point(fields=["情感"], on="pointerover", clear="pointerout")
    arc = alt.Chart(frame).mark_arc(innerRadius=72, outerRadius=105, stroke="white", strokeWidth=3).encode(
        theta=alt.Theta("数量:Q"),
        color=alt.Color("情感:N", scale=alt.Scale(domain=["正面", "中性", "负面"], range=["#2D826D", "#7D8D9F", "#C15D4F"]), legend=alt.Legend(orient="bottom")),
        opacity=alt.condition(active, alt.value(1), alt.value(.5)),
        tooltip=[alt.Tooltip("情感:N"), alt.Tooltip("数量:Q", title="评价数"), alt.Tooltip("占成功样本比例:Q", title="有效评价占比", format=".1%")],
    ).add_params(active)
    # Streamlit selections support a single view, so keep the donut itself
    # clickable instead of composing text layers that disable click events.
    _linked_chart(arc.properties(height=275, description="整批情感分布；点击一项查看原文。"),
                  key="sentiment", field="sentiment", column="情感")


def _render_count_chart(title: str, frame: pd.DataFrame, *, priority=False, negative_only=False) -> None:
    st.subheader(title)
    visible = frame.loc[frame["数量"] > 0].copy()
    if visible.empty:
        st.caption("暂无可统计的评价。")
        return
    label = frame.columns[0]
    active = alt.selection_point(fields=[label], on="pointerover", clear="pointerout")
    color = alt.value("#087F8C")
    if priority:
        color = alt.Color(f"{label}:N", scale=alt.Scale(domain=["高", "中", "低"], range=["#C15D4F", "#C69852", "#5B9096"]), legend=None)
    chart = alt.Chart(visible).mark_bar(cornerRadiusEnd=3, size=22).encode(
        x=alt.X("数量:Q", axis=alt.Axis(title="评价数", tickMinStep=1)),
        y=alt.Y(f"{label}:N", sort="-x", axis=alt.Axis(title=None, labelLimit=170)),
        color=color, opacity=alt.condition(active, alt.value(.95), alt.value(.45)),
        tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip("数量:Q", title="评价数")],
    ).add_params(active).properties(height=max(150, len(visible) * 36), description=f"{title}，每条有效评价计一次；悬停查看数量。")
    _linked_chart(chart, key="priority" if priority else "negative_category" if negative_only else "category",
                  field="priority" if priority else "category", column=label, negative_only=negative_only)


def _detail_table(frame: pd.DataFrame, report: dict, *, height=None) -> None:
    text_column = report.get("text_column")
    summary_column = report.get("detail_columns", {}).get("summary")
    configs = {}
    if text_column in frame.columns:
        configs[text_column] = st.column_config.TextColumn(text_column, width="large")
    if summary_column in frame.columns:
        configs[summary_column] = st.column_config.TextColumn(summary_column, width="medium")
    kwargs = {"height": height} if height else {}
    st.dataframe(frame, hide_index=True, width="stretch", column_config=configs, **kwargs)


def _render_overview(report: dict) -> None:
    metrics = report["metrics"]
    if not metrics["success_rows"]:
        st.info("本次还没有有效分析结果。请在评价明细中查看未完成原因。")
        return
    sentiment, categories = st.columns([1, 1.35], gap="large")
    with sentiment:
        st.subheader("顾客情感")
        _render_sentiment_chart(report)
    with categories:
        _render_count_chart("主要问题分布", report["category_counts"])
    insights = report.get("insights", [])
    if insights:
        st.subheader("本次发现")
        for insight in insights[:3]:
            st.markdown(f"**{insight['title']}**")
            st.write(insight["body"])
    st.caption("以上为整批有效评价分布。点击图表可查对应原文；类别不能直接解释问题原因。")


def _render_problem_insights(report: dict) -> None:
    ranking, priorities = st.columns([1.35, 1], gap="large")
    with ranking:
        _render_count_chart("差评主要集中在哪里", report.get("problem_ranking", report["category_counts"]), negative_only=True)
    with priorities:
        _render_count_chart("处理优先级", report["priority_counts"], priority=True)
    st.subheader("回到原文核对")
    st.caption("筛选差评证据，核对问题判断与摘要；优先级供安排跟进时参考。")
    evidence = report["negative_examples"]
    names = report.get("detail_columns", {})
    if evidence.empty:
        st.info("本次有效评价中没有差评。")
    else:
        topic_col, priority_col = st.columns(2)
        with topic_col:
            categories = list(evidence[names["category"]].drop_duplicates())
            selected_category = st.selectbox("差评问题", [None] + categories, format_func=lambda value: "全部问题" if value is None else value, key="customer_insight_category")
        with priority_col:
            priorities = list(evidence[names["priority"]].drop_duplicates())
            selected_priority = st.selectbox("差评优先级", [None] + priorities, format_func=lambda value: "全部优先级" if value is None else value, key="customer_insight_priority")
        shown = evidence
        if selected_category is not None:
            shown = shown[shown[names["category"]].eq(selected_category)]
        if selected_priority is not None:
            shown = shown[shown[names["priority"]].eq(selected_priority)]
        st.caption(f"符合条件 {len(shown)} 条差评。")
        _detail_table(shown, report)
    if report["metrics"]["high_priority_rows"]:
        with st.expander("查看全部高优先级评价"):
            detail = report["detail"]
            _detail_table(detail[detail[names["priority"]].isin(["高", "high", "高优先级"])], report)


def _render_group_comparison(report: dict) -> None:
    grouped = report["grouped"]
    if grouped.empty:
        st.info("本次未选择分组列。需要比较产品或门店时，请在上传设置中选择相应列后开始分析。")
        return
    st.subheader(f"按「{report.get('group_column', '分组')}」比较评价")
    controls, chart_area = st.columns([.75, 2], gap="large")
    with controls:
        metric = st.selectbox("对比指标", ["负面评价数", "负面占比", "高优先级数", "高优先级占比", "成功分析数"], key="customer_group_chart_metric")
        limit = st.select_slider("展示分组数", options=sorted(set([min(len(grouped), n) for n in (5, 10, 20)])), value=min(len(grouped), 10), key="customer_group_chart_limit")
        st.caption("每条原始评价归入其所属分组，相同文本复用分析后仍分别计数。")
        st.caption("占比的分母是各组有效评价数。小样本门店应结合评价数量判断。")
    with chart_area:
        shown = grouped.sort_values(metric, ascending=False, kind="stable").head(limit)
        percent = metric.endswith("占比")
        active = alt.selection_point(fields=["分组"], on="pointerover", clear="pointerout")
        chart = alt.Chart(shown).mark_bar(cornerRadiusEnd=3, size=22).encode(
            x=alt.X(f"{metric}:Q", axis=alt.Axis(title=metric, format=".0%" if percent else "d")),
            y=alt.Y("分组:N", sort="-x", axis=alt.Axis(title=None, labelLimit=160)),
            color=alt.value("#087F8C"), opacity=alt.condition(active, alt.value(.95), alt.value(.5)),
            tooltip=[alt.Tooltip("分组:N"), alt.Tooltip("原始记录数:Q"), alt.Tooltip("成功分析数:Q", title="有效评价"), alt.Tooltip("负面评价数:Q"), alt.Tooltip("负面占比:Q", format=".1%")],
        ).add_params(active).properties(height=max(180, len(shown) * 38), description=f"按{metric}排序的产品或门店对比。")
        _linked_chart(chart, key="groups", field="group", column="分组")
    st.dataframe(grouped, hide_index=True, width="stretch", column_config={
        "负面占比": st.column_config.ProgressColumn("差评占比", format="percent", min_value=0, max_value=1),
        "高优先级占比": st.column_config.ProgressColumn("高优先级占比", format="percent", min_value=0, max_value=1),
        "成功分析数": st.column_config.NumberColumn("有效评价"),
    })


def _render_details(report: dict) -> None:
    st.subheader("逐条评价明细")
    st.caption("所有原始行均保留。筛选与搜索只改变当前显示，不会重新发起分析。")
    detail = report["detail"]
    names = report.get("detail_columns", {})
    search = st.text_input("搜索评价原文或摘要", placeholder="输入关键词，例如：配送、破损、客服", key="customer_filter_search")
    controls = st.columns(4)
    filters = []
    for container, key, label in zip(controls[:3], ["sentiment", "priority", "processing_status"], ["情感", "优先级", "处理状态"]):
        with container:
            column = names[key]
            options = [value for value in detail[column].drop_duplicates().tolist() if value]
            selected = st.multiselect(label, options, placeholder="全部", key=f"customer_filter_{key}")
            filters.append((column, selected))
    with controls[3]:
        group_column = report.get("group_column")
        if group_column in detail.columns:
            options = list(dict.fromkeys(detail[group_column].fillna("（未填写）").map(str)))
            selected = st.multiselect("产品或门店", options, placeholder="全部", key="customer_filter_group")
            filters.append((group_column, selected))
        else:
            selected = st.multiselect("问题类别", list(detail[names["category"]].loc[detail[names["category"]].ne("")].drop_duplicates()), placeholder="全部", key="customer_filter_category")
            filters.append((names["category"], selected))
    shown = detail.copy()
    for column, values in filters:
        if values:
            shown = shown[shown[column].fillna("（未填写）").map(str).isin(values)]
    if search.strip():
        search_columns = [column for column in (report.get("text_column"), names.get("summary")) if column in shown.columns]
        mask = pd.Series(False, index=shown.index)
        for column in search_columns:
            mask |= shown[column].fillna("").map(str).str.contains(search.strip(), case=False, regex=False)
        shown = shown[mask]
    st.session_state["customer_visible_detail"] = shown.copy(deep=True)
    st.caption(f"当前显示 {len(shown)} / {len(detail)} 条评价。筛选不会改变上方整体统计。")
    if shown.empty:
        st.info("没有符合条件的评价，请减少筛选条件或换一个关键词。")
    _detail_table(shown, report, height=420)
    filtered_report = {**report, "detail": shown}
    st.download_button("下载当前筛选明细", export_customer_csv(filtered_report), "顾客评价筛选明细.csv", mime="text/csv", key="customer_download_filtered")


def _render_exports(report: dict, *, is_demo: bool) -> None:
    st.subheader("把分析结果交给团队")
    st.caption("下载不会重新分析。完整报告包含统计、图表、原文证据和全部明细。")
    details, document = st.columns(2, gap="large")
    try:
        if api_ui_enabled() and not is_demo:
            batch_id = st.session_state["customer_active_batch_id"]
            csv_data = _job_client().export(batch_id, "csv")
            context = report.get("historical_context")
            comparison_id = context.get("batch", {}).get("id") if isinstance(context, dict) else None
            html_data = (_job_client().export(batch_id, "html", comparison_id=comparison_id)
                         if comparison_id else _job_client().export(batch_id, "html"))
        else:
            csv_data, html_data = export_customer_csv(report), export_customer_html(report)
    except JobClientError as error:
        st.warning(str(error))
        return
    with details:
        st.markdown("**逐条分析明细**")
        st.write("CSV 可直接用 Excel 打开，保留原表字段、分析标签、摘要和处理状态。")
        st.download_button("下载逐条分析明细 CSV", csv_data, "顾客评价分析明细.csv", mime="text/csv", key="customer_download_csv", width="stretch")
    with document:
        st.markdown("**完整分析报告**")
        st.write("HTML 报告可离线打开；在浏览器中打印即可另存为 PDF，适合分享与归档。")
        st.download_button("下载完整分析报告", html_data, "顾客评价分析报告.html", mime="text/html", key="customer_download_report", type="primary", width="stretch")
    if is_demo:
        st.info("这是合成示例的预置分析，不代表真实店铺或模型准确率。" if report.get("data_origin") == "synthetic"
                else "这是历史公开样例的报告，不能代表你自己的店铺情况。")
    selected_answers = st.session_state.get("customer_agent_brief", [])
    if selected_answers:
        from core.agent_brief import render_agent_brief
        try:
            brief = render_agent_brief(report, selected_answers)
        except ValueError:
            st.caption("所选回答与当前报告范围不同，请重新将当前回答加入简报。")
        else:
            st.markdown(f"**经营简报 · 已选 {len(selected_answers)} 个回答**")
            st.download_button("下载经营简报（含问答、图表与原文）", brief, "CustomerInsight经营简报.html",
                               mime="text/html", key="customer_download_brief", width="stretch")
    st.markdown("**保存与清除**")
    st.write("报告含原始评价，请妥善保存。清除当前页面只关闭本次会话，已保存报告仍可从左侧历史打开；删除历史请使用历史管理。")
    st.button("清除本次上传与结果", key="customer_clear_export", on_click=clear_customer_data)


def _agent_result_markdown(result: AnalysisAgentResult) -> bytes:
    lines = [
        "# CustomerInsight 经营建议", "", f"问题：{result.question}", "",
        "## 结论", "", result.conclusion,
    ]
    if result.conclusion_metrics:
        lines.extend(["", "依据：" + "；".join(result.conclusion_metrics)])
    if result.evidence_rows:
        lines.extend(["", "参考原表行：" + "、".join(map(str, result.evidence_rows))])
    for reference in getattr(result, "historical_evidence", []):
        lines.extend(["", f"上批《{reference['filename']}》第 {reference['source_row']} 行：{reference['text']}"])
    for heading, points in (("主要问题", result.findings), ("建议下一步", result.actions)):
        lines.extend(["", f"## {heading}", ""])
        for point in points:
            lines.append(f"- **{point.title}**：{point.narrative}")
            if point.metrics:
                lines.append("  - 统计依据：" + "；".join(point.metrics))
            if point.evidence_rows:
                lines.append("  - 核对表格原始行：" + "、".join(map(str, point.evidence_rows)))
    for chart in result.charts:
        lines.extend(["", f"## {chart.title}", "", chart.denominator_note, "",
                      "| 项目 | 数值 | 分子 / 分母 |", "| --- | ---: | ---: |"])
        for point in chart.data:
            label = (point.series + " · " if point.series else "") + point.label
            label = label.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {label} | {point.value:g}{chart.unit} | {point.numerator} / {point.denominator} |")
    lines.extend(["", "## 适用范围", "", result.scope_note, "",
                  f"模型请求：{result.model_calls} 次；本地只读工具：{result.tool_calls} 个。"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _add_brief_answer(result: dict) -> None:
    answers = st.session_state.setdefault("customer_agent_brief", [])
    # A regenerated answer replaces the earlier version of the same question.
    answers = [item for item in answers if not (item.get("question") == result.get("question")
               and item.get("report_fingerprint") == result.get("report_fingerprint"))]
    st.session_state["customer_agent_brief"] = (answers + [result])[-10:]
    st.toast("已加入简报，可在“明细与下载”中下载。")


def _render_agent_charts(result: AnalysisAgentResult) -> None:
    for index, chart in enumerate(result.charts):
        frame = pd.DataFrame([point.model_dump() for point in chart.data])
        if frame.empty:
            continue
        st.markdown(f"**{chart.title}**")
        st.caption(chart.denominator_note)
        encoding = {
            "x": alt.X("value:Q", title=f"数值（{chart.unit}）"),
            "y": alt.Y("label:N", title=None, sort="-x", axis=alt.Axis(labelLimit=160)),
            "tooltip": [alt.Tooltip("label:N", title="项目"), alt.Tooltip("value:Q", title=chart.unit, format=".2f"),
                        alt.Tooltip("numerator:Q", title="分子"), alt.Tooltip("denominator:Q", title="分母")],
            "color": alt.value("#087F8C"),
        }
        if frame["series"].ne("").any():
            encoding.update(color=alt.Color("series:N", title=None), yOffset="series:N")
        visual = alt.Chart(frame).mark_bar(cornerRadiusEnd=3).encode(**encoding).properties(height=max(170, len(frame) * 28))
        st.altair_chart(_chart_style(visual), use_container_width=True)


def _render_agent_result(report: dict, result: AnalysisAgentResult, *, turn_key: str = "") -> None:
    if result.status == "fallback":
        st.markdown('<span class="ci-agent-status fallback">回答未完成</span>', unsafe_allow_html=True)
        st.warning("本轮模型回答未完成，或引用未通过检查。你可以重新发送问题，已有分析报告仍可使用。")
    elif result.answer_origin == "llm":
        label = "LLM 回答" if result.answer_type == "supported" else "LLM 回答 · 需补充信息"
        st.markdown(f'<span class="ci-agent-status">{label}</span>', unsafe_allow_html=True)
    elif result.scope_note.startswith("离线示例回放") and result.model_calls == 0:
        st.markdown('<span class="ci-agent-status">离线回放 · 预置回答</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="ci-agent-status fallback">历史模板回答</span>', unsafe_allow_html=True)
    st.markdown(result.conclusion)
    _render_agent_charts(result)
    if result.findings or result.actions:
        # Older stored conversations retain their original answer format.
        columns = st.columns([1, 1], gap="large")
        for container, heading, points in zip(
            columns, ("主要发现", "建议下一步"), (result.findings, result.actions),
        ):
            with container:
                if points:
                    st.subheader(heading)
                for point in points:
                    st.markdown(f"**{point.title}**")
                    st.write(point.narrative)
                    if point.metrics:
                        st.caption("统计依据：" + "；".join(point.metrics))
                    if point.evidence_rows:
                        st.caption("核对表格原始行：" + "、".join(map(str, point.evidence_rows)))
    evidence_rows = sorted(set(result.evidence_rows) | {row for point in result.findings + result.actions for row in point.evidence_rows})
    if evidence_rows:
        st.caption("参考原表第 " + "、".join(map(str, evidence_rows)) + " 行")
    names, detail = report.get("detail_columns", {}), report.get("detail")
    source_column = names.get("source_row")
    if evidence_rows and isinstance(detail, pd.DataFrame) and source_column in detail.columns:
        evidence = detail.loc[detail[source_column].isin(evidence_rows)]
        with st.expander("核对引用的评价原文"):
            _detail_table(evidence, report)
    history_evidence = getattr(result, "historical_evidence", [])
    if history_evidence:
        with st.expander("核对上批引用的原文"):
            for reference in history_evidence:
                st.caption(f"上批《{reference['filename']}》第 {reference['source_row']} 行")
                st.text(reference["text"])
    with st.expander("查看回答依据与运行信息"):
        st.caption(result.scope_note)
        if result.conclusion_metrics:
            st.caption("使用的统计：" + "；".join(result.conclusion_metrics))
        used_tools = [TOOL_LABELS[name] for name in result.tool_trace if name in TOOL_LABELS]
        if used_tools:
            st.caption("本地查询：" + "、".join(used_tools))
        usage = f"；服务返回总用量 {result.total_tokens} token" if result.total_tokens is not None else ""
        st.caption(f"实际模型请求 {result.model_calls} 次；执行本地只读查询 {result.tool_calls} 个{usage}。")
    st.download_button(
        "下载本次经营建议", _agent_result_markdown(result), "经营建议.md",
        mime="text/markdown", key=f"customer_agent_download{turn_key}",
    )
    if result.status == "completed":
        st.button("加入经营简报", key=f"customer_agent_brief_add{turn_key}",
                  on_click=_add_brief_answer, args=(result.model_dump(mode="json"),))


def _queue_agent_question() -> None:
    """A widget callback can clear the composer before the next script run."""
    st.session_state["customer_agent_pending_question"] = str(
        st.session_state.get("customer_agent_question", "")
    ).strip()
    st.session_state["customer_agent_question"] = ""


def _agent_turns(fingerprint: str) -> list[dict]:
    """Load only validated conversation entries owned by this report/session."""
    saved = st.session_state.get("customer_agent_history", [])
    # Preserve a previous one-shot answer when Streamlit reruns updated code.
    if not saved and st.session_state.get("customer_agent_result") is not None:
        result = AnalysisAgentResult.model_validate(st.session_state["customer_agent_result"])
        saved = [{"question": result.question, "result": result.model_dump(mode="json")}]
    if not isinstance(saved, list):
        raise ValueError("对话记录无效。")
    turns = []
    for turn in saved[-MAX_CHAT_TURNS:]:
        if not isinstance(turn, dict) or not isinstance(turn.get("question"), str):
            raise ValueError("对话记录无效。")
        if not 1 <= len(turn["question"]) <= MAX_QUESTION_CHARACTERS:
            raise ValueError("对话问题长度无效。")
        if turn.get("result") is not None:
            result = AnalysisAgentResult.model_validate(turn["result"])
            if result.report_fingerprint != fingerprint or result.question != turn["question"]:
                raise ValueError("对话记录不属于当前报告。")
            turns.append({"question": result.question, "result": result.model_dump(mode="json")})
        else:
            turns.append({"question": turn["question"], "result": None,
                          "error": "回答暂时未生成，请重新发送问题。"})
    return turns


def _render_agent_turn(report: dict, turn: dict, index: int, *, latest: bool) -> None:
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        if turn.get("result") is None:
            st.error("回答暂时未生成，请重新发送问题。")
            return
        result = AnalysisAgentResult.model_validate(turn["result"])
        if latest:
            _render_agent_result(report, result, turn_key=f"_{index}")
        else:
            st.write(result.conclusion)
            with st.expander("查看完整回答与依据"):
                _render_agent_result(report, result, turn_key=f"_{index}")


def _agent_conversation_markdown(turns: list[dict]) -> bytes:
    sections = ["# CustomerInsight 对话记录\n"]
    for index, turn in enumerate(turns, 1):
        sections.append(f"## 第 {index} 轮\n\n问题：{turn['question']}\n")
        if turn.get("result") is not None:
            answer = _agent_result_markdown(AnalysisAgentResult.model_validate(turn["result"]))
            sections.append(answer.decode("utf-8").replace("# CustomerInsight 经营建议\n", "", 1))
        else:
            sections.append("回答暂时未生成。\n")
    return "\n".join(sections).encode("utf-8")


def _activate_demo_workspace() -> None:
    """Save only fixed synthetic records; the subsequent chat needs its own consent."""
    from core.demo_workspace import save_demo_workspace

    question = st.session_state.get("customer_agent_question", "")
    try:
        if api_ui_enabled():
            identifiers = _job_client().import_demo_workspace()
            current = decode_view(_job_client().view_batch(identifiers["current_batch_id"], identifiers["previous_batch_id"]))
        else:
            store = _history_store()
            identifiers = save_demo_workspace(store)
            current = store.load_batch(identifiers["current_batch_id"])
        _restore_batch(current, history_view=True)
        st.session_state["customer_previous_batch_id"] = identifiers["previous_batch_id"]
        if question:
            st.session_state["customer_agent_question"] = question
    except Exception:
        st.error("示例暂未保存，请先确认已登录且后台服务可用；离线回放仍可使用。")
        return
    st.rerun()


def _render_demo_agent(report: dict, status: dict) -> None:
    workspace = st.session_state.get("customer_demo_workspace")
    if not workspace:
        st.info("公开英文样例只展示已保存结果。可返回体验中文完整示例，查看两轮回放和批次比较。")
        return
    st.caption("先体验两轮离线回放：内容由固定合成评价计算与预置，未调用 LLM。")
    count = min(2, int(st.session_state.get("customer_demo_replay_count", 0)))
    for index, payload in enumerate(workspace["replay_turns"][:count]):
        _render_agent_turn(report, {"question": payload["question"], "result": payload}, index, latest=index == count - 1)
    if count < 2:
        st.write(workspace["questions"][count])
        if st.button("回放第一轮" if count == 0 else "继续回放第二轮", key="customer_demo_replay", type="primary"):
            st.session_state["customer_demo_replay_count"] = count + 1
            st.rerun()
    else:
        st.button("重新回放", key="customer_demo_replay_reset", on_click=lambda: st.session_state.update(customer_demo_replay_count=0))
    st.divider()
    st.markdown("**想自由提问？**")
    st.caption("先把这两批合成示例保存到自己的历史工作区，再明确发送问题。保存不调用模型；实时问答使用部署者配置的分析服务。")
    allowed = not api_ui_enabled() or bool(st.session_state.get("ci_account_token"))
    if not allowed:
        st.caption("登录后即可保存示例并使用实时问答。")
    if st.button("保存示例并进入实时问答", key="customer_demo_activate", disabled=not allowed, width="stretch"):
        _activate_demo_workspace()


def _render_analysis_agent(report: dict, status: dict, *, is_demo=False) -> None:
    st.subheader("继续问这份报告")
    st.caption("可以连续追问；信息不够时，Agent 会查询原表再回答。")
    if is_demo:
        _render_demo_agent(report, status)
        return

    fingerprint = report_fingerprint(report)
    saved_key = st.session_state.get("customer_agent_report_key")
    if saved_key is not None and saved_key != fingerprint:
        _clear_agent_state()
    if api_ui_enabled() and st.session_state.get("customer_agent_loaded_scope") != fingerprint:
        try:
            st.session_state["customer_agent_history"] = _job_client().chat_history(
                st.session_state["customer_active_batch_id"],
                st.session_state.get("customer_previous_batch_id"))["items"]
            st.session_state["customer_agent_loaded_scope"] = fingerprint
        except JobClientError as error:
            st.warning(str(error))
            return
    try:
        turns = _agent_turns(fingerprint)
    except (ValueError, TypeError):
        _clear_agent_state()
        turns = []
    history_slot = st.container()
    with history_slot:
        for index, turn in enumerate(turns):
            _render_agent_turn(report, turn, index, latest=index == len(turns) - 1)
    if turns:
        conversation_download, clear_chat = st.columns([1, 1])
        with conversation_download:
            st.download_button("下载对话记录", _agent_conversation_markdown(turns), "评价分析对话.md",
                               mime="text/markdown", key="customer_agent_download_chat")
        with clear_chat:
            st.button("清空对话", key="customer_agent_clear_chat", on_click=_clear_saved_chat)

    metrics = report["metrics"]
    if not metrics["success_rows"]:
        st.info("本次没有有效评价，无法生成经营建议。请先查看评价明细中的处理状态。")
    elif metrics["success_rows"] < metrics["total_rows"]:
        st.warning(f"本次仅基于 {metrics['success_rows']} 条有效评价生成建议，未覆盖记录不会进入结论。")
    st.markdown("**可以从这些问题开始**")
    for index, suggestion in enumerate(recommended_questions(report)):
        st.button(suggestion, key=f"customer_agent_suggestion_{index}", on_click=_prefill_question,
                  args=(suggestion,), width="stretch")
    focus = st.session_state.get("customer_focus")
    if focus:
        st.caption("当前关注：" + focus_label(report, focus) + "。整体统计和图表仍以整批评价为范围。")
    question = st.text_area(
        "你还想了解什么？", value="" if turns else DEFAULT_AGENT_QUESTION,
        max_chars=MAX_QUESTION_CHARACTERS, key="customer_agent_question",
        placeholder="例如：刚才提到的问题，有哪些评价能说明？" if turns else "最需要优先处理什么？",
        help="可以接着上一轮提问，也可以针对当前报告换一个话题。",
    )
    consent = st.checkbox(
        "我同意发送本次对话、当前统计及 Agent 查询到的原表内容用于回答。",
        key="customer_agent_consent",
    )
    configured = bool(status.get("configured"))
    remaining = int(status.get("remaining_today", 0))
    required = int(status.get("agent_requests_per_run", DEFAULT_MAX_MODEL_CALLS))
    with st.expander("数据与请求说明"):
        st.caption("回答正文由 LLM 生成。它可以检索所有情感的评价、读取指定原始行及字段、汇总表格列；引用的统计与行号会在本地检查。")
        st.caption("Agent 只读取当前表格和你明确选择的对比批次，不联网搜索。引用存在性检查不等于模型的全部解释或因果判断都已证实。")
        st.caption(f"已归档报告的对话保存在本地，最多显示最近 {MAX_CHAT_TURNS} 轮；每次结合最近 {MAX_HISTORY_TURNS} 轮回答。切换比较对象会重新核对对话范围。")
        st.caption(f"每次提问最多 {required} 次模型请求；今日剩余 {remaining} 次。查看图表、筛选和下载不会产生请求。")
    if not configured:
        st.warning("分析服务尚未配置，请联系管理员开通经营建议功能。")
    elif remaining < required:
        st.warning(f"经营建议需要预留 {required} 次请求，今日剩余 {remaining} 次。")
    pending_question = st.session_state.get("customer_agent_pending_question", "")
    can_run = (configured and metrics["success_rows"] > 0 and remaining >= required and consent
               and bool(question.strip() or pending_question))
    if st.button("继续提问" if turns else "向 Agent 提问", key="customer_agent_generate", type="primary",
                 disabled=not can_run, on_click=_queue_agent_question):
        clean_question = st.session_state.pop("customer_agent_pending_question", "")
        if not clean_question:
            return
        prior_answers = [AnalysisAgentResult.model_validate(turn["result"])
                         for turn in turns if turn.get("result") is not None]
        try:
            started = time.perf_counter()
            with st.status("正在读取报告并核对依据…", expanded=True) as activity:
                if api_ui_enabled():
                    st.write("问题已提交到工作区，正在等待分析服务返回；请勿重复发送。")
                    result = AnalysisAgentResult.model_validate(_job_client().chat(
                        st.session_state["customer_active_batch_id"], clean_question,
                        comparison_id=st.session_state.get("customer_previous_batch_id"),
                        max_requests=required)["result"])
                else:
                    def on_progress(event):
                        if event.get("message"):
                            st.write(event["message"])
                    result = run_customer_report_agent(report, clean_question, history=prior_answers, progress_callback=on_progress)
                used_tools = [TOOL_LABELS.get(name, name) for name in result.tool_trace]
                if used_tools:
                    st.write("已完成查询：" + "、".join(used_tools))
                activity.update(label=f"回答已返回，用时 {time.perf_counter() - started:.1f} 秒", state="complete", expanded=False)
            result = AnalysisAgentResult.model_validate(result.model_dump(mode="json"))
            if result.report_fingerprint != fingerprint or result.question != clean_question:
                raise ValueError("回答不属于当前问题与报告。")
            st.session_state["customer_agent_result"] = result.model_dump(mode="json")
            st.session_state["customer_agent_report_key"] = fingerprint
            st.session_state["customer_agent_question_used"] = clean_question
            new_turn = {"question": clean_question, "result": result.model_dump(mode="json")}
        except Exception:
            new_turn = {"question": clean_question, "result": None,
                        "error": "回答暂时未生成，请重新发送问题。"}
        st.session_state["customer_agent_history"] = (turns + [new_turn])[-MAX_CHAT_TURNS:]
        st.session_state["customer_agent_report_key"] = fingerprint
        _save_current_chat(st.session_state["customer_agent_history"])
        # The explicit submission has been consumed. Rerender the transcript and
        # empty composer without resending any request.
        st.rerun()


def render_customer_report(report: dict, status: dict, *, is_demo=False) -> None:
    st.markdown('<div id="report" class="ci-section"></div>', unsafe_allow_html=True)
    st.header("分析结果")
    period = report.get("period", {})
    if period.get("date_column"):
        st.caption(f"评价日期：{period.get('label', '导入批次')}；可识别 {period.get('valid_rows', 0)} 条，缺失或无效 {period.get('invalid_rows', 0)} 条。")
    if report.get("data_origin") == "synthetic":
        st.info("两批中文合成评价 · 预置分析标签 · 示例结果不代表真实业务。" +
                ("查看回放不调用模型。" if is_demo else "实时提问仍需明确确认；历史中的示例会保留此来源说明。"))
    elif is_demo:
        st.info("历史公开样例 · 已保存结果，不消耗今日额度。上传自己的表格后即可生成专属分析。")
    st.markdown("**本次评价总结**")
    st.write(build_report_summary(report))
    metrics = report["metrics"]
    total, success, negative, high = st.columns(4)
    total.metric("上传评价", metrics["total_rows"])
    success.metric("有效评价", metrics["success_rows"])
    negative.metric("差评占比", f"{metrics['negative_rate']:.1%}" if metrics["success_rows"] else "—")
    high.metric("高优先级评价", metrics["high_priority_rows"])
    reused = metrics.get("reused_rows", 0)
    independently_analyzed = metrics.get("model_success_rows", metrics["success_rows"])
    unresolved_duplicates = metrics.get("unresolved_duplicate_rows", metrics.get("skipped_duplicate_rows", 0))
    skipped = metrics["skipped_empty_rows"] + metrics["skipped_invalid_rows"] + unresolved_duplicates
    meta = report.get("analysis_meta", {})
    if metrics["pending_rows"] or metrics["failed_rows"] or unresolved_duplicates or meta.get("run_incomplete"):
        st.warning("部分评价未完成分析。统计仅反映有效评价，请在评价明细中查看处理状态和原因。")
    results_tab, agent_tab, data_tab = st.tabs(["分析结果", "问 Agent", "明细与下载"])
    with results_tab:
        _render_topics(report)
        if not is_demo:
            report = _render_history_comparison(report)
        elif st.session_state.get("customer_demo_workspace"):
            _render_demo_comparison(report)
        _render_overview(report)
        with st.expander("查看问题证据与处理优先级"):
            _render_problem_insights(report)
        grouped = report.get("grouped")
        if isinstance(grouped, pd.DataFrame) and not grouped.empty:
            with st.expander("查看产品或门店对比"):
                _render_group_comparison(report)
        _render_focused_evidence(report)
    with agent_tab:
        _render_analysis_agent(report, status, is_demo=is_demo)
    with data_tab:
        _render_details(report)
        st.divider()
        _render_exports(report, is_demo=is_demo)
    with st.expander("查看数据范围与统计说明"):
        if report.get("data_origin") == "synthetic":
            st.caption(f"本批 {metrics['total_rows']} 条合成评价，标签由开发者预置，统计由程序计算；不是独立模型分析。")
        else:
            st.caption(
            f"差评占比以 {metrics['success_rows']} 条有效评价为分母；实际分析成功 {independently_analyzed} 条，"
            f"相同文本复用 {reused} 条。失败 {metrics['failed_rows']} 条，跳过 {skipped} 条，尚未处理 {metrics['pending_rows']} 条；"
            "这些未覆盖记录不会被算作好评或中性评价。"
            )
        if meta:
            if is_demo:
                st.caption(f"历史样例当时使用 {meta.get('actual_requests', '未记录')} 次独立请求；本次只加载已保存结果。")
            else:
                actual = meta.get("actual_requests")
                planned = meta.get("planned_requests")
                elapsed = meta.get("elapsed_seconds")
                timing = f"，用时 {float(elapsed):.1f} 秒" if elapsed is not None else ""
                if actual is not None:
                    st.caption(f"本次实际使用 {actual} 次请求（预计 {planned if planned is not None else '未记录'} 次）{timing}。")
                    if meta.get("run_incomplete"):
                        st.caption("本次运行中断，已保留收到的结果；没有返回的评价保持待处理。")
                elif meta.get("run_incomplete"):
                    st.caption("本次分析已中断，实际请求用量未完整返回。以下保留已收到的结果，未返回的评价仍标为待处理。")
        for note in report.get("notes", []):
            st.caption(str(note).replace("模型", "自动分析"))


def _render_demo_comparison(report: dict) -> None:
    context = report.get("historical_context", {})
    comparison = context.get("comparison", {})
    with st.expander("与示例上批比较", expanded=False):
        st.caption("两批各 30 条合成评价；仅比较批次差异，不证明时间趋势或改进效果。")
        changes = comparison.get("topics", [])
        if comparison.get("topics_comparable") and changes:
            st.dataframe(pd.DataFrame([{
                "具体主题": item.get("topic", item.get("key", "")),
                "本批条数": item["current_count"], "上批条数": item["previous_count"],
                "本批占比": f"{item['current_share']:.1%}", "上批占比": f"{item['previous_share']:.1%}",
                "变化（百分点）": round(item["change_pp"], 1),
            } for item in changes]), hide_index=True, width="stretch")
        st.caption("上批原文可在第二轮示例回放中核对，也可以下载两批原表。")


def _load_demo_workspace() -> None:
    from core.demo_workspace import load_demo_workspace
    from core.agent_charts import build_agent_charts
    try:
        workspace = load_demo_workspace()
        for index, answer in enumerate(workspace["replay_turns"]):
            requests = [{"source": "topics", "limit": 5}] if index == 0 else [{"source": "group_negative_rate", "limit": 5}, {"source": "history_sentiment", "limit": 5}]
            answer["charts"] = [chart.model_dump(mode="json") for chart in build_agent_charts(workspace["current_report"], requests)]
        _clear_results()
        st.session_state["customer_demo_workspace"] = workspace
        st.session_state["customer_demo_report"] = workspace["current_report"]
        st.session_state["customer_demo_replay_count"] = 0
    except Exception:
        st.error("完整示例暂时无法读取，请检查示例文件；你仍可以上传自己的表格。")


def _load_demo_report() -> None:
    """Load reviewed public artifacts without a client, key or new request."""
    data_path = ROOT / "datasets" / "public_reviews"
    sample = data_path / "customer_reviews_sample.xlsx"
    result_file = data_path / "demo_results.json"
    try:
        content = json.loads(result_file.read_text(encoding="utf-8"))
        results = [FeedbackResult.model_validate(item) for item in content["results"]]
        frame = read_feedback_xlsx(sample.read_bytes(), sample.name)
        report = build_customer_report(frame, results_to_dataframe(results), "feedback", group_column="source")
        report["analysis_meta"] = content.get("analysis_meta", {})
        report["notes"].append("示例数据来源：UCI Sentiment Labelled Sentences，Dimitrios Kotzias，2015，CC BY 4.0。历史英文评价摘录，仅用于展示；来源：https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences")
        _clear_results()
        st.session_state["customer_demo_report"] = report
    except Exception:
        st.error("示例报告暂时无法读取，请先下载公开样例，或联系管理员检查示例文件。")


def render_sidebar(status: dict) -> None:
    with st.sidebar:
        st.markdown('<div class="ci-brand">CustomerInsight<small>顾客评价分析</small></div>', unsafe_allow_html=True)
        st.caption("当前表格")
        if "customer_demo_report" in st.session_state:
            st.write("两批中文合成示例" if "customer_demo_workspace" in st.session_state else "历史公开样例")
        elif st.session_state.get("customer_file_name"):
            st.write(st.session_state["customer_file_name"])
        else:
            st.write("尚未上传表格")
        st.divider()
        _render_history_library()
        with st.expander("模板、示例与使用说明"):
            workspace = st.session_state.get("customer_demo_workspace")
            if workspace:
                st.download_button("下载两批示例原表", workspace["workbook_bytes"], workspace["workbook_filename"],
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="customer_demo_original")
            render_samples()
            st.caption("一行写一条评价；保留产品或门店列，便于分组比较。")
            st.caption("分析后，原表与结果保存在部署机器的历史记录中。清除当前页面不会删除历史；可在历史管理中下载、备份或删除。")


def _render_history_library() -> None:
    with st.expander("历史报告", expanded=bool(st.session_state.get("customer_history_view"))):
        try:
            store = _history_store()
            records = store.list_batches()
        except Exception:
            st.warning("历史记录暂时无法读取，请检查本地数据目录。")
            return
        if not records:
            st.caption("完成第一次分析后，这里会保存原表与报告。")
            return
        by_id = {record["id"]: record for record in records}
        options = list(by_id)
        selected = st.selectbox("选择历史报告", options,
            format_func=lambda value: _history_batch_label(by_id[value]),
            key="customer_history_selection")
        st.caption(f"{by_id[selected]['total_rows']} 条原始记录 · {by_id[selected]['success_rows']} 条有效评价")
        if st.button("打开历史报告", key="customer_open_history", width="stretch"):
            try:
                _restore_batch(store.load_batch(selected), history_view=True)
                st.rerun()
            except Exception:
                st.error("历史报告无法打开，文件或记录可能已损坏。")
        st.button("新建分析", key="customer_new_analysis", on_click=clear_customer_data, width="stretch")
        with st.expander("原件、备份与删除"):
            try:
                original = store.read_original(selected)
                st.download_button("下载原始表格", original, by_id[selected]["filename"],
                                   key="customer_download_original", width="stretch")
            except Exception:
                st.warning("原始表格暂不可读取，请从已有备份恢复；系统不会加载缺少原件校验的报告。")
            st.caption("历史报告备份包含原表、报告和对话，不包括后台任务数据库与额度台账。完整备份请先停止服务，再备份部署数据目录（Docker 中为 /data）。")
            if st.button("准备历史报告备份", key="customer_prepare_backup", width="stretch"):
                try:
                    st.session_state["customer_backup_blob"] = store.export_backup()
                except Exception:
                    st.error("备份未生成，请检查磁盘和历史文件完整性。")
            backup = st.session_state.get("customer_backup_blob")
            if backup:
                st.download_button("下载全部历史备份", backup, "CustomerInsight历史备份.zip",
                                   mime="application/zip", key="customer_download_backup", width="stretch")
            try:
                related_jobs = _history_has_related_jobs(store, by_id[selected])
            except Exception:
                related_jobs = True
                st.warning("暂时无法核对后台任务关联，当前请保留这份历史。")
            if related_jobs:
                st.info("后台任务原件和报告需一起清理，当前请保留。")
            confirmed = st.checkbox("确认删除所选报告、原表和对话", key=f"customer_delete_confirm_{selected}",
                                    disabled=related_jobs)
            if st.button("删除所选历史", key="customer_delete_history", disabled=related_jobs or not confirmed):
                try:
                    if not _delete_history_if_unreferenced(store, by_id[selected]):
                        st.warning("后台任务原件和报告需一起清理，当前请保留。")
                        return
                    st.session_state.pop("customer_backup_blob", None)
                    if st.session_state.get("customer_active_batch_id") == selected:
                        clear_customer_data()
                    st.rerun()
                except Exception:
                    st.error("删除未完成，请重新检查所选报告。")


def render_upload(status: dict) -> None:
    st.markdown('<div id="upload"></div>', unsafe_allow_html=True)
    if st.session_state.get("customer_history_view") and "customer_report" in st.session_state:
        st.caption(f"已保存报告：{st.session_state.get('customer_file_name', '')}。打开历史不产生模型请求。")
        st.button("上传新表格", key="customer_upload_new_from_history", on_click=clear_customer_data)
        render_customer_report(st.session_state["customer_report"], status)
        return
    has_report = "customer_report" in st.session_state or "customer_demo_report" in st.session_state
    with st.expander("上传评价表", expanded=not has_report):
        introduction, files = st.columns([1.5, 1], gap="large")
        with introduction:
            st.subheader("上传顾客评价表")
            row_limit = 1000 if queued_analysis_enabled() else Config.MAX_BATCH_ROWS
            st.caption(f"Excel（.xlsx）或 CSV，每张表最多 {row_limit} 条评价、5 MB。第一行为列名，一行一条评价。")
            nonce = st.session_state.get("customer_upload_nonce", 0)
            uploaded = st.file_uploader(
                "选择评价表格", type=["xlsx", "csv", "xls"], key=f"customer_upload_{nonce}",
                help="不要上传含敏感个人信息的文件。旧版 .xls 请先另存为 .xlsx。",
            )
        with files:
            st.subheader("先体验一份完整分析")
            st.write("从具体问题到原文查证，再看两轮追问和上批比较。")
            if uploaded is None:
                st.button("体验完整示例", key="customer_view_workspace", type="primary", width="stretch", on_click=_load_demo_workspace)
                st.caption("两批中文合成评价，打开即看，不调用模型。")
            if uploaded is None and (ROOT / "datasets" / "public_reviews" / "demo_results.json").is_file():
                st.button("查看公开英文样例", key="customer_view_demo", width="stretch", on_click=_load_demo_report)
        if uploaded is not None:
            _render_uploaded_file(uploaded, status)
    if uploaded is None:
        if "customer_demo_report" in st.session_state:
            render_customer_report(st.session_state["customer_demo_report"], status, is_demo=True)
        else:
            st.session_state.pop("customer_file_name", None)
            _clear_results()
            st.markdown('<div class="ci-empty"><h3>上传表格，开始分析</h3><p>系统会生成顾客情感、主要问题和分组对比图表。分析后还可以继续向 Agent 提问。</p></div>', unsafe_allow_html=True)
    elif "customer_report" in st.session_state:
        render_customer_report(st.session_state["customer_report"], status)


def _store_snapshot_report(snapshot: dict, frame: pd.DataFrame, text_column: str, group_column: str | None) -> None:
    """Build a same-session report from callbacks, leaving untouched rows pending."""
    if not snapshot.get("received"):
        return
    if st.session_state.get("customer_input_key") != snapshot["input_key"]:
        return
    result_frame = results_to_dataframe([FeedbackResult.model_validate(item) for item in snapshot["rows"]])
    report = build_customer_report(frame, result_frame, text_column, group_column=group_column)
    meta = dict(snapshot.get("analysis_meta", {}))
    if not snapshot.get("complete"):
        meta["run_incomplete"] = True
        meta.setdefault("planned_requests", snapshot["planned_requests"])
    report["analysis_meta"] = meta
    report["period"] = date_scope(frame, snapshot.get("mapping", {}).get("date_column"))
    st.session_state["customer_results"] = result_frame
    st.session_state["customer_report"] = report


def _render_remote_upload(uploaded, status: dict) -> None:
    """Render API-owned parsing and planning; never touch a local data store."""
    st.session_state.pop("customer_demo_report", None)
    st.session_state.pop("customer_demo_workspace", None)
    st.session_state["customer_file_name"] = uploaded.name
    st.button("清除本次上传与结果", key="customer_clear", on_click=clear_customer_data)
    data = uploaded.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    extension = Path(uploaded.name).suffix.lower()
    if extension not in {".xlsx", ".csv"}:
        st.error("请上传 .xlsx 或 .csv 文件；旧版 .xls 请先另存为 .xlsx。")
        return
    encoding = "utf-8-sig"
    if extension == ".csv":
        label = st.selectbox("CSV 编码", ["UTF-8（推荐）", "GB18030 / GBK"], key="customer_csv_encoding")
        encoding = "utf-8-sig" if label.startswith("UTF-8") else "gb18030"
    try:
        client = _job_client()
        remote = st.session_state.get("customer_remote_upload")
        if not remote or (remote.get("digest"), remote.get("encoding"), remote.get("filename")) != (digest, encoding, uploaded.name):
            _clear_results()
            remote = {"digest": digest, "encoding": encoding, "filename": uploaded.name,
                      "manifest": client.upload(uploaded.name, data, encoding=encoding)}
            st.session_state["customer_remote_upload"] = remote
        manifest = remote["manifest"]
        sheets = manifest["sheet_names"]
        sheet = st.selectbox("选择工作表", sheets, key=f"customer_sheet_{digest}") if sheets else None
        if len(sheets) > 1:
            st.caption("本次只分析当前选中的工作表，其他工作表不会自动分析。")
        preview = client.preview(manifest["upload_id"], sheet_name=sheet, encoding=encoding)
        columns, suggested = preview["columns"], preview["suggested_mapping"]
        selection_key = f"{digest}_{sheet}_{encoding}"
        st.subheader("确认评价与分组列")
        text_slot, group_slot = st.columns(2)
        with text_slot:
            text_column = st.selectbox("哪一列是顾客评价？", columns,
                index=columns.index(suggested["text_column"]) if suggested["text_column"] in columns else 0,
                key=f"customer_text_{selection_key}")
        with group_slot:
            options = [None] + [column for column in columns if column != text_column]
            group_column = st.selectbox("按产品或门店汇总（可选）", options,
                index=options.index(suggested["group_column"]) if suggested["group_column"] in options else 0,
                format_func=lambda value: "不分组" if value is None else value,
                key=f"customer_group_{selection_key}_{text_column}")
        with st.expander("评价日期（可选）"):
            options = [None] + [column for column in columns if column not in (text_column, group_column)]
            date_column = st.selectbox("哪一列是评价日期？", options,
                index=options.index(suggested["date_column"]) if suggested["date_column"] in options else 0,
                format_func=lambda value: "未提供日期，只比较导入批次" if value is None else value,
                key=f"customer_date_{selection_key}_{text_column}_{group_column}")
        mapping = {"sheet_name": sheet, "encoding": None if extension == ".xlsx" else encoding,
                   "text_column": text_column, "group_column": group_column, "date_column": date_column}
        inspection = client.inspect_upload(manifest["upload_id"], mapping)
        input_key = (digest, uploaded.name, sheet, mapping["encoding"], text_column, group_column, date_column)
        if st.session_state.get("customer_input_key") != input_key:
            _clear_results()
            st.session_state["customer_input_key"] = input_key
            if inspection["cached_batch_id"]:
                _restore_batch(_history_store().load_batch(inspection["cached_batch_id"]), history_view=False)
                st.session_state["customer_input_key"] = input_key
                st.info("已恢复相同文件、相同选列的历史结果，无需重复分析。")
        counts = inspection["counts"]
        pending = counts.get("pending", 0)
        expected = inspection["expected_requests"]
        st.session_state["customer_expected_requests"] = expected
        st.info(f"已识别 {pending} 条可分析评价。确认下方选项后即可开始。")
        with st.expander("查看识别详情"):
            total, valid, requests = st.columns(3)
            total.metric("表格记录", inspection["total_rows"])
            valid.metric("有效待分析", pending)
            requests.metric("预计合并请求", expected)
            st.caption("空白、重复与无效记录保留原始行；相同文本成功分析后复用。")
        with st.expander("核对表格预览"):
            st.dataframe(pd.DataFrame(preview["rows"], columns=columns), hide_index=True, width="stretch")
        consent = st.checkbox("我确认已移除姓名、电话、订单号等个人信息，并同意发送所选评价用于分析。",
                              key="customer_consent")
        remaining = int(status.get("remaining_today", 0))
        if not status.get("configured"):
            st.warning("分析服务尚未配置，请联系管理员开通。")
        elif expected > remaining:
            st.warning(f"今日剩余额度为 {remaining} 次请求，本次预计需要 {expected} 次。")
        active = bool(st.session_state.get("customer_active_job_id"))
        if st.button("分析评价", key="customer_analyze", type="primary",
                     disabled=active or not (status.get("configured") and consent and pending and expected <= remaining)):
            _submit_background_job(uploaded, input_key, mapping, expected)
    except JobClientError as error:
        st.error(str(error))
        # An API failure never opens local files or invokes a local model.
        return


def _render_uploaded_file(uploaded, status: dict) -> None:
    if api_ui_enabled():
        _render_remote_upload(uploaded, status)
        return
    st.session_state.pop("customer_demo_report", None)
    st.session_state.pop("customer_demo_workspace", None)
    st.session_state["customer_file_name"] = uploaded.name
    st.button("清除本次上传与结果", key="customer_clear", on_click=clear_customer_data)
    data = uploaded.getvalue()
    file_digest = hashlib.sha256(data).hexdigest()
    extension = Path(uploaded.name).suffix.lower()
    if st.session_state.get("customer_file_digest") != file_digest:
        _clear_results()
        st.session_state["customer_file_digest"] = file_digest
    sheet_name, encoding = None, None
    row_options = {"max_rows": 1000} if queued_analysis_enabled() else {}
    try:
        if extension == ".xls":
            raise ExcelInputError("暂不支持旧版 .xls，请在 Excel 中另存为 .xlsx 后上传。")
        if extension == ".xlsx":
            sheets = inspect_workbook(data, uploaded.name, **row_options)
            sheet_name = st.selectbox("选择工作表", sheets, key=f"customer_sheet_{file_digest}")
            if len(sheets) > 1:
                st.caption("本次只分析当前选中的工作表，其他工作表不会自动分析。")
            frame = read_feedback_xlsx(data, uploaded.name, sheet_name, **row_options)
        elif extension == ".csv":
            encoding_label = st.selectbox("CSV 编码", ["UTF-8（推荐）", "GB18030 / GBK"], key="customer_csv_encoding")
            encoding = "utf-8-sig" if encoding_label.startswith("UTF-8") else "gb18030"
            frame = read_feedback_csv(data, filename=uploaded.name, encoding=encoding, **row_options)
        else:
            raise CsvInputError("请上传 .xlsx 或 .csv 文件。")
        st.subheader("确认评价与分组列")
        columns = list(frame.columns)
        try:
            saved_mapping = _history_store().get_mapping(columns)
        except Exception:
            saved_mapping = None
        suggested = suggest_columns(columns, saved_mapping)
        selection_key = f"{file_digest}_{sheet_name}_{encoding}"
        text_container, group_container = st.columns(2)
        with text_container:
            text_column = st.selectbox(
                "哪一列是顾客评价？", columns, index=columns.index(suggested["text_column"]),
                key=f"customer_text_{selection_key}",
            )
        with group_container:
            group_options = [None] + [column for column in columns if column != text_column]
            group_selection = st.selectbox(
                "按产品或门店汇总（可选）", group_options,
                index=group_options.index(suggested["group_column"]) if suggested["group_column"] in group_options else 0,
                format_func=lambda value: "不分组" if value is None else value,
                key=f"customer_group_{selection_key}_{text_column}",
            )
        group_column = group_selection
        with st.expander("评价日期（可选）"):
            date_options = [None] + [column for column in columns if column not in (text_column, group_column)]
            date_column = st.selectbox("哪一列是评价日期？", date_options,
                index=date_options.index(suggested["date_column"]) if suggested["date_column"] in date_options else 0,
                format_func=lambda value: "未提供日期，只比较导入批次" if value is None else value,
                key=f"customer_date_{selection_key}_{text_column}_{group_column}")
            st.caption("只识别明确的年月日；日期缺失时不会推断周度趋势。相同表头会记住这次选列。")
        rows = (prepare_excel_feedback_rows(frame, text_column, **row_options) if extension == ".xlsx"
                else prepare_feedback_rows(frame, text_column, **row_options))
    except (CsvInputError, ExcelInputError) as error:
        _clear_results()
        st.error(str(error))
        return
    except Exception as error:
        _clear_results()
        st.error(_safe_service_error(error))
        return
    mapping = {"sheet_name": sheet_name, "encoding": encoding, "text_column": text_column,
               "group_column": group_column, "date_column": date_column}
    input_key = (file_digest, uploaded.name, sheet_name, encoding, text_column, group_column, date_column)
    if st.session_state.get("customer_input_key") != input_key:
        _clear_results()
        st.session_state["customer_input_key"] = input_key
        try:
            previous = _history_store().find_batch(file_digest, mapping)
            if previous:
                _restore_batch(_history_store().load_batch(previous["id"]), history_view=False)
                st.session_state["customer_input_key"] = input_key
                st.info("已恢复相同文件、相同选列的历史结果，无需重复分析。需要更新结果时可再次明确点击分析。")
        except Exception:
            st.caption("历史恢复暂不可用，本次仍可正常分析并下载结果。")
    saved_snapshot = st.session_state.get("customer_run_snapshot")
    if "customer_report" not in st.session_state and saved_snapshot and saved_snapshot.get("input_key") == input_key:
        _store_snapshot_report(saved_snapshot, frame, text_column, group_column)
    counts = Counter(row.processing_status for row in rows)
    pending = counts[ProcessingStatus.PENDING]
    try:
        expected_requests = estimate_customer_requests(rows, **row_options)
    except Exception as error:
        st.error(_safe_service_error(error))
        return
    st.session_state["customer_expected_requests"] = expected_requests
    st.info(f"已识别 {pending} 条可分析评价。确认下方选项后即可开始。")
    with st.expander("查看识别详情"):
        record_count, valid_count, request_count = st.columns(3)
        record_count.metric("表格记录", len(rows))
        valid_count.metric("有效待分析", pending)
        request_count.metric("预计合并请求", expected_requests)
        st.caption(
            f"有效待分析 {pending} 条，预计合并 {expected_requests} 次请求。"
            f"空白 {counts[ProcessingStatus.SKIPPED_EMPTY]} 条、重复 {counts[ProcessingStatus.SKIPPED_DUPLICATE]} 条、"
            f"无效 {counts[ProcessingStatus.SKIPPED_INVALID]} 条均保留原始行。相同文本只分析一次，成功后复用到各原始行。"
        )
    with st.expander("核对表格预览", expanded=False):
        st.dataframe(frame.head(10), hide_index=True, width="stretch")
        st.caption("预览前 10 行。原始行号在导出明细中保留，方便回到 Excel 核对。")
    with st.expander("数据使用说明"):
        st.caption(
            f"分析时只发送「{text_column}」列中的有效评价文本；其他列仅用于本地汇总和导出。"
            "原表、分析结果和对话会保存在部署机器的历史记录中，可在左侧备份或删除。Agent 查询原表时可能发送所需的其他字段。"
        )
    consent = st.checkbox(
        "我确认已移除姓名、电话、订单号等个人信息，并同意发送所选评价用于分析。",
        key="customer_consent",
    )
    configured = bool(status.get("configured"))
    remaining = int(status.get("remaining_today", 0))
    if not configured:
        st.warning("分析服务尚未配置。普通业务用户请联系管理员开通，之后只需上传表格。")
        with st.expander("部署者如何开启分析？"):
            st.write("请在项目根目录的 .env 中填写你自己的模型接口、API Key 和模型名称，保存后重新启动服务。部署完成后，业务用户无需填写 Key。")
    elif expected_requests > remaining:
        st.warning(f"今日剩余额度为 {remaining} 次请求，本次预计需要 {expected_requests} 次，请缩小表格或明日再试。")
    elif not pending:
        st.warning("没有可分析的评价，请检查空白、重复或非文本内容。")
    active_job = bool(st.session_state.get("customer_active_job_id")) if queued_analysis_enabled() else False
    if active_job:
        st.caption("当前正在查看已提交的任务。请在上方查看进度，避免重复提交同一批评价。")
    if st.button(
        "分析评价", key="customer_analyze", type="primary",
        disabled=active_job or not (configured and consent and pending and expected_requests <= remaining),
    ):
        if queued_analysis_enabled():
            _submit_background_job(uploaded, input_key, mapping, expected_requests)
            return
        _clear_agent_state()
        st.session_state.pop("customer_report", None)
        st.session_state.pop("customer_results", None)
        # Store the mutable object before starting. Callbacks touch only this
        # reference, so a Streamlit yield cannot discard a received row.
        snapshot = {
            "input_key": input_key,
            "mapping": mapping,
            "rows": [row.model_dump(mode="json") for row in rows],
            "received": 0, "complete": False, "planned_requests": expected_requests,
        }
        positions = {row.feedback_id: index for index, row in enumerate(rows)}
        st.session_state["customer_run_snapshot"] = snapshot
        progress = st.progress(0.0, text="正在准备评价分析…")

        def update_progress(completed: int, total: int) -> None:
            progress.progress(completed / total if total else 1.0, text=f"已处理 {completed}/{total} 条评价")

        def save_result(result: FeedbackResult) -> None:
            validated = FeedbackResult.model_validate(result.model_dump())
            index = positions.get(validated.feedback_id)
            if index is None:
                raise ValueError("返回的评价标识无法关联本次表格。")
            original = rows[index]
            if validated.source_row != original.source_row or validated.original_text != original.original_text:
                raise ValueError("返回的评价记录与本次输入不一致。")
            snapshot["rows"][index] = validated.model_dump(mode="json")
            snapshot["received"] += 1

        report_saved = False
        try:
            results = analyze_customer_rows(rows, progress_callback=update_progress, result_callback=save_result, run_state=snapshot)
            snapshot["rows"] = [row.model_dump(mode="json") for row in results]
            snapshot["analysis_meta"] = getattr(results, "analysis_meta", snapshot.get("analysis_meta", {}))
            snapshot["complete"] = True
            snapshot["received"] = max(snapshot["received"], len(results))
            result_frame = results_to_dataframe(results)
            report = build_customer_report(frame, result_frame, text_column, group_column=group_column)
            report["analysis_meta"] = dict(snapshot["analysis_meta"])
            st.session_state["customer_results"] = result_frame
            st.session_state["customer_report"] = report
            _persist_report(report, uploaded, mapping, frame)
            report_saved = True
            st.success("分析已结束，请查看结果；失败和跳过的评价也已保留。")
        except Exception as error:
            st.error(_safe_service_error(error))
        finally:
            if not report_saved and snapshot["received"]:
                _store_snapshot_report(snapshot, frame, text_column, group_column)
                partial = st.session_state.get("customer_report")
                if partial:
                    _persist_report(partial, uploaded, mapping, frame)
            progress.empty()


def main() -> None:
    st.set_page_config(page_title="顾客评价分析 | CustomerInsight", page_icon="📋", layout="wide")
    st.markdown(STYLE, unsafe_allow_html=True)
    st.title("顾客评价分析")
    st.caption("上传表格，查看可视化结果，再向 Agent 继续提问。")
    if _access_allowed():
        try:
            status = _job_client().status() if api_ui_enabled() else get_service_status()
        except Exception as error:
            st.error(_safe_service_error(error))
            status = {"configured": False, "remaining_today": 0, "daily_limit": 0}
        render_background_jobs()
        render_upload(status)
        render_sidebar(status)
    else:
        st.button("先体验完整示例", key="customer_public_demo", on_click=_load_demo_workspace)
        if "customer_demo_workspace" in st.session_state:
            render_customer_report(st.session_state["customer_demo_report"], {"configured": False}, is_demo=True)
    st.markdown('<div id="privacy"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="ci-footer">基于 <a href="https://github.com/ysskrishna/ai-customer-feedback-analyzer">'
        'Y. Siva Sai Krishna 的开源项目</a> 扩展 · MIT License。自动分析供业务参考，重要事项请核对原文。</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
