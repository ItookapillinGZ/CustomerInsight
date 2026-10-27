from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.enums import ActionItemPriorityType, ProcessingStatus, TopicType, SentimentType
from core.review_topics import ReviewAspect, validate_aspects

class ActionableItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    priority: ActionItemPriorityType
    category: TopicType
    reasoning: str

    @field_validator("action", "reasoning")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Action and reasoning must contain text.")
        return value


class FeedbackAnalysis(BaseModel):
    """Keep the original single-feedback contract for callers."""

    model_config = ConfigDict(extra="forbid")

    sentiment: SentimentType
    actionable_items: list[ActionableItem] = Field(max_length=5)


class CustomerFeedbackAnalysis(FeedbackAnalysis):
    """LLM-owned fields; identifiers and processing metadata stay in Python."""

    category: TopicType
    priority: ActionItemPriorityType
    summary: str

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Summary must contain text.")
        return value


class FeedbackResult(BaseModel):
    """One traceable result for every CSV data record, including skipped rows."""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str
    source_row: int = Field(ge=2)
    original_text: str
    sentiment: SentimentType | None = None
    category: TopicType | None = None
    priority: ActionItemPriorityType | None = None
    summary: str | None = None
    processing_status: ProcessingStatus = ProcessingStatus.PENDING
    latency_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    error_message: str | None = None
    duplicate_of: str | None = None
    aspects: list[ReviewAspect] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_result_state(self) -> "FeedbackResult":
        analysis_fields = (self.sentiment, self.category, self.priority, self.summary)
        if self.processing_status == ProcessingStatus.SUCCESS:
            if any(value is None for value in analysis_fields) or not self.summary.strip():
                raise ValueError("Successful rows require all validated analysis fields.")
            if self.error_message is not None:
                raise ValueError("Successful rows cannot contain an error.")
            self.aspects = validate_aspects(self.aspects, self.original_text)
        elif any(value is not None for value in analysis_fields):
            raise ValueError("Non-successful rows cannot contain analysis labels.")
        elif self.aspects:
            raise ValueError("Non-successful rows cannot contain review aspects.")
        return self

