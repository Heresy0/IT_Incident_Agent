"""Small, strict contracts for bounded incident collaboration."""
from typing import Any, Literal
from pydantic import Field
from .contracts import Contract, EscalationSelection, ReferenceSelection, SelectedFinding, Finding, EvidenceRef

Role = Literal["investigation", "knowledge"]


class TaskRequest(Contract):
    role: Role
    goal: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class SupervisorPlan(Contract):
    action: Literal["dispatch", "diagnose", "request_info", "escalate", "finish"]
    tasks: list[TaskRequest] = Field(default_factory=list, max_length=2)
    reason: str = Field(min_length=1, max_length=500)
    missing_information: list[str] = Field(default_factory=list, max_length=8)


class SupervisorSelection(SupervisorPlan, EscalationSelection):
    """Model selects an observed owner; decisions retain the expanded public team."""


class SupervisorDecision(SupervisorPlan):
    escalation_team: str | None = Field(default=None, max_length=80)


class HypothesisSelection(Contract):
    cause: str = Field(min_length=1, max_length=500)
    support_refs: list[ReferenceSelection] = Field(default_factory=list, max_length=8)
    counter_refs: list[ReferenceSelection] = Field(default_factory=list, max_length=8)
    pending_checks: list[str] = Field(default_factory=list, max_length=4)


class Hypothesis(Contract):
    hypothesis_id: str
    cause: str
    support_refs: list[EvidenceRef]
    counter_refs: list[EvidenceRef]
    pending_checks: list[str]
    status: Literal["tentative", "supported", "refuted", "unresolved"] = "tentative"


class RecommendedAction(Contract):
    action: str = Field(min_length=1, max_length=500)
    condition: str = Field(min_length=1, max_length=300)
    expected_result: str = Field(min_length=1, max_length=300)
    risk: str = Field(min_length=1, max_length=300)
    requires_approval: bool


class DiagnosisSelection(EscalationSelection):
    findings: list[SelectedFinding] = Field(default_factory=list, max_length=8)
    hypotheses: list[HypothesisSelection] = Field(default_factory=list, max_length=4)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list, max_length=4)
    missing_information: list[str] = Field(default_factory=list, max_length=8)


class DiagnosisDraft(Contract):
    findings: list[Finding]
    hypotheses: list[Hypothesis]
    recommended_actions: list[RecommendedAction]
    missing_information: list[str]
    escalation_team: str | None


class Assessment(Contract):
    target_id: str = Field(pattern=r"^[FHA][1-9][0-9]?$", max_length=3)
    verdict: Literal["supported", "not_supported", "uncertain"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class ReadOnlyCheck(Contract):
    tool: Literal['get_service_metrics', 'get_service_logs', 'get_recent_changes',
                  'get_service_owner', 'search_runbooks', 'search_incidents']
    args: dict[str, Any] = Field(description='Exact arguments from the supplied read-only tool schema; no identities or changes.')


class EvidenceRequest(Contract):
    hypothesis_id: str = Field(pattern=r"^H[1-4]$")
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    missing_observation: str = Field(min_length=1, max_length=400)
    proposed_check: str = Field(min_length=1, max_length=400)
    expected_value: str = Field(min_length=1, max_length=400)
    target_role: Role = "investigation"
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2,
        description='One or two executable read-only checks. Empty means a human-only proposal, never automatic rework.')


class ReviewDecision(Contract):
    assessments: list[Assessment] = Field(min_length=1, max_length=16)
    request_evidence: EvidenceRequest | None = None
    missing_information: list[str] = Field(default_factory=list, max_length=8)
