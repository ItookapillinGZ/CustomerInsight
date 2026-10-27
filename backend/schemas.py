"""Strict input contracts: reading data never implies consent to a model call."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.analysis_agent import AnalysisAgentResult


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    upload_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sheet_name: str | None = Field(default=None, max_length=255)
    encoding: Literal["utf-8-sig", "gb18030"] = "utf-8-sig"
    text_column: str = Field(min_length=1, max_length=1000)
    group_column: str | None = Field(default=None, max_length=1000)
    date_column: str | None = Field(default=None, max_length=1000)
    allow_model_call: bool = False
    max_requests: int | None = Field(default=None, ge=1, le=10000)
    force: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question: str = Field(min_length=1, max_length=300)
    allow_model_call: bool = False
    max_requests: int | None = Field(default=None, ge=1, le=10000)
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("question")
    @classmethod
    def require_question(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空。")
        return value


class JobRequest(AnalysisRequest):
    idempotency_key: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ResumeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allow_model_call: bool = False
    max_requests: int = Field(ge=1, le=10000)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    username: str
    workspace_id: str


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str
    token_type: Literal["bearer"]
    expires_at: float
    user: AccountResponse


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoWorkspaceRequest(BaseModel):
    """Only the bundled fixture can be imported; client data is forbidden."""
    model_config = ConfigDict(extra="forbid", strict=True)


class DemoWorkspaceResponse(PublicResponse):
    current_batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    previous_batch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    model_calls: Literal[0] = 0
    data_origin: Literal["synthetic"] = "synthetic"


class HealthResponse(PublicResponse):
    status: Literal["ok"]


class ErrorDetail(PublicResponse):
    code: str
    message: str


class ErrorResponse(PublicResponse):
    error: ErrorDetail
    request_id: str


class UploadResponse(PublicResponse):
    upload_id: str
    filename: str
    size_bytes: int = Field(ge=1)
    sheet_names: list[str]
    expires_at: str


class MappingSuggestion(PublicResponse):
    text_column: str | None
    group_column: str | None
    date_column: str | None


class PreviewResponse(PublicResponse):
    upload_id: str
    filename: str
    sheet_name: str | None
    columns: list[str]
    total_rows: int = Field(ge=0)
    rows: list[dict[str, Any]]
    suggested_mapping: MappingSuggestion


class BatchMetadata(PublicResponse):
    id: str
    filename: str
    created_at: str
    updated_at: str
    total_rows: int = Field(ge=0)
    success_rows: int = Field(ge=0)
    text_column: str | None
    group_column: str | None
    mapping: dict[str, Any]
    content_hash: str
    report_fingerprint: str
    analysis_meta: dict[str, Any]


class AnalysisResponse(PublicResponse):
    batch_id: str
    cached: bool
    status: Literal["completed", "partial"]
    metadata: BatchMetadata
    error_code: str | None = None


class BatchListResponse(PublicResponse):
    items: list[BatchMetadata]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class BatchResponse(PublicResponse):
    metadata: BatchMetadata
    report: dict[str, Any]


class RowsResponse(PublicResponse):
    columns: list[str]
    items: list[dict[str, Any]]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class ChatTurn(PublicResponse):
    question: str
    result: AnalysisAgentResult | None = None
    comparison_batch_id: str | None = None
    error: str | None = None


class ChatHistoryResponse(PublicResponse):
    items: list[ChatTurn]
    total: int = Field(ge=0)
    comparison_id: str | None


class ChatResponse(PublicResponse):
    result: AnalysisAgentResult
    turn_count: int = Field(ge=1)


class JobResponse(PublicResponse):
    id: str | None
    status: Literal["queued", "running", "partial", "completed", "failed", "cancelled", "needs_review"]
    filename: str
    total_rows: int = Field(ge=0)
    completed_rows: int = Field(ge=0)
    success_rows: int = Field(ge=0)
    total_groups: int = Field(ge=0)
    completed_groups: int = Field(ge=0)
    failed_groups: int = Field(ge=0)
    uncertain_groups: int = Field(ge=0)
    request_count: int = Field(ge=0)
    max_requests: int = Field(ge=0)
    batch_id: str | None = None
    error_code: str | None = None
    can_resume: bool = False
    cancel_requested: bool = False
    cached: bool = False
    created_at: str
    updated_at: str


class JobListResponse(PublicResponse):
    items: list[JobResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class InspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sheet_name: str | None = Field(default=None, max_length=255)
    encoding: Literal["utf-8-sig", "gb18030"] = "utf-8-sig"
    text_column: str = Field(min_length=1, max_length=1000)
    group_column: str | None = Field(default=None, max_length=1000)
    date_column: str | None = Field(default=None, max_length=1000)


class InspectionResponse(PublicResponse):
    mapping: dict[str, Any]
    total_rows: int
    counts: dict[str, int]
    expected_requests: int
    cached_batch_id: str | None = None


class ReportViewResponse(PublicResponse):
    metadata: BatchMetadata
    packed_report: str


class ServiceStatusResponse(PublicResponse):
    configured: bool
    remaining_today: int
    daily_limit: int
    request_batch_size: int
    max_batch_characters: int
    agent_requests_per_run: int


class DeletionResponse(PublicResponse):
    allowed: bool
