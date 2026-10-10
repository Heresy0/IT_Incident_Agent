"""Small, strict contracts for bounded incident collaboration."""
from typing import Any, Literal
from pydantic import Field
from evidence.contracts import Contract, EscalationSelection, ReferenceSelection, SelectedFinding, Finding, EvidenceRef
from repairs.contracts import RepairIntent

Role = Literal["investigation", "knowledge"]


class ReadOnlyCheck(Contract):
    tool: Literal['get_service_metrics', 'get_service_logs', 'get_recent_changes',
                  'get_service_owner', 'search_runbooks', 'search_incidents']
    args: dict[str, Any] = Field(description='Exact arguments from the supplied read-only tool schema; no identities or changes.')


class EvidenceNeed(Contract):
    need_id: str = Field(pattern=r'^N[1-6]$')
    question: str = Field(min_length=1, max_length=400)
    purpose: Literal['symptom', 'mechanism', 'alternative', 'action_condition', 'trigger']
    status: Literal['pending', 'supported', 'unavailable', 'deferred', 'not_required'] = 'pending'
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=400)
    next_check: ReadOnlyCheck | None = None
    role: Role = 'investigation'
    blocking: bool | None = Field(default=None,
        description='Whether this unresolved question changes the current diagnosis or next step. Symptom/mechanism always block; null preserves the conservative default. Explain a nonblocking limitation in reason.')


class NeedAssessment(Contract):
    need_id: str = Field(pattern=r'^N[1-6]$')
    verdict: Literal['supported', 'uncertain', 'not_required']
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=400)
    blocking: bool | None = Field(default=None,
        description='Independently assess materiality; null retains the existing setting. Symptom/mechanism cannot be nonblocking.')


class TaskRequest(Contract):
    role: Role
    goal: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    need_ids: list[str] = Field(default_factory=list, max_length=6,
        description='Existing N identifiers whose questions this task investigates. Task completion does not resolve them.')
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2,
        description='Concrete checks required by this goal, from read_only_tools[role]. They define coverage, not automatic execution or causal proof. Reuse completed results.')
    expected_value: str | None = Field(default=None, min_length=1, max_length=400,
        description='What observed outcome would support or distinguish the candidate mechanism? Not a claimed observation.')


class SupervisorPlan(Contract):
    action: Literal["dispatch", "diagnose", "request_info", "escalate", "finish"]
    tasks: list[TaskRequest] = Field(default_factory=list, max_length=2)
    reason: str = Field(min_length=1, max_length=500)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    evidence_needs: list[EvidenceNeed] = Field(default_factory=list, max_length=6,
        description='Full updated shared ledger. Preserve existing identifiers and purposes. Omission retains unresolved needs, never proves completion.')


class EvidenceNeedUpdate(Contract):
    """Partial assessment of a server-owned question; identity and purpose stay fixed."""
    need_id: str = Field(pattern=r'^N[1-6]$')
    status: Literal['pending', 'supported', 'unavailable', 'deferred', 'not_required']
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=400)
    next_check: ReadOnlyCheck | None = None
    role: Role = 'investigation'
    blocking: bool | None = Field(default=None,
        description='Only optional questions may be nonblocking. The server always keeps symptom/mechanism blocking.')


class AdditionalEvidenceNeed(Contract):
    need_id: str = Field(pattern=r'^N[1-6]$')
    question: str = Field(min_length=1, max_length=400)
    purpose: Literal['trigger', 'action_condition']
    reason: str = Field(min_length=1, max_length=400)
    role: Role = 'investigation'


class SupervisorRequest(EscalationSelection):
    """Small model-facing contract; no round-trip of the full shared ledger."""
    action: Literal['dispatch', 'diagnose', 'request_info', 'escalate', 'finish']
    tasks: list[TaskRequest] = Field(default_factory=list, max_length=2)
    reason: str = Field(min_length=1, max_length=500)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    need_updates: list[EvidenceNeedUpdate] = Field(default_factory=list, max_length=6,
        description='Only changed assessments of existing N identifiers. Omitted questions stay unchanged; no automatic support.')
    additional_needs: list[AdditionalEvidenceNeed] = Field(default_factory=list, max_length=3)


class SupervisorSelection(SupervisorRequest):
    # Compatibility input for old scripted responses; not advertised to models.
    evidence_needs: list[EvidenceNeed] = Field(default_factory=list, max_length=6)


class SupervisorDecision(SupervisorPlan):
    escalation_team: str | None = Field(default=None, max_length=80)


class HypothesisSelection(Contract):
    cause: str = Field(min_length=1, max_length=500)
    level: Literal['mechanism', 'trigger', 'alternative', 'unspecified'] = Field(default='unspecified',
        description='Mechanism explains how the symptom occurs; trigger explains why it started; alternative competes with that explanation. Unspecified supports legacy reports only.')
    evidence_explanation: str = Field(default='', max_length=500,
        description='Brief public explanation of which cited observation supports the causal link, and what remains unknown. No hidden reasoning.')
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
    level: Literal['mechanism', 'trigger', 'alternative', 'unspecified'] = 'unspecified'
    evidence_explanation: str = Field(default='', max_length=500)


class RecommendedAction(Contract):
    action: str = Field(min_length=1, max_length=500)
    condition: str = Field(min_length=1, max_length=300)
    expected_result: str = Field(min_length=1, max_length=300)
    risk: str = Field(min_length=1, max_length=300)
    requires_approval: bool
    kind: Literal['read_only_check', 'manual_change', 'registered_repair', 'unspecified'] = Field(default='unspecified',
        description='Separate investigation from changes. Unspecified is the legacy default, never inferred from action prose.')
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2,
        description='Executable scoped read-only proposals for read_only_check; never automatically executed or approved by this report.')
    repair: RepairIntent | None = Field(default=None,
        description='Optional candidate from registered repair capabilities; never approval or execution. Cite current evidence IDs.')


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


class EvidenceRequest(Contract):
    hypothesis_id: str = Field(pattern=r"^H[1-4]$")
    need_id: str | None = Field(default=None, pattern=r'^N[1-6]$')
    target_id: str | None = Field(default=None, pattern=r'^[HA][1-4]$',
        description='Disputed H or A target. Defaults to hypothesis_id. An A target may need applicability checks while its related H remains supported.')
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    missing_observation: str = Field(min_length=1, max_length=400)
    proposed_check: str = Field(min_length=1, max_length=400)
    expected_value: str = Field(min_length=1, max_length=400)
    target_role: Role = "investigation"
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2,
        description='One or two executable read-only checks. Empty means a human-only proposal, never automatic rework.')


class ReviewDecision(Contract):
    assessments: list[Assessment] = Field(min_length=1, max_length=16)
    need_assessments: list[NeedAssessment] = Field(default_factory=list, max_length=6,
        description='Assess every shared evidence need once, independently of declared task completion. Missing entries remain uncertain.')
    request_evidence: EvidenceRequest | None = None
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    follow_up_reason: str | None = Field(default=None, min_length=1, max_length=400,
        description='When an H/A is uncertain and no evidence request is made, explain why no useful scoped read-only check is available or appropriate.')
