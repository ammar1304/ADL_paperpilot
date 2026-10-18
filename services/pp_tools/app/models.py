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
    # A PDF sent through the form: POST /uploads returns this id (the file's SHA-256).
    draft_pdf_upload: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    venue: str | None = Field(default=None, max_length=200)
    # Set by the web app (D41) so the page can follow the run before the webhook answers.
    client_ref: str | None = Field(default=None, pattern=r"^[a-z0-9]{12,40}$")
    query_language: Literal["en", "fr"] = "en"

    @field_validator("idea_text", "venue", "draft_pdf_url", "draft_pdf_upload", mode="before")
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
        if self.mode == "audit":
            if self.draft_pdf_url is None and self.draft_pdf_upload is None:
                raise ValueError("draft_pdf_url or draft_pdf_upload is required when mode is 'audit'")
            if self.draft_pdf_url is not None and self.draft_pdf_upload is not None:
                raise ValueError("give draft_pdf_url or draft_pdf_upload, not both")
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


# ---- corpus and retrieval (milestone 1) ------------------------------------------

Strategy = Literal["fixed", "structured"]


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # true: drop and rebuild both collections (reproducible); false: upsert into them.
    recreate: bool = True
    # Index only the first N manifest rows (quick checks); None = the whole manifest.
    limit: int | None = Field(default=None, ge=1, le=5000)
    n8n_execution_id: str | None = Field(default=None, max_length=100)


class JobAccepted(BaseModel):
    job_id: UUID
    status: Literal["queued"]


class JobRecord(BaseModel):
    job_id: UUID
    kind: str
    status: Literal["queued", "running", "done", "failed"]
    params: dict[str, Any]
    progress: dict[str, Any] | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    n8n_execution_id: str | None
    created_at: str
    updated_at: str


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str | None = None
    chunk_type: Literal["window", "abstract", "section", "table", "figure"] | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=3, max_length=2000)
    strategy: Strategy = "structured"
    k: int = Field(default=10, ge=1, le=50)
    # "chunk": top-k chunks; "paper": top-k distinct papers, best chunk each.
    level: Literal["chunk", "paper"] = "chunk"
    filters: SearchFilters | None = None


class SearchHit(BaseModel):
    chunk_id: str
    paper_id: str
    score: float
    chunk_type: str
    section: str | None
    title: str
    year: int
    text: str


class SearchResponse(BaseModel):
    query: str
    strategy: Strategy
    level: str
    hits: list[SearchHit]


# ---- prompts and novelty branch (milestone 2) ---------------------------------------


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variables: dict[str, Any] = Field(default_factory=dict)
    version: str | None = Field(default=None, pattern=r"^v\d+$")


class RenderedPromptOut(BaseModel):
    name: str
    version: str
    system: str
    user: str


class JudgePromptOut(RenderedPromptOut):
    attempt: int


class LlmOutput(BaseModel):
    """What n8n posts after an LLM node: the parsed output and the prompt version used."""

    model_config = ConfigDict(extra="forbid")

    output: Any
    prompt_version: str | None = Field(default=None, max_length=20)


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Strategy | None = None


class PlanApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: Any = None
    prompt_version: str | None = None


class RankRequest(BaseModel):
    """Stateless ranking for retrieval experiments: no run, no LLM, nothing stored."""

    model_config = ConfigDict(extra="forbid")

    idea_text: str = Field(min_length=3, max_length=4000)
    queries: list[str] = Field(min_length=1, max_length=8)
    strategy: Strategy | None = None
    rerank: bool | None = None
    idea_as_query: bool | None = None
    pool_papers: int | None = Field(default=None, ge=6, le=100)
    rerank_abstract: bool | None = None
    order: Literal["rerank", "hybrid"] | None = None
