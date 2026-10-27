"""Run with ``uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1``."""

from __future__ import annotations

import os

# core.config intentionally gives the project .env priority for model settings.
# An explicitly supplied API token must nevertheless win over a blank/copied
# .env entry. Capture only this setting before importing the existing core.
_PROCESS_API_TOKEN = os.getenv("APP_API_TOKEN", "").strip()

import hmac
import logging
import re
import time
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, UploadFile, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool

from core.config import Config, ConfigurationError
from core.csv_io import CsvInputError
from core.history_store import HistoryCorruptionError, HistoryNotFoundError, HistoryStoreError
from core.product_service import ProductLimitError

from backend.schemas import (
    AnalysisRequest, AnalysisResponse, BatchListResponse, BatchResponse,
    ChatHistoryResponse, ChatRequest, ChatResponse, ErrorResponse,
    HealthResponse, PreviewResponse, RowsResponse, UploadResponse,
    JobRequest, JobResponse, JobListResponse, ResumeJobRequest,
    LoginRequest, LoginResponse, AccountResponse, PasswordRequest,
    InspectionRequest, InspectionResponse, ReportViewResponse, ServiceStatusResponse, DeletionResponse,
    DemoWorkspaceRequest, DemoWorkspaceResponse,
)
from backend.accounts import AccountService, AccountError
from backend.database import database_enabled, get_engine
from backend.service import ApiError, WorkspaceService
from backend.job_service import JobService
from backend.job_store import JobNotFoundError, JobConflictError, JobBudgetError, JobStoreError


LOGGER = logging.getLogger("customerinsight.api")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
_BEARER = HTTPBearer(auto_error=False)
_API_ERRORS = {status: {"model": ErrorResponse} for status in (401, 404, 409, 410, 413, 422, 428, 429, 500, 502, 503)}


def _error_response(status, code, message, request_id):
    response = JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message}, "request_id": request_id,
    })
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


class ApiBoundaryMiddleware:
    """Reject unauthenticated/oversized requests before multipart spooling.

    Only bounded requests are buffered. Logs contain route templates, status,
    elapsed time and a generated/validated request ID; never headers, queries,
    filenames, questions, uploaded cells or exception messages.
    """

    def __init__(self, app, token, accounts=None):
        self.app, self.token, self.accounts = app, token, accounts

    def _authorized(self, scope):
        if not self.token:
            return 503, "api_not_configured", "请由部署者配置 APP_API_TOKEN 后再使用接口。"
        authorization = next((value.decode("latin-1") for name, value in scope["headers"]
                              if name.lower() == b"authorization"), "")
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential.encode("utf-8"), self.token.encode("utf-8")):
            return 401, "unauthorized", "请提供有效的 Bearer 访问凭据。"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = next((value.decode("latin-1") for name, value in scope["headers"]
                         if name.lower() == b"x-request-id"), "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started, response_status, response_started = time.perf_counter(), 500, False

        async def guarded_send(message):
            nonlocal response_status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() not in {b"x-request-id", b"cache-control"}]
                headers.extend([(b"x-request-id", request_id.encode("ascii")), (b"cache-control", b"no-store")])
                message["headers"] = headers
            await send(message)

        try:
            if scope["path"].startswith("/api/") and scope["path"] != "/api/v1/auth/login":
                if self.accounts is not None:
                    authorization = next((value.decode("latin-1") for name, value in scope["headers"]
                                          if name.lower() == b"authorization"), "")
                    scheme, _, credential = authorization.partition(" ")
                    try:
                        if scheme.lower() != "bearer":
                            raise AccountError(401, "unauthorized", "请先登录。")
                        principal = await run_in_threadpool(self.accounts.authenticate, credential)
                        scope["state"]["principal"] = principal
                        problem = None
                    except AccountError as error:
                        problem = (error.status_code, error.code, error.message)
                else:
                    problem = self._authorized(scope)
                if problem:
                    return await _error_response(*problem, request_id)(scope, receive, guarded_send)
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                limit = Config.MAX_CSV_BYTES + 64 * 1024 if scope["path"] == "/api/v1/uploads" else 64 * 1024
                raw_length = next((value for name, value in scope["headers"] if name.lower() == b"content-length"), None)
                if raw_length is not None:
                    try:
                        length = int(raw_length)
                        if length < 0:
                            raise ValueError
                    except ValueError:
                        return await _error_response(400, "invalid_content_length", "请求长度格式无效。", request_id)(scope, receive, guarded_send)
                    if length > limit:
                        return await _error_response(413, "request_too_large", "请求内容超过大小限制。", request_id)(scope, receive, guarded_send)
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > limit:
                        return await _error_response(413, "request_too_large", "请求内容超过大小限制。", request_id)(scope, receive, guarded_send)
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def bounded_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, bounded_receive, guarded_send)
            else:
                await self.app(scope, receive, guarded_send)
        except Exception as error:
            # ServerErrorMiddleware otherwise re-raises after returning its 500,
            # and Uvicorn may log a provider exception's sensitive message.
            # Keep unexpected errors inside this redacted HTTP boundary.
            from backend.observability import emit_event
            emit_event("http_error", request_id=request_id, error_type=type(error).__name__)
            if not response_started:
                await _error_response(500, "internal_error", "服务暂时无法完成请求，请保留请求编号以便排查。", request_id)(scope, receive, guarded_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            from backend.observability import emit_event
            emit_event("http_request", request_id=request_id, method=scope["method"], route=route,
                       status_code=response_status, elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                       workspace_id=scope.get("state", {}).get("principal", {}).get("workspace_id"))


def create_app(*, data_dir=None, api_token=None, budget=None, analysis_runner=None, agent_runner=None,
               job_store=None, job_dispatcher=None, account_service=None, database_url=None):
    """Application factory with isolated storage and injectable model boundaries."""
    token = (api_token if api_token is not None else
             (_PROCESS_API_TOKEN or os.getenv("APP_API_TOKEN", "").strip()
              or os.getenv("APP_ACCESS_PASSWORD", "").strip()))
    token = token.strip() if isinstance(token, str) else ""
    accounts = account_service
    if accounts is None and (database_url or database_enabled()):
        accounts = AccountService(get_engine(database_url))
    app = FastAPI(title="CustomerInsight API", version="1.2.0", debug=False,
                  description="Review API with durable jobs and optional PostgreSQL account-isolated workspaces.")
    app.state.service = WorkspaceService(data_dir=data_dir, budget=budget,
                                       analysis_runner=analysis_runner, agent_runner=agent_runner)
    app.state.jobs = JobService(app.state.service, store=job_store, dispatcher=job_dispatcher)
    app.state.accounts = accounts
    if accounts is not None:
        from backend.tenancy import TenantServices
        app.state.tenants = TenantServices(accounts.engine, data_dir=data_dir, budget=budget,
                                           analysis_runner=analysis_runner, agent_runner=agent_runner,
                                           dispatcher=job_dispatcher)
    app.add_middleware(ApiBoundaryMiddleware, token=token, accounts=accounts)

    async def require_token(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]):
        # The outer boundary checks before parsing a body; this dependency also
        # publishes the Bearer requirement in the generated OpenAPI contract.
        if accounts is not None:
            if not getattr(request.state, "principal", None):
                raise ApiError(401, "unauthorized", "请先登录。")
            return
        if not token:
            raise ApiError(503, "api_not_configured", "请由部署者配置 APP_API_TOKEN 后再使用接口。")
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode("utf-8"), token.encode("utf-8")):
            raise ApiError(401, "unauthorized", "请提供有效的 Bearer 访问凭据。")

    @app.exception_handler(ApiError)
    @app.exception_handler(AccountError)
    async def api_error(request, error):
        return _error_response(error.status_code, error.code, error.message, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic's default errors include input values. Do not echo uploaded
        # fragments, questions or accidentally supplied credentials.
        return _error_response(422, "invalid_request", "请求字段无效，请核对接口说明。", request.state.request_id)

    @app.exception_handler(HistoryNotFoundError)
    async def history_not_found(request, error):
        return _error_response(404, "batch_not_found", "历史报告不存在或已删除。", request.state.request_id)

    @app.exception_handler(JobNotFoundError)
    async def job_missing(request, error):
        return _error_response(404, "job_not_found", "任务不存在或已删除。", request.state.request_id)

    @app.exception_handler(JobConflictError)
    async def job_conflict(request, error):
        return _error_response(409, "job_conflict", str(error), request.state.request_id)

    @app.exception_handler(JobBudgetError)
    async def job_budget(request, error):
        return _error_response(429, "job_budget_exceeded", "任务请求预算不足。", request.state.request_id)

    @app.exception_handler(JobStoreError)
    async def job_storage(request, error):
        return _error_response(422, "invalid_job", str(error), request.state.request_id)

    @app.exception_handler(HistoryCorruptionError)
    async def history_corrupt(request, error):
        return _error_response(409, "history_integrity_error", "历史记录未通过完整性检查。", request.state.request_id)

    @app.exception_handler(CsvInputError)
    async def input_error(request, error):
        return _error_response(422, "invalid_table", str(error), request.state.request_id)

    @app.exception_handler(ProductLimitError)
    async def product_limit(request, error):
        return _error_response(429, "service_limit", "模型服务额度不足或正在忙，请稍后重试。", request.state.request_id)

    @app.exception_handler(HistoryStoreError)
    @app.exception_handler(ConfigurationError)
    async def storage_error(request, error):
        return _error_response(503, "workspace_unavailable", "工作区暂不可用，请联系部署者检查配置。", request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        message = "接口不存在。" if error.status_code == 404 else "请求无法处理，请核对接口说明。"
        return _error_response(error.status_code, "http_error", message, request.state.request_id)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        LOGGER.error("request_id=%s error_type=%s", request.state.request_id, type(error).__name__)
        return _error_response(500, "internal_error", "服务暂时无法完成请求，请保留请求编号以便排查。", request.state.request_id)

    @app.get("/healthz", tags=["health"], response_model=HealthResponse)
    def health():
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def ready():
        from backend.operations import ReadinessProbe
        selected_engine = accounts.engine if accounts is not None and (account_service is not None or database_url) else None
        result = ReadinessProbe(engine=selected_engine, data_dir=app.state.service.root_dir).check()
        return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)

    def account_mode():
        if accounts is None:
            raise ApiError(404, "accounts_disabled", "当前部署未启用账户模式。")
        return accounts

    @app.post("/api/v1/auth/login", tags=["accounts"], response_model=LoginResponse, responses=_API_ERRORS)
    def login(body: LoginRequest, request: Request):
        return account_mode().login(body.username, body.password,
                                    client_ip=request.client.host if request.client else "unknown")

    @app.get("/api/v1/auth/me", tags=["accounts"], response_model=AccountResponse,
             dependencies=[Depends(require_token)], responses=_API_ERRORS)
    def me(request: Request):
        account_mode()
        return request.state.principal

    @app.post("/api/v1/auth/logout", tags=["accounts"], status_code=204,
              dependencies=[Depends(require_token)], responses=_API_ERRORS)
    def logout(request: Request):
        account_mode().logout(request.headers.get("authorization", "").partition(" ")[2])
        return Response(status_code=204)

    @app.post("/api/v1/auth/password", tags=["accounts"], status_code=204,
              dependencies=[Depends(require_token)], responses=_API_ERRORS)
    def password(body: PasswordRequest, request: Request):
        account_mode().change_password(request.headers.get("authorization", "").partition(" ")[2], body.new_password,
                                       current_password=body.current_password)
        return Response(status_code=204)

    def resolve_service(request: Request):
        if accounts is None:
            return app.state.service
        return app.state.tenants.for_workspace(request.state.principal["workspace_id"])[0]

    def resolve_jobs(request: Request):
        if accounts is None:
            return app.state.jobs
        return app.state.tenants.for_workspace(request.state.principal["workspace_id"])[1]

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)], responses={
        status: {"model": ErrorResponse} for status in (401, 404, 409, 410, 413, 422, 428, 429, 500, 502, 503)
    })

    @router.post("/uploads", status_code=201, tags=["uploads"], response_model=UploadResponse)
    async def upload(file: Annotated[UploadFile, File()],
                     encoding: Annotated[Literal["utf-8-sig", "gb18030"], Form()] = "utf-8-sig",
                     purpose: Annotated[Literal["analysis", "job"], Form()] = "analysis",
                     service=Depends(resolve_service)):
        try:
            data = await file.read(Config.MAX_CSV_BYTES + 1)
            return await run_in_threadpool(service.upload, file.filename, data, encoding, purpose)
        finally:
            await file.close()

    @router.get("/uploads/{upload_id}/preview", tags=["uploads"], response_model=PreviewResponse)
    def preview(upload_id: str, sheet_name: str | None = Query(default=None, max_length=255),
                encoding: Literal["utf-8-sig", "gb18030"] = "utf-8-sig", service=Depends(resolve_service)):
        return service.preview(upload_id, sheet_name=sheet_name, encoding=encoding)

    @router.get("/status", tags=["workspace"], response_model=ServiceStatusResponse)
    def status(service=Depends(resolve_service)):
        return service.status()

    @router.post("/demo-workspace", tags=["workspace"], response_model=DemoWorkspaceResponse)
    def import_demo_workspace(body: DemoWorkspaceRequest | None = None, service=Depends(resolve_service)):
        return service.import_demo_workspace()

    @router.get("/operations/metrics", tags=["operations"])
    def operations(service=Depends(resolve_service), job_service=Depends(resolve_jobs)):
        from backend.operations import operations_snapshot
        return operations_snapshot(job_service.store, budget=service.budget)

    @router.post("/uploads/{upload_id}/inspect", tags=["uploads"], response_model=InspectionResponse)
    def inspect_upload(upload_id: str, body: InspectionRequest, service=Depends(resolve_service)):
        return service.inspect_upload(upload_id, body)

    @router.get("/backup", tags=["history"])
    def backup(service=Depends(resolve_service)):
        return Response(service.history.export_backup(), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="customerinsight-workspace-history.zip"',
            "X-Content-Type-Options": "nosniff",
        })

    @router.post("/analyses", tags=["analysis"], response_model=AnalysisResponse)
    def analyze(request: AnalysisRequest, service=Depends(resolve_service)):
        if os.getenv("APP_ANALYSIS_MODE", "direct").strip().lower() == "queued":
            raise ApiError(409, "background_jobs_required", "当前部署使用后台任务，请通过任务接口提交分析。")
        return service.analyze(request)

    @router.post("/jobs", tags=["jobs"], response_model=JobResponse, status_code=202)
    def create_job(request: JobRequest, response: Response, job_service=Depends(resolve_jobs)):
        result, created = job_service.create(request)
        response.status_code = 202 if created or result["status"] in {"queued", "running"} else 200
        if result["id"]:
            response.headers["Location"] = f"/api/v1/jobs/{result['id']}"
        return result

    @router.get("/jobs", tags=["jobs"], response_model=JobListResponse)
    def jobs(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), job_service=Depends(resolve_jobs)):
        return job_service.list(limit=limit, offset=offset)

    @router.get("/jobs/{job_id}", tags=["jobs"], response_model=JobResponse)
    def job(job_id: str, job_service=Depends(resolve_jobs)):
        return job_service.get(job_id)

    @router.post("/jobs/{job_id}/cancel", tags=["jobs"], response_model=JobResponse)
    def cancel_job(job_id: str, job_service=Depends(resolve_jobs)):
        return job_service.cancel(job_id)

    @router.post("/jobs/{job_id}/resume", tags=["jobs"], response_model=JobResponse)
    def resume_job(job_id: str, request: ResumeJobRequest, job_service=Depends(resolve_jobs)):
        return job_service.resume(job_id, request)

    @router.get("/batches", tags=["history"], response_model=BatchListResponse)
    def batches(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), service=Depends(resolve_service)):
        return service.list_batches(limit=limit, offset=offset)

    @router.get("/batches/{batch_id}", tags=["history"], response_model=BatchResponse)
    def batch(batch_id: str, service=Depends(resolve_service)):
        return service.batch(batch_id)

    @router.get("/batches/{batch_id}/view", tags=["history"], response_model=ReportViewResponse)
    def report_view(batch_id: str, comparison_id: str | None = None, service=Depends(resolve_service)):
        return service.report_view(batch_id, comparison_id)

    @router.get("/batches/{batch_id}/deletion", tags=["history"], response_model=DeletionResponse)
    def deletion(batch_id: str, service=Depends(resolve_service), job_service=Depends(resolve_jobs)):
        record = service.history.load_batch(batch_id)
        return {"allowed": not job_service.store.has_related_jobs(batch_id, record.get("content_hash"))}

    @router.delete("/batches/{batch_id}", tags=["history"], status_code=204)
    def delete_batch(batch_id: str, service=Depends(resolve_service), job_service=Depends(resolve_jobs)):
        record = service.history.load_batch(batch_id)
        with job_service.store.history_delete_guard(batch_id, original_hash=record.get("content_hash")) as allowed:
            if not allowed:
                raise ApiError(409, "batch_retained_by_job", "后台任务仍引用该原件或报告，请保留历史。")
            service.history.delete_batch(batch_id)
        return Response(status_code=204)

    @router.get("/batches/{batch_id}/rows", tags=["history"], response_model=RowsResponse)
    def rows(batch_id: str, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), service=Depends(resolve_service)):
        return service.rows(batch_id, limit=limit, offset=offset)

    @router.get("/batches/{batch_id}/exports/{kind}", tags=["reports"])
    def export(batch_id: str, kind: Literal["csv", "html", "original"], comparison_id: str | None = None, service=Depends(resolve_service)):
        data, media, filename = service.export(batch_id, kind, comparison_id=comparison_id)
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            # Downloaded reports are standalone HTML, not executable app pages.
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
        })

    @router.get("/batches/{batch_id}/chat", tags=["agent"], response_model=ChatHistoryResponse)
    def chat_history(batch_id: str, comparison_id: str | None = None, service=Depends(resolve_service)):
        return service.chat_history(batch_id, comparison_id)

    @router.delete("/batches/{batch_id}/chat", tags=["agent"], status_code=204)
    def clear_chat(batch_id: str, comparison_id: str | None = None, service=Depends(resolve_service)):
        service.clear_chat(batch_id, comparison_id)
        return Response(status_code=204)

    @router.post("/batches/{batch_id}/chat", tags=["agent"], response_model=ChatResponse)
    def chat(batch_id: str, request: ChatRequest, service=Depends(resolve_service)):
        return service.chat(batch_id, request)

    app.include_router(router)
    return app


app = create_app()
