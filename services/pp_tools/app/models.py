"""Request and response contracts shared by n8n and pp-tools."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

Mode = Literal["novelty", "audit"]
RunStatus = Literal["received", "running", "done", "failed"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed"})

IDEA_MIN_CHARS = 20
IDEA_MAX_CHARS = 5000


class RunRequest(BaseModel):
    """What a user submits through the webhook or form."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Mode
    idea_text: str | None = Field(default=None, min_length=IDEA_MIN_CHARS, max_length=IDEA_MAX_CHARS)
    draft_pdf_url: HttpUrl | None = None
    venue: str | None = Field(default=None, max_length=200)
    query_language: Literal["en", "fr"] = "en"

    @field_validator("idea_text", "venue", mode="before")
    @classmethod
    def blank_to_none(cls, v: Any) -> Any:
        # A form often sends "" for an empty field; treat it as missing.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def required_fields_per_mode(self) -> "RunRequest":
        if self.mode == "novelty" and self.idea_text is None:
            raise ValueError("idea_text is required when mode is 'novelty'")
        if self.mode == "audit" and self.draft_pdf_url is None:
            raise ValueError("draft_pdf_url is required when mode is 'audit'")
        return self


class CreateRunBody(BaseModel):
    """Body n8n sends to POST /runs: the raw user request plus tracing info."""

    model_config = ConfigDict(extra="forbid")

    # Kept as a plain dict here so we can return field-level errors ourselves.
    request: dict[str, Any]
    n8n_execution_id: str | None = Field(default=None, max_length=100)


class CreateRunResponse(BaseModel):
    run_id: UUID
    mode: Mode
    status: RunStatus
    request: RunRequest


class UpdateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class FailByExecutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n8n_execution_id: str = Field(min_length=1, max_length=100)
    error: dict[str, Any]


class RunRecord(BaseModel):
    run_id: UUID
    mode: Mode
    status: RunStatus
    input: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    tokens_used: int
    cost_usd: float
    n8n_execution_id: str | None
    created_at: str
    updated_at: str


class FieldError(BaseModel):
    field: str
    message: str


class ValidationErrorResponse(BaseModel):
    errors: list[FieldError]
