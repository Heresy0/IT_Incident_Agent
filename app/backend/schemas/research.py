from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=6000)
    user_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    thread_id: str = Field(default="default_thread", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    tenant_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    max_iterations: int | None = Field(default=None, ge=0, le=1)
    enable_memory: bool = False


class ResearchResponse(BaseModel):
    query: str
    user_id: str
    thread_id: str
    tenant_id: str
    final: str
    run_id: str
    status: str
    route: str = "unknown"
    run_summary: dict = Field(default_factory=dict)
    sources: list[dict] = Field(default_factory=list)
    question_coverage: list[dict] = Field(default_factory=list)
    coverage_stage: str = "not_assessed"
    missing_gaps: list[str] = Field(default_factory=list)
    verification_rejections: list[dict] = Field(default_factory=list)
