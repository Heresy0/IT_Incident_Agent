from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from evidence.contracts import Window


class IncidentCreate(Window):
    # HTTP JSON is already decoded by FastAPI; permit ISO datetime parsing here.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)
    title: str = Field(min_length=1, max_length=160)
    symptoms: str = Field(min_length=1, max_length=2000)
    purpose: Literal['diagnosis', 'status_check'] = 'diagnosis'
    service: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    environment: Literal["staging", "production"]
    service_version: str = Field(min_length=1, max_length=40)
    occurred_at: datetime
    impact: str = Field(default="", max_length=500)
    reported_severity: Literal["unknown", "low", "medium", "high", "critical"] = "unknown"
    demo_case_id: Literal["case_001", "case_002", "case_003", "case_004", "case_005", "case_006"] | None = None

    @model_validator(mode="after")
    def occurred_in_window(self):
        if self.occurred_at.tzinfo is None or not self.start <= self.occurred_at <= self.end:
            raise ValueError("occurred_at must be timezone-aware and inside the window")
        return self


class IncidentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    changes: IncidentCreate


class DiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    execution_mode: Literal["scripted_control_only", "live"] = "scripted_control_only"
    model_budget: int | None = Field(default=None, ge=6, le=16)
    tool_budget: int | None = Field(default=None, ge=1, le=24)

    @model_validator(mode="after")
    def explicit_allowance(self):
        if self.execution_mode == "live" and (self.model_budget is None or self.tool_budget is None):
            raise ValueError("live requires explicit model_budget and tool_budget")
        return self


class SubmittedObservation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_name: str = Field(min_length=1, max_length=120, strict=True)
    service: str = Field(min_length=1, max_length=80, strict=True)
    observed_at: datetime
    content: str = Field(min_length=1, max_length=2000, strict=True)

    @model_validator(mode='after')
    def valid_source(self):
        if self.observed_at.tzinfo is None or not self.source_name.strip() or not self.content.strip():
            raise ValueError('A named source, nonblank content and timezone-aware observation time are required')
        return self


class FollowupInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    parent_run_id: str = Field(min_length=1, max_length=40)
    revision: int = Field(ge=1)
    note: str = Field(default='', max_length=2000)
    observations: list[SubmittedObservation] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def has_information(self):
        if not self.note.strip() and not self.observations:
            raise ValueError('Explain the disputed point or provide a sourced observation')
        return self


class FollowupDiagnosisRequest(DiagnosisRequest):
    followup: FollowupInput


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    revision: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=40)
    confirmed_cause: str = Field(min_length=1, max_length=1000)
    resolution: str = Field(min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    error_codes: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded_labels(self):
        if any(not s or len(s) > 64 for s in [*self.tags, *self.error_codes]):
            raise ValueError("labels must be 1..64 characters")
        return self
