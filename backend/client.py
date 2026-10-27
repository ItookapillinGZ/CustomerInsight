"""Small server-side HTTP client for the Streamlit durable-job workflow.

Credentials stay in the Streamlit process. Mutations are sent once: a lost
response must never turn into an implicit second, potentially paid analysis.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx


class JobClientError(RuntimeError):
    """A safe message which never contains a URL, token, or provider response."""
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def api_ui_enabled() -> bool:
    mode = os.getenv("APP_UI_MODE", "").strip().lower()
    if mode:
        return mode == "api"
    # Stage-three deployments selected accounts through this environment
    # marker. Recognize it without opening a database so upgrading an older
    # Compose deployment cannot expose its legacy workspace without login.
    return bool(os.getenv("APP_DATABASE_URL", "").strip())


def queued_analysis_enabled() -> bool:
    return api_ui_enabled() or os.getenv("APP_ANALYSIS_MODE", "direct").strip().lower() == "queued"


class JobApiClient:
    def __init__(self, base_url: str, token: str, *, transport=None):
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            _ = parsed.port
        except (TypeError, ValueError):
            raise JobClientError("后台任务服务地址配置无效，请联系管理员。") from None
        if not isinstance(token, str) or not token.strip():
            raise JobClientError("后台任务服务尚未配置访问凭据，请联系管理员。")
        self._base_url = base_url.rstrip("/") + "/api/v1"
        self._token = token.strip()
        self._transport = transport

    @classmethod
    def from_environment(cls):
        return cls(os.getenv("APP_API_URL", "http://127.0.0.1:8000"), os.getenv("APP_API_TOKEN", ""))

    def _request(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self._token}"}
        headers.update(kwargs.pop("headers", {}))
        try:
            timeout = kwargs.pop("timeout", 15.0)
            binary = kwargs.pop("binary", False)
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0),
                              transport=self._transport, follow_redirects=False,
                              trust_env=False) as client:
                response = client.request(method, self._base_url + path, headers=headers, **kwargs)
        except (httpx.HTTPError, ValueError):
            if method == "GET":
                message = "暂时无法连接后台任务服务，请稍后刷新；已提交的任务仍保留在后台。"
            else:
                message = "未能确认后台是否收到操作。请先刷新任务列表核对，系统不会自动重复提交。"
            raise JobClientError(message) from None
        if not response.is_success:
            messages = {
                401: "登录信息无效或已过期，请重新登录。",
                403: "当前操作未获允许，请检查分析确认选项。",
                404: "该任务或上传文件已不存在，请刷新任务列表。",
                409: "任务当前状态不支持此操作，请刷新进度后核对。",
                413: "表格超过后台允许的大小，请缩小后上传。",
                422: "表格选列或任务参数无效，请重新核对。",
                429: "当前分析额度或并发已满，请稍后查看任务状态。",
                503: "后台任务服务暂不可用，请稍后刷新；已有任务不会被重新提交。",
            }
            if method == "POST" and path == "/auth/login":
                messages.update({
                    401: "用户名或密码不正确，请使用已创建的账户登录。",
                    422: "用户名或密码字段无效，请核对输入格式。",
                    429: "登录尝试过于频繁，请稍后再试。",
                })
            raise JobClientError(messages.get(response.status_code, "后台操作暂未完成，请刷新任务状态后再处理。"),
                                 status_code=response.status_code)
        if binary:
            return response.content
        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, TypeError):
            raise JobClientError("后台返回了无法识别的数据，请联系管理员；系统不会自动重复分析。") from None

    def upload(self, filename: str, data: bytes, *, encoding: str = "utf-8-sig") -> dict:
        if encoding not in {"utf-8-sig", "gb18030"}:
            raise JobClientError("不支持所选 CSV 编码，请重新选择。")
        return self._request("POST", "/uploads", files={"file": (filename, data)},
                             data={"purpose": "job", "encoding": encoding})

    def login(self, username: str, password: str) -> dict:
        return self._request("POST", "/auth/login", json={"username": username, "password": password})

    def me(self) -> dict:
        return self._request("GET", "/auth/me")

    def status(self) -> dict:
        return self._request("GET", "/status")

    def import_demo_workspace(self) -> dict:
        return self._request("POST", "/demo-workspace")

    def preview(self, upload_id, *, sheet_name=None, encoding="utf-8-sig"):
        params = {"encoding": encoding}
        if sheet_name is not None:
            params["sheet_name"] = sheet_name
        return self._request("GET", f"/uploads/{self._job_id(upload_id)}/preview", params=params)

    def inspect_upload(self, upload_id, mapping):
        return self._request("POST", f"/uploads/{self._job_id(upload_id)}/inspect", json={
            **mapping, "encoding": mapping.get("encoding") or "utf-8-sig"})

    def list_batches(self, *, limit=100, offset=0):
        return self._request("GET", "/batches", params={"limit": limit, "offset": offset})

    def view_batch(self, batch_id, comparison_id=None):
        params = {"comparison_id": self._job_id(comparison_id)} if comparison_id else {}
        return self._request("GET", f"/batches/{self._job_id(batch_id)}/view", params=params)

    def export(self, batch_id, kind, *, comparison_id=None):
        if kind not in {"csv", "html", "original"}:
            raise JobClientError("导出格式无效。")
        options = {}
        if comparison_id is not None:
            if kind != "html":
                raise JobClientError("仅完整 HTML 报告支持包含批次比较。")
            options["params"] = {"comparison_id": self._job_id(comparison_id)}
        return self._request("GET", f"/batches/{self._job_id(batch_id)}/exports/{kind}", binary=True, **options)

    def deletion(self, batch_id):
        return self._request("GET", f"/batches/{self._job_id(batch_id)}/deletion")

    def delete_batch(self, batch_id):
        return self._request("DELETE", f"/batches/{self._job_id(batch_id)}")

    def backup(self):
        return self._request("GET", "/backup", binary=True, timeout=60.0)

    def chat_history(self, batch_id, comparison_id=None):
        params = {"comparison_id": self._job_id(comparison_id)} if comparison_id else {}
        return self._request("GET", f"/batches/{self._job_id(batch_id)}/chat", params=params)

    def chat(self, batch_id, question, *, comparison_id=None, max_requests):
        return self._request("POST", f"/batches/{self._job_id(batch_id)}/chat", json={
            "question": question, "comparison_id": comparison_id,
            "allow_model_call": True, "max_requests": max_requests}, timeout=600.0)

    def clear_chat(self, batch_id, comparison_id=None):
        params = {"comparison_id": self._job_id(comparison_id)} if comparison_id else {}
        return self._request("DELETE", f"/batches/{self._job_id(batch_id)}/chat", params=params)

    def logout(self) -> dict:
        return self._request("POST", "/auth/logout")

    def change_password(self, current_password: str, new_password: str) -> dict:
        return self._request("POST", "/auth/password", json={"current_password": current_password, "new_password": new_password})

    def create_job(self, payload: dict, *, idempotency_key: str) -> dict:
        return self._request("POST", "/jobs", json={**payload, "idempotency_key": idempotency_key})

    def list_jobs(self) -> dict:
        return self._request("GET", "/jobs", params={"limit": 20})

    def get_job(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{self._job_id(job_id)}")

    def cancel_job(self, job_id: str) -> dict:
        return self._request("POST", f"/jobs/{self._job_id(job_id)}/cancel", json={})

    def resume_job(self, job_id: str, *, max_requests: int) -> dict:
        return self._request("POST", f"/jobs/{self._job_id(job_id)}/resume",
                             json={"allow_model_call": True, "max_requests": max_requests})

    @staticmethod
    def _job_id(value):
        if not isinstance(value, str) or len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
            raise JobClientError("任务标识无效，请从任务列表重新选择。")
        return value
