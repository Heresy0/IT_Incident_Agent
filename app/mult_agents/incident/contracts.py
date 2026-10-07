from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Window(Contract):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start or (self.end - self.start).total_seconds() > 86400:
            raise ValueError("window must be ordered and at most 24 hours")
        return self


class IncidentScope(Window):
    tenant_id: str
    user_id: str
    service: str
    environment: Literal["staging", "production"]
    service_version: str
    allowed_dependencies: tuple[str, ...] = ()


class Ticket(Contract):
    incident_id: str
    title: str = Field(max_length=160)
    symptoms: str = Field(max_length=2000)
    scope: IncidentScope


Metric = Literal["request_error_rate", "request_latency", "dependency_error_rate", "db_cpu",
                 "pool_usage", "pool_wait", "request_rate"]
Category = Literal["dependency", "configuration", "resource"]


class MetricsArgs(Window):
    metrics: list[Metric] = Field(min_length=1, max_length=7)
    granularity: Literal["1m", "5m"] = "1m"

    @field_validator("metrics")
    @classmethod
    def canonical(cls, value):
        return sorted(set(value))


class LogsArgs(Window):
    category: Category
    level: Literal["INFO", "WARN", "ERROR"] | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z0-9_]+$")


class ChangesArgs(Window):
    category: Literal["deployment", "configuration", "scaling"] | None = None


class RunbooksArgs(Contract):
    query: str = Field(min_length=1, max_length=200)
    category: Category | None = None


class IncidentsArgs(Contract):
    symptoms: str = Field(min_length=1, max_length=200)
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z0-9_]+$")
    version: str | None = Field(default=None, max_length=40)


class OwnerArgs(Contract):
    alias: str = Field(min_length=1, max_length=80)


class ToolError(Contract):
    code: str
    retryable: bool = False


class Evidence(Contract):
    evidence_id: str
    kind: Literal["observation", "runbook", "past_incident"]
    provider: str
    service: str
    environment: str
    observed_from: datetime
    observed_to: datetime
    retrieved_at: datetime
    payload: dict[str, Any]
    excerpt: str
    locator: str
    data_version: str
    hash: str
    allowed_field_paths: list[str] = Field(default_factory=list, max_length=40)


class ToolResult(Contract):
    status: Literal["ok", "empty", "error"]
    evidence: list[Evidence] = Field(default_factory=list)
    truncated: bool = False
    error: ToolError | None = None


class EvidenceRef(Contract):
    evidence_id: str
    field_path: str = Field(max_length=120, description="Dot path relative to this evidence's payload. Choose an exact allowed_field_paths entry, e.g. value, message, team or config_summary.pool_max. Never prepend payload. or the evidence ID.")
    value: Any
    unit: str
    observed_at: datetime
    data_version: str
    quote: str = Field(min_length=1, max_length=1000)


class Finding(Contract):
    statement: str = Field(min_length=1, max_length=500)
    refs: list[EvidenceRef] = Field(min_length=1, max_length=8)


class InvestigationOutput(Contract):
    findings: list[Finding] = Field(default_factory=list, max_length=12)
    tentative_hypotheses: list[str] = Field(default_factory=list, max_length=5)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    escalation_team: str | None = Field(default=None, max_length=80)


TOOL_ARGS = {
    "get_service_metrics": MetricsArgs, "get_service_logs": LogsArgs,
    "get_recent_changes": ChangesArgs, "search_runbooks": RunbooksArgs,
    "search_incidents": IncidentsArgs, "get_service_owner": OwnerArgs,
}
INVESTIGATION_TOOLS = frozenset({"get_service_metrics", "get_service_logs", "get_recent_changes", "get_service_owner"})
KNOWLEDGE_TOOLS = frozenset({"search_runbooks", "search_incidents"})
