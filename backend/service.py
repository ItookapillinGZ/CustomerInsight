"""Adapters around the existing analysis/history services, without a second schema.

Phase one deliberately runs one HTTP process. Its local locks are not a durable
task queue, and the existing browser UI remains a separate entry point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4

import pandas as pd

from core.analysis_agent import (
    AnalysisAgentResult, MAX_CHAT_TURNS, MAX_HISTORY_TURNS,
    estimate_agent_requests, report_fingerprint,
)
from core.config import Config
from core.csv_io import CsvInputError, prepare_feedback_rows, read_feedback_csv, results_to_dataframe
from core.customer_report import build_customer_report, export_customer_csv, export_customer_html
from core.excel_io import inspect_workbook, prepare_excel_feedback_rows, read_feedback_xlsx
from core.history_store import HistoryStore, _pack
from core.history_workflow import attach_historical_context, date_scope, suggest_columns
from core.product_service import (
    ProductRequestBudget, analyze_customer_rows, estimate_customer_requests,
    run_customer_report_agent,
)
from models.models import FeedbackResult

from backend.serialization import batch_metadata, public_json, report_projection


_IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
UPLOAD_TTL_SECONDS = 3600
MAX_STAGED_FILES = 100
MAX_STAGED_BYTES = 100 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ApiError(422, "invalid_identifier", "记录编号格式无效。")
    return value


def _root_directory():
    return Path(os.getenv("APP_DATA_DIR") or (
        Path(os.environ["APP_HISTORY_DB_PATH"]).parent
        if os.getenv("APP_HISTORY_DB_PATH") else Path(".runtime") / "history"
    )).resolve()


class WorkspaceService:
    def __init__(self, *, data_dir=None, budget=None, analysis_runner=None, agent_runner=None):
        self.root_dir = Path(data_dir).resolve() if data_dir is not None else _root_directory()
        self._history = None
        self._budget = budget
        self._explicit_data_dir = data_dir is not None
        self.analysis_runner = analysis_runner or analyze_customer_rows
        self.agent_runner = agent_runner or run_customer_report_agent
        self._stage_lock = threading.Lock()
        # One shared lock bounds concurrent API mutations and prevents chat lost
        # updates. It does not coordinate a second process or the legacy UI.
        self._mutation_lock = threading.Lock()

    @property
    def history(self):
        if self._history is None:
            self._history = HistoryStore(self.root_dir)
        return self._history

    @history.setter
    def history(self, value):
        self._history = value

    @property
    def budget(self):
        if self._budget is None:
            # Explicit factory storage is an isolated workspace for tests and
            # acceptance. Normal deployment still honors APP_USAGE_DB_PATH.
            self._budget = (ProductRequestBudget(self.root_dir / "api_usage.sqlite3")
                            if self._explicit_data_dir else ProductRequestBudget())
        return self._budget

    @budget.setter
    def budget(self, value):
        self._budget = value

    def import_demo_workspace(self):
        """Import only fixed synthetic fixtures into this scoped history."""
        from core.demo_workspace import save_demo_workspace

        if not self._mutation_lock.acquire(blocking=False):
            raise ApiError(409, "workspace_busy", "工作区正在执行另一个操作，请稍后重试。")
        try:
            batches = save_demo_workspace(self.history)
            return {**batches, "model_calls": 0, "data_origin": "synthetic"}
        finally:
            self._mutation_lock.release()

    def _staging_directory(self):
        self.root_dir.mkdir(parents=True, exist_ok=True)
        directory = self.root_dir / "api_uploads"
        if directory.is_symlink():
            raise ApiError(503, "upload_storage_unavailable", "上传暂存目录不可用。")
        directory.mkdir(exist_ok=True)
        return directory

    def _stage_path(self, upload_id, suffix):
        path = self._staging_directory() / (identifier(upload_id) + suffix)
        if path.is_symlink() or path.resolve().parent != self._staging_directory():
            raise ApiError(409, "invalid_upload", "上传记录校验失败，请重新上传。")
        return path

    def _cleanup_staged(self):
        directory, now = self._staging_directory(), time.time()
        count, size = 0, 0
        # Only UUID-named API staging files are touched; originals and history
        # are outside this directory and never participate in TTL cleanup.
        for manifest in directory.glob("*.json"):
            if not _IDENTIFIER.fullmatch(manifest.stem) or manifest.is_symlink():
                continue
            data_path = self._stage_path(manifest.stem, ".blob")
            if now - manifest.stat().st_mtime >= UPLOAD_TTL_SECONDS:
                manifest.unlink(missing_ok=True)
                data_path.unlink(missing_ok=True)
            else:
                count += 1
                size += data_path.stat().st_size if data_path.exists() else 0
        # Clean an interrupted write only after the same TTL; an in-flight
        # upload is protected by _stage_lock in this supported single process.
        for data_path in directory.glob("*.blob"):
            if not _IDENTIFIER.fullmatch(data_path.stem) or data_path.is_symlink():
                continue
            if not data_path.with_suffix(".json").exists() and now - data_path.stat().st_mtime >= UPLOAD_TTL_SECONDS:
                data_path.unlink(missing_ok=True)
        return count, size

    def upload(self, filename, data, encoding="utf-8-sig", purpose="analysis"):
        if purpose not in {"analysis", "job"}:
            raise ApiError(422, "invalid_purpose", "上传用途无效。")
        limit = 1000 if purpose == "job" else Config.MAX_BATCH_ROWS
        if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
            raise ApiError(422, "invalid_filename", "请选择有效的 Excel 或 CSV 文件。")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if len(filename) > 255 or Path(filename).suffix.lower() not in {".xlsx", ".csv"}:
            raise ApiError(422, "unsupported_file", "仅支持 .xlsx 和 .csv 文件。")
        if not data:
            raise ApiError(422, "empty_upload", "上传文件不能为空。")
        if len(data) > Config.MAX_CSV_BYTES:
            raise ApiError(413, "upload_too_large", "上传文件不能超过 5 MB。")
        if Path(filename).suffix.lower() == ".xlsx":
            sheet_names = inspect_workbook(data, filename, max_rows=limit)
        else:
            read_feedback_csv(data, filename=filename, encoding=encoding, max_rows=limit)
            sheet_names = []
        now = time.time()
        manifest = {
            "upload_id": uuid4().hex, "filename": filename, "size_bytes": len(data),
            "sheet_names": sheet_names, "created_at": now,
            "content_hash": hashlib.sha256(data).hexdigest(),
            "purpose": purpose,
        }
        with self._stage_lock:
            count, size = self._cleanup_staged()
            if count >= MAX_STAGED_FILES or size + len(data) > MAX_STAGED_BYTES:
                raise ApiError(429, "upload_capacity", "暂存文件已达到上限，请稍后重试。")
            blob = self._stage_path(manifest["upload_id"], ".blob")
            descriptor = self._stage_path(manifest["upload_id"], ".json")
            try:
                with blob.open("xb") as destination:
                    destination.write(data)
                with descriptor.open("x", encoding="utf-8") as destination:
                    json.dump(manifest, destination, ensure_ascii=False)
            except BaseException:
                blob.unlink(missing_ok=True)
                descriptor.unlink(missing_ok=True)
                raise
        return {
            key: manifest[key] for key in ("upload_id", "filename", "size_bytes", "sheet_names")
        } | {"expires_at": datetime.fromtimestamp(now + UPLOAD_TTL_SECONDS, timezone.utc).isoformat()}

    def _upload(self, upload_id):
        with self._stage_lock:
            manifest_path = self._stage_path(upload_id, ".json")
            blob_path = self._stage_path(upload_id, ".blob")
            if not manifest_path.exists() or not blob_path.exists():
                raise ApiError(404, "upload_not_found", "上传记录不存在或已过期，请重新上传。")
            if time.time() - manifest_path.stat().st_mtime >= UPLOAD_TTL_SECONDS:
                raise ApiError(410, "upload_expired", "上传记录已过期，请重新上传。")
            if manifest_path.stat().st_size > 16384 or blob_path.stat().st_size > Config.MAX_CSV_BYTES:
                raise ApiError(409, "invalid_upload", "上传记录校验失败，请重新上传。")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                data = blob_path.read_bytes()
                valid = (manifest["upload_id"] == upload_id and manifest["size_bytes"] == len(data)
                         and manifest["content_hash"] == hashlib.sha256(data).hexdigest()
                         and Path(manifest["filename"]).suffix.lower() in {".xlsx", ".csv"})
            except (KeyError, ValueError, TypeError):
                valid = False
            if not valid:
                raise ApiError(409, "invalid_upload", "上传记录校验失败，请重新上传。")
            return manifest, data

    def _frame(self, manifest, data, sheet_name=None, encoding="utf-8-sig"):
        limit = 1000 if manifest.get("purpose") == "job" else Config.MAX_BATCH_ROWS
        if Path(manifest["filename"]).suffix.lower() == ".xlsx":
            return read_feedback_xlsx(data, manifest["filename"], sheet_name, max_rows=limit)
        if sheet_name is not None:
            raise ApiError(422, "invalid_sheet", "CSV 文件不能选择 Excel 工作表。")
        return read_feedback_csv(data, filename=manifest["filename"], encoding=encoding, max_rows=limit)

    def preview(self, upload_id, *, sheet_name=None, encoding="utf-8-sig"):
        manifest, data = self._upload(upload_id)
        frame = self._frame(manifest, data, sheet_name, encoding)
        columns = frame.columns.tolist()
        return {
            "upload_id": upload_id, "filename": manifest["filename"],
            "sheet_name": frame.attrs.get("sheet_name"), "columns": columns,
            "total_rows": len(frame), "rows": public_json(frame.head(10)),
            "suggested_mapping": suggest_columns(columns, self.history.get_mapping(columns)),
        }

    def status(self):
        from core.product_service import _grouping_settings
        size, characters = _grouping_settings()
        return {"configured": bool(Config.OPENAI_API_KEY), "remaining_today": self.budget.remaining(),
                "daily_limit": self.budget.daily_limit, "request_batch_size": size,
                "max_batch_characters": characters, "agent_requests_per_run": estimate_agent_requests()}

    def inspect_upload(self, upload_id, request):
        manifest, data = self._upload(upload_id)
        frame = self._frame(manifest, data, request.sheet_name, request.encoding)
        selected = [column for column in (request.text_column, request.group_column, request.date_column)
                    if column is not None]
        if len(set(selected)) != len(selected) or any(column not in frame.columns for column in selected):
            raise ApiError(422, "invalid_columns", "请为评价、分组和日期选择不同且存在的字段。")
        excel = Path(manifest["filename"]).suffix.lower() == ".xlsx"
        mapping = {"sheet_name": frame.attrs.get("sheet_name"), "encoding": None if excel else request.encoding,
                   "text_column": request.text_column, "group_column": request.group_column,
                   "date_column": request.date_column}
        limit = 1000 if manifest.get("purpose") == "job" else Config.MAX_BATCH_ROWS
        rows = (prepare_excel_feedback_rows if excel else prepare_feedback_rows)(
            frame, request.text_column, max_rows=limit)
        existing = self.history.find_batch(manifest["content_hash"], mapping)
        return {"mapping": mapping, "total_rows": len(rows),
                "counts": {key.value: count for key, count in Counter(row.processing_status for row in rows).items()},
                "expected_requests": estimate_customer_requests(rows, max_rows=limit),
                "cached_batch_id": existing["id"] if existing else None}

    def report_view(self, batch_id, comparison_id=None):
        loaded = self.history.load_batch(identifier(batch_id))
        report = self._chat_context(batch_id, comparison_id)
        # The existing, bounded typed JSON codec restores DataFrames and their
        # indices/attrs exactly. It never imports classes or executes pickle.
        return {"metadata": batch_metadata(loaded), "packed_report": _pack(report)}

    def clear_chat(self, batch_id, comparison_id=None):
        self._chat_context(batch_id, comparison_id)
        if hasattr(self.history, "clear_chat"):
            self.history.clear_chat(batch_id, comparison_id=comparison_id)
            return
        with self._mutation_lock:
            saved = self.history.load_chat(batch_id)
            self.history.save_chat(batch_id, [turn for turn in saved
                                             if turn.get("comparison_batch_id") != comparison_id])

    @staticmethod
    def _consent(allowed, max_requests, needed):
        if not allowed:
            raise ApiError(428, "model_consent_required", "请明确允许发送所选评价和问题到模型接口。")
        if max_requests is None:
            raise ApiError(428, "request_budget_required", "请明确设置本次模型请求上限。")
        if max_requests < needed:
            raise ApiError(422, "request_budget_too_small", "本次请求上限小于预计需要的模型请求数。")

    def _analysis_response(self, batch_id, *, cached, error_code=None):
        loaded = self.history.load_batch(batch_id)
        metrics = loaded["report"].get("metrics", {})
        complete = (metrics.get("success_rows", 0) == metrics.get("total_rows", 0)
                    and not metrics.get("failed_rows", 0) and not metrics.get("pending_rows", 0))
        result = {"batch_id": batch_id, "cached": cached,
                  "status": "completed" if complete else "partial", "metadata": batch_metadata(loaded)}
        if error_code:
            result["error_code"] = error_code
        return result

    def analyze(self, request):
        manifest, data = self._upload(request.upload_id)
        frame = self._frame(manifest, data, request.sheet_name, request.encoding)
        for column in (request.text_column, request.group_column, request.date_column):
            if column is not None and column not in frame.columns:
                raise ApiError(422, "invalid_column", "所选字段不存在，请重新选择。")
        selected = [column for column in (request.text_column, request.group_column, request.date_column)
                    if column is not None]
        if len(set(selected)) != len(selected):
            raise ApiError(422, "overlapping_columns", "评价、分组和日期需要选择不同字段。")
        is_excel = Path(manifest["filename"]).suffix.lower() == ".xlsx"
        # The existing UI stores encoding=None for Excel. Keep that exact
        # mapping so its already paid-for results are found without reanalysis.
        mapping = {"sheet_name": frame.attrs.get("sheet_name"), "encoding": None if is_excel else request.encoding,
                   "text_column": request.text_column, "group_column": request.group_column,
                   "date_column": request.date_column}
        if not self._mutation_lock.acquire(blocking=False):
            raise ApiError(409, "workspace_busy", "工作区正在执行另一个操作，请稍后重试。")
        try:
            existing = self.history.find_batch(manifest["content_hash"], mapping)
            if existing and not request.force:
                return self._analysis_response(existing["id"], cached=True)
            prepare = prepare_excel_feedback_rows if is_excel else prepare_feedback_rows
            rows = prepare(frame, request.text_column)
            needed = estimate_customer_requests(rows)
            self._consent(request.allow_model_call, request.max_requests, needed)
            if self.budget.remaining() < needed:
                raise ApiError(429, "daily_budget_exceeded", "今日剩余额度不足，请稍后再试。")
            by_id = {row.feedback_id: row for row in rows}
            received = {}
            run_state = {}

            def on_result(raw):
                result = FeedbackResult.model_validate(raw.model_dump() if isinstance(raw, FeedbackResult) else raw)
                original = by_id.get(result.feedback_id)
                if original is None or result.source_row != original.source_row or result.original_text != original.original_text:
                    raise ValueError("分析结果不能关联到本次原始记录。")
                received[result.feedback_id] = result

            interrupted = False
            try:
                results = self.analysis_runner(
                    rows, budget=self.budget, max_attempts=request.max_requests,
                    result_callback=on_result, run_state=run_state,
                )
                for result in results:
                    on_result(result)
                metadata = dict(getattr(results, "analysis_meta", run_state.get("analysis_meta", {})))
            except Exception:
                # Preserve only validated callbacks; completed groups survive a
                # timeout, provider error, or interruption of a later group.
                interrupted = True
                metadata = dict(run_state.get("analysis_meta", {}))
                metadata["run_status"] = "interrupted"
            all_results = [received.get(row.feedback_id, row) for row in rows]
            report = build_customer_report(
                frame, results_to_dataframe(all_results), request.text_column,
                request.group_column, analysis_meta=metadata,
            )
            report["source_frame"] = frame.copy(deep=True)
            report["period"] = date_scope(frame, request.date_column)
            batch_id = self.history.save_batch(
                report, filename=manifest["filename"], file_bytes=data, mapping=mapping,
            )
            self.history.save_mapping(frame.columns.tolist(), mapping)
            return self._analysis_response(batch_id, cached=False,
                                           error_code="analysis_interrupted" if interrupted else None)
        finally:
            self._mutation_lock.release()

    def list_batches(self, *, limit, offset):
        store = self.history
        if hasattr(store, "page_batches"):
            page = store.page_batches(limit=limit, offset=offset)
            return {**page, "items": [batch_metadata(item) for item in page["items"]]}
        # Read the existing schema without migration. A transaction keeps the
        # count and the selected identifiers on the same SQLite snapshot.
        with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True, timeout=15)) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            ids = connection.execute(
                "SELECT id FROM batches ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset),
            ).fetchall()
        items = []
        for (batch_id,) in ids:
            items.append(batch_metadata(store.load_batch(batch_id)))
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def batch(self, batch_id):
        loaded = self.history.load_batch(identifier(batch_id))
        return {"metadata": batch_metadata(loaded), "report": report_projection(loaded["report"])}

    def rows(self, batch_id, *, limit, offset):
        detail = self.history.load_batch(identifier(batch_id))["report"]["detail"]
        return {"columns": detail.columns.tolist(), "items": public_json(detail.iloc[offset:offset + limit]),
                "total": len(detail), "limit": limit, "offset": offset}

    def export(self, batch_id, kind, comparison_id=None):
        loaded = self.history.load_batch(identifier(batch_id))
        report = loaded["report"]
        if comparison_id is not None:
            if kind != "html":
                raise ApiError(422, "invalid_comparison_export", "仅完整 HTML 报告支持包含批次比较。")
            report = self._chat_context(batch_id, comparison_id)
        if kind == "csv":
            return export_customer_csv(report), "text/csv; charset=utf-8", "customerinsight-report.csv"
        if kind == "html":
            return export_customer_html(report), "text/html; charset=utf-8", "customerinsight-report.html"
        if kind == "original":
            suffix = Path(loaded["filename"]).suffix.lower()
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if suffix == ".xlsx" else "text/csv"
            return loaded["original_bytes"], media, "customerinsight-original" + suffix
        raise ApiError(404, "export_not_found", "该导出格式不存在。")

    def _chat_context(self, batch_id, comparison_id):
        loaded = self.history.load_batch(identifier(batch_id))
        if comparison_id == batch_id:
            raise ApiError(422, "invalid_comparison", "请另选一份历史报告进行对比。")
        previous = self.history.load_batch(identifier(comparison_id)) if comparison_id else None
        return attach_historical_context(loaded["report"], previous)

    def chat_history(self, batch_id, comparison_id=None):
        self._chat_context(batch_id, comparison_id)
        items = [turn for turn in self.history.load_chat(batch_id)
                 if turn.get("comparison_batch_id") == comparison_id]
        return {"items": public_json(items[-MAX_CHAT_TURNS:]), "total": len(items),
                "comparison_id": comparison_id}

    def chat(self, batch_id, request):
        identifier(batch_id)
        needed = estimate_agent_requests()
        self._consent(request.allow_model_call, request.max_requests, needed)
        if not self._mutation_lock.acquire(blocking=False):
            raise ApiError(409, "workspace_busy", "工作区正在执行另一个操作，请稍后重试。")
        try:
            report = self._chat_context(batch_id, request.comparison_id)
            fingerprint = report_fingerprint(report)
            saved = self.history.load_chat(batch_id)
            scoped = [turn for turn in saved if turn.get("comparison_batch_id") == request.comparison_id]
            prior = []
            for turn in scoped[-MAX_HISTORY_TURNS:]:
                if isinstance(turn.get("result"), dict):
                    answer = AnalysisAgentResult.model_validate(turn["result"])
                    if answer.report_fingerprint == fingerprint:
                        prior.append(answer)
            if self.budget.remaining() < needed:
                raise ApiError(429, "daily_budget_exceeded", "今日剩余额度不足，请稍后再试。")
            raw = self.agent_runner(report, request.question, budget=self.budget, history=prior)
            answer = AnalysisAgentResult.model_validate(raw.model_dump(mode="json") if isinstance(raw, AnalysisAgentResult) else raw)
            if answer.question != request.question or answer.report_fingerprint != fingerprint:
                raise ApiError(502, "invalid_agent_result", "回答与本次问题或报告不匹配，请重新提问。")
            if answer.model_calls > request.max_requests:
                raise ApiError(502, "invalid_agent_result", "回答的模型请求数未通过校验。")
            turn = {"question": request.question, "result": answer.model_dump(mode="json"),
                    "comparison_batch_id": request.comparison_id}
            if hasattr(self.history, "append_chat"):
                self.history.append_chat(batch_id, turn, max_turns=MAX_CHAT_TURNS)
            else:
                self.history.save_chat(batch_id, (saved + [turn])[-MAX_CHAT_TURNS:])
            return {"result": public_json(turn["result"]), "turn_count": min(len(saved) + 1, MAX_CHAT_TURNS)}
        finally:
            self._mutation_lock.release()
