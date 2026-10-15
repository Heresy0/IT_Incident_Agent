"""Small, strict contracts for bounded incident collaboration."""
from typing import Any, Literal
from pydantic import Field
from evidence.contracts import Contract, EscalationSelection, ReferenceSelection, SelectedFinding, Finding, EvidenceRef
from repairs.contracts import RepairIntent

Role = Literal["investigation", "knowledge"]

# Query proposals promise information, never a particular cause or recovery.
CHECK_EXPECTED_RESULTS = {
    'read_only_check': '取得登记查询的实际结果，判断哪些候选原因得到支持或反证；查询失败或证据不足时保留缺口。',
    'manual_check': '取得上述人工核查的实际记录与结果，判断哪些候选原因得到支持或反证；无法确认的原因继续保留。',
}


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


class ReviewBlocker(Contract):
    cause_level: Literal['observation', 'mechanism', 'trigger', 'alternative', 'action_condition']
    affected_targets: list[str] = Field(min_length=1, max_length=4,
        description='Actual F/H/A targets whose verdict would change with this observation.')
    missing_observation: str = Field(min_length=1, max_length=300)
    decision_impact: str = Field(min_length=1, max_length=300,
        description='Which possible result changes which conclusion or next step?')
    resolution: Literal['scoped_check', 'external_source', 'not_planned']
    target_role: Role = 'investigation'
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2)


class NeedAssessment(Contract):
    need_id: str = Field(pattern=r'^N[1-6]$')
    verdict: Literal['supported', 'uncertain', 'not_required']
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=400)
    blocking: bool | None = Field(default=None,
        description='Independently assess materiality; null retains the existing setting. Symptom/mechanism cannot be nonblocking.')
    blocker: ReviewBlocker | None = Field(default=None,
        description='Required for a blocking uncertain question in the live workflow. External source means another service not readable by the scoped tools, not an empty query.')


class TaskRequest(Contract):
    role: Role
    goal: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    need_ids: list[str] = Field(default_factory=list, max_length=6,
        description='Existing N identifiers whose questions this task investigates. Task completion does not resolve them.')
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2,
        description='Concrete checks required by this goal, from read_only_tools[role]. Programmatic Investigation executes validated checks; execution does not prove a cause. Reuse completed results.')
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
    expected_result: str = Field(min_length=1, max_length=300,
        description='For checks: information to obtain and how different outcomes affect the decision; do not assert an unverified cause or a predetermined result. For changes: conditional effect, not proof that prerequisites hold or recovery is guaranteed.')
    risk: str = Field(min_length=1, max_length=300)
    requires_approval: bool
    kind: Literal['read_only_check', 'manual_check', 'manual_change', 'registered_repair', 'unspecified'] = Field(default='unspecified',
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
    rejected_actions: list[dict[str, str]] = Field(default_factory=list)


class RevisionConcern(Contract):
    target_id: str = Field(pattern=r'^H[1-4]$')
    reason: str = Field(min_length=1, max_length=300)
    candidate: HypothesisSelection


class RevisionDiagnosisSelection(DiagnosisSelection):
    revision_concerns: list[RevisionConcern] = Field(default_factory=list, max_length=4,
        description='Untrusted proposed corrections to non-target hypotheses; never edits to the committed draft.')


class RefutationBasis(Contract):
    kind: Literal['direct_contradiction', 'weakens_explanation', 'absence_of_support']
    claim: str = Field(min_length=1, max_length=300,
        description='The specific assertion in the target hypothesis being challenged. Classification is a model judgment, not a program-certified contradiction.')


class Assessment(Contract):
    target_id: str = Field(pattern=r"^[FHA][1-9][0-9]?$", max_length=3)
    verdict: Literal["supported", "not_supported", "uncertain"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    refutation_basis: RefutationBasis | None = None


class EvidenceRequest(Contract):
    hypothesis_id: str | None = Field(default=None, pattern=r"^H[1-4]$")
    need_id: str | None = Field(default=None, pattern=r'^N[1-6]$')
    target_id: str | None = Field(default=None, pattern=r'^[HA][1-4]$',
        description='Disputed H or A target. Defaults to hypothesis_id. An A target may need applicability checks while its related H remains supported.')
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    missing_observation: str = Field(min_length=1, max_length=400)
    proposed_check: str = Field(default='', max_length=400,
        description='Optional investigation hint, not an executable instruction.')
    expected_value: str = Field(default='', max_length=400,
        description='Optional evidence acceptance condition; Supervisor designs the concrete check.')
    target_role: Role = "investigation"
    checks: list[ReadOnlyCheck] = Field(default_factory=list, max_length=2,
        description='Optional read-only hints. Supervisor may replace these; only its validated plan authorizes rework.')


class ReviewDecision(Contract):
    assessments: list[Assessment] = Field(min_length=1, max_length=16)
    need_assessments: list[NeedAssessment] = Field(default_factory=list, max_length=6,
        description='Assess every shared evidence need once, independently of declared task completion. Missing entries remain uncertain.')
    request_evidence: EvidenceRequest | None = None
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    follow_up_reason: str | None = Field(default=None, min_length=1, max_length=400,
        description='When an H/A is uncertain and no evidence request is made, explain why no useful scoped read-only check is available or appropriate.')


class RevisionRequest(Contract):
    target_id: str = Field(pattern=r'^[HA][1-4]$')
    issue_type: Literal['expression', 'reasoning']
    reason: str = Field(min_length=1, max_length=300)
    required_change: str = Field(min_length=1, max_length=300,
        description='A correction possible from existing evidence; do not request new observations here.')


class RevisionScopeAssessment(Contract):
    target_id: str = Field(pattern=r'^H[1-4]$')
    disposition: Literal['keep', 'revise', 'evidence']
    reason: str = Field(min_length=1, max_length=300)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class RevisionNeedAssessment(NeedAssessment):
    gap_kind: Literal['evidence', 'revision'] = 'evidence'
    revision_targets: list[str] = Field(default_factory=list, max_length=4,
        description='For gap_kind=revision only: existing H/A requiring an authorized edit, not a new observation.')


class RequestedSource(Contract):
    tool: Literal['get_service_logs', 'get_service_metrics', 'get_recent_changes',
                  'get_service_owner', 'search_runbooks', 'search_incidents']
    category: Literal['dependency', 'configuration', 'resource'] | None = None


class CounterpointField(Contract):
    evidence_id: str
    field_path: str = Field(min_length=1, max_length=120)


class ClaimCounterpoint(Contract):
    target_id: str = Field(pattern=r'^H[1-4]$')
    claim_quote: str = Field(min_length=3, max_length=300,
        description='An exact substring of the existing hypothesis cause, not a rewritten broader claim.')
    effect: Literal['weakens', 'conflicts_with_quoted_claim', 'insufficient_to_exclude']
    reason: str = Field(min_length=1, max_length=300)
    fields: list[CounterpointField] = Field(min_length=1, max_length=4)
    remaining_uncertainty: str = Field(min_length=1, max_length=300)


class ReadStateAssertion(Contract):
    read_id: str = Field(pattern=r'^RD_[0-9a-f]{16}$')
    state: Literal['observed', 'empty', 'failed', 'not_queried', 'blocked_channel', 'truncated', 'missing_samples']


class ManualHandoffTask(Contract):
    target_id: str = Field(pattern=r'^A[1-4]$')
    action_quote: str = Field(min_length=3, max_length=500)
    subject: Literal['ticket_service', 'external_dependency']
    record_scope: Literal['anchored_record', 'new_observation'] = 'new_observation'
    question: str = Field(min_length=1, max_length=300)
    requested_fields: list[str] = Field(min_length=1, max_length=6)
    anchor_evidence_ids: list[str] = Field(default_factory=list, max_length=4)
    related_reads: list[ReadStateAssertion] = Field(default_factory=list, max_length=3)
    completion_criteria: str = Field(min_length=1, max_length=300)
    decision_use: str = Field(min_length=1, max_length=300)


class RevisionReviewDecision(ReviewDecision):
    counterpoints: list[ClaimCounterpoint] = Field(default_factory=list, max_length=4)
    ledger_assertions: list[ReadStateAssertion] = Field(default_factory=list, max_length=8)
    manual_tasks: list[ManualHandoffTask] = Field(default_factory=list, max_length=4)
    request_source: RequestedSource | None = Field(default=None,
        description='For request_evidence seeking a ticket-service channel: identify its tool and log category if known. '
                    'This is a channel hint, not permission or query parameters. External-service information leaves this null.')
    need_assessments: list[RevisionNeedAssessment] = Field(default_factory=list, max_length=6)
    revision_requests: list[RevisionRequest] = Field(default_factory=list, max_length=4,
        description='Only unsupported H/A wording or reasoning fixable with existing evidence. '
                    'Evidence gaps remain in request_evidence and need_assessments.')
    scope_assessments: list[RevisionScopeAssessment] = Field(default_factory=list, max_length=4,
        description='Independently resolve each untrusted non-target concern: keep original, authorize a reasoning edit, or seek evidence.')


class ObservedClaim(Contract):
    statement: str = Field(min_length=1, max_length=500)
    refs: list[ReferenceSelection] = Field(min_length=1, max_length=8)


class ObservationReview(Contract):
    directly_supported: list[ObservedClaim] = Field(default_factory=list, max_length=4)
    unverified_causes: list[str] = Field(default_factory=list, max_length=4,
        description='Specific causal explanations not established by these observations; keep them separate from observed failure mechanisms.')
