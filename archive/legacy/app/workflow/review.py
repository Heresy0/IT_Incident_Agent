"""Review validation and bounded follow-up decisions from explicit read views.

Review rules never commit needs, execute queries or schedule model calls. Audit
diagnostics are returned through an explicit accumulator, including on failure.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

from agents.contracts import DiagnosisDraft, EvidenceNeed, EvidenceRequest, Hypothesis, ReviewDecision
from evidence.contracts import Evidence, EvidenceRef, IncidentScope
from evidence.diagnosis import ProtocolError, expand_refs
from evidence.observations import gap_message, source_statement
from runtime.context import ExecutionError
from workflow.diagnostic_audit import validate_blockers, blocker_follow_up
from workflow.evidence_needs import check_ids, validate_assessments, is_blocking, current_support
from workflow.review_outcome import readiness_gaps, nonblocking_trigger_request
from workflow.review_protection import conservative, BOUNDED_POLICY
from workflow.task_checks import adds_information


@dataclass(frozen=True)
class ReviewContext:
    needs: Mapping[str, EvidenceNeed]
    evidence: Mapping[str, Evidence]
    references: Mapping[str, EvidenceRef]
    executors: Mapping[str, Any]
    targets: Mapping[str, Any]
    scope: IncidentScope
    observation_gaps: Sequence[str]
    protection: str
    require_blockers: bool
    existing_gaps: Sequence[str]


@dataclass
class ReviewAudit:
    """Request-local diagnostics; Coordinator commits these even if validation fails."""
    hint_status: list[dict[str, str]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewRoutingContext:
    draft: DiagnosisDraft | None
    review: ReviewDecision | None
    needs: Mapping[str, EvidenceNeed]
    targets: Mapping[str, Any]
    purpose: str
    protection: str
    reworks: int
    protocol_fallback_used: bool
    clarification_used: bool


@dataclass(frozen=True)
class ReviewHandoff:
    request: EvidenceRequest | None
    deferred_request: EvidenceRequest | None = None
    reason: str | None = None
    limitation: str | None = None


@dataclass(frozen=True)
class ReworkDecision:
    kind: Literal['protocol_stop', 'limit_stop', 'budget_stop', 'plan']
    authorize_additional: bool = False


def validate_review(review: ReviewDecision, view: ReviewContext, *,
                    audit: ReviewAudit, emit: Callable[..., None]) -> ReviewDecision:
    core_flags = [a for a in review.need_assessments if a.need_id in view.needs
                  and view.needs[a.need_id].purpose in {'symptom', 'mechanism'}
                  and a.blocking is not None]
    assessments = [a.model_copy(update={'blocking': None}) if a in core_flags else a
                   for a in review.need_assessments]
    need_assessments = validate_assessments(view.needs, assessments, view.evidence)
    for assessment in core_flags:
        emit('reviewer_fields_ignored', 'reviewer', code='CORE_BLOCKING_RETAINED',
                   need_id=assessment.need_id, path='blocking', actual=assessment.blocking)
    targets = view.targets
    ids = [a.target_id for a in review.assessments]
    if len(ids) != len(set(ids)) or set(ids) != set(targets):
        raise ProtocolError("REVIEW_TARGET_COVERAGE", [{
            'required_target_ids': list(targets),
            'missing_target_ids': sorted(set(targets) - set(ids)),
            'unexpected_target_ids': sorted(set(ids) - set(targets)),
            'duplicate_target_ids': sorted({key for key in ids if ids.count(key) > 1}),
        }])
    checked = []
    for index, assessment in enumerate(review.assessments):
        check_ids(assessment.evidence_ids, view.evidence, f'assessments.{index}.evidence_ids')
        target = targets[assessment.target_id]
        if assessment.verdict == "supported" and assessment.target_id.startswith("H"):
            if not any(view.evidence[r.evidence_id].kind == "observation" for r in target.support_refs):
                assessment = assessment.model_copy(update={"verdict": "uncertain",
                    "reason": "Program gate: historical knowledge alone cannot establish a current cause."})
                emit("review_gate", "reviewer", target_id=assessment.target_id, code="NO_CURRENT_SUPPORT")
            elif view.observation_gaps:
                assessment = assessment.model_copy(update={'verdict': 'uncertain', 'reason': gap_message(view.observation_gaps)})
                emit('review_gate', 'reviewer', target_id=assessment.target_id, code='MISSING_HEALTH_CONTROL')
        checked.append(assessment)
    if conservative(view.protection):
        from workflow.review_protection import protect_refutations
        checked = protect_refutations(review.model_copy(update={'assessments': checked}),
            view.evidence, emit, bounded=view.protection == BOUNDED_POLICY,
            scope=view.scope).assessments
    request = review.request_evidence
    audit.hint_status = []
    if request:
        check_ids(request.evidence_ids, view.evidence, 'request_evidence.evidence_ids')
        if request.need_id is not None and request.need_id not in view.needs:
            raise ProtocolError('UNKNOWN_OR_DUPLICATE_NEED')
        disputed = request.target_id or request.hypothesis_id
        supported_check_request = (conservative(view.protection)
            and disputed in targets and disputed.startswith('A')
            and targets[disputed].kind in {'manual_check', 'read_only_check'})
        if (request.hypothesis_id is not None and request.hypothesis_id not in targets
                or disputed is not None and (disputed not in targets
                    or disputed.startswith('H') and request.hypothesis_id not in {None, disputed}
                    or any(a.target_id == disputed and a.verdict == 'supported' for a in checked)
                        and not supported_check_request)
                or disputed is None and request.need_id is None):
            raise ProtocolError('INVALID_REWORK_TARGET')
        if request.need_id and disputed and disputed.startswith('H'):
            purpose = view.needs[request.need_id].purpose
            level = targets[disputed].level
            if (level == 'trigger' and purpose in {'symptom', 'mechanism', 'alternative'}
                    or purpose == 'trigger' and level in {'mechanism', 'alternative'}):
                # The target and gap remain valid. Detach only the conflicting
                # edge; never promote a verdict or discard a missing observation.
                emit('review_request_scope_detached', 'reviewer', code='REVIEW_REQUEST_SCOPE',
                    need_id=request.need_id, target_id=disputed,
                    need_purpose=purpose, target_level=level, verdicts_changed=False)
                request = request.model_copy(update={'need_id': None})
        # Hints never authorize a call. Discard unsafe/redundant suggestions,
        # but preserve the legitimate evidence gap for Supervisor to plan.
        normalized, signatures = [], set()
        executor = view.executors[request.target_role]
        for check in request.checks:
            code = None
            try:
                args = executor.validate_args(check.tool, check.args)
                signature = (check.tool, args.model_dump_json())
                candidate = check.model_copy(update={'args': args.model_dump(mode='json')})
                if signature in signatures or not adds_information([candidate.model_dump(mode='json')], executor):
                    code = 'DUPLICATE_TOOL'
                signatures.add(signature)
            except ExecutionError as exc:
                code = exc.code
            if code:
                audit.hint_status.append({'tool': check.tool, 'code': code})
                emit('review_hint_ignored', 'reviewer', code=code, name=check.tool)
            else:
                normalized.append(candidate)
        request = request.model_copy(update={'checks': normalized})
        reopened = []
        for assessment in need_assessments:
            if assessment.need_id == request.need_id and assessment.verdict != 'uncertain':
                emit('review_need_reopened', 'reviewer', need_id=assessment.need_id,
                           target_id=disputed, previous_verdict=assessment.verdict,
                           verdict='uncertain', code='EVIDENCE_GAP_PENDING')
                assessment = assessment.model_copy(update={'verdict': 'uncertain',
                    'reason': ('复核提出证据缺口，此问题暂缓关闭：' + request.missing_observation)[:400]})
            reopened.append(assessment)
        need_assessments = reopened
    normalized = review.model_copy(update={'assessments': checked, 'need_assessments': need_assessments,
                                           'request_evidence': request})
    try:
        normalized = validate_blockers(view.needs, normalized, view.executors,
            required=view.require_blockers, targets=targets,
            conservative=conservative(view.protection), event=emit)
    except ProtocolError as exc:
        if exc.code == 'REVIEW_BLOCKER_REQUIRED':
            for detail in exc.details:
                for index, original in enumerate(review.need_assessments):
                    if (original.need_id == detail['need_id'] and original.verdict == 'supported'
                            and not current_support(original.evidence_ids, view.evidence)):
                        detail['support_gate'] = {'submitted_verdict': 'supported', 'effective_verdict': 'uncertain',
                            'path': f'need_assessments.{index}.evidence_ids',
                            'reason': 'NO_CURRENT_OBSERVATION_REFERENCE',
                            'instruction': '原supported没有当前观测引用，已被降级uncertain。真有相关观测才补引用；否则保留未知并给blocker或同N请求，不机械补不相关EV。'}
        raise
    if conservative(view.protection):
        for assessment in normalized.need_assessments:
            if assessment.blocker and not is_blocking(view.needs[assessment.need_id].model_copy(
                    update={} if assessment.blocking is None else {'blocking': assessment.blocking})):
                gap = '复核保留范围限制：' + assessment.blocker.missing_observation
                if gap not in view.existing_gaps and gap not in audit.limitations:
                    audit.limitations.append(gap)
    handoff = blocker_follow_up(view.needs, normalized, view.executors)
    if handoff:
        emit('review_blocker_handoff', 'reviewer', need_id=handoff.need_id,
                   target_id=handoff.target_id, planning_required=True)
        normalized = normalized.model_copy(update={'request_evidence': handoff})
        # A legacy blocker-derived request must pass the same ownership gate.
        return validate_review(normalized, view, audit=audit, emit=emit)
    return normalized


def validate_observation_review(value, view: ReviewContext):
    claims = []
    for claim in value.directly_supported:
        refs = expand_refs(claim.refs, view)
        if any(view.evidence[ref.evidence_id].kind != 'observation' for ref in refs):
            raise ProtocolError('REVIEW_CURRENT_EVIDENCE_REQUIRED')
        claims.append(claim.model_copy(update={'statement': source_statement(refs, view.evidence)}))
    return value.model_copy(update={'directly_supported': claims})


def additional_rework_eligible(view: ReviewRoutingContext) -> bool:
    if view.reworks != 1 or view.purpose != 'diagnosis' or view.protocol_fallback_used or not view.review:
        return False
    request = view.review.request_evidence
    if not request or not request.need_id or not request.target_id:
        return False
    claim = view.targets.get(request.target_id)
    if claim is None or (isinstance(claim, Hypothesis) and claim.level not in {'mechanism', 'alternative'}):
        return False
    target = next((a for a in view.review.assessments if a.target_id == request.target_id), None)
    assessment = next((a for a in view.review.need_assessments if a.need_id == request.need_id), None)
    if target is None or target.verdict != 'uncertain' or assessment is None or assessment.verdict != 'uncertain':
        return False
    blocker = assessment.blocker
    return bool(blocker and request.target_id in blocker.affected_targets
                and blocker.cause_level in {'observation', 'mechanism', 'alternative', 'action_condition'}
                and blocker.resolution != 'external_source' and blocker.decision_impact.strip()
                and is_blocking(view.needs[request.need_id].model_copy(update={'blocking': assessment.blocking})))


def needs_review_follow_up(view: ReviewRoutingContext, *, can_collect: Callable[[], bool]) -> bool:
    review = view.review
    if review is None:
        return False
    return (not view.protocol_fallback_used and view.purpose == 'diagnosis' and not view.clarification_used
        and not view.reworks and not review.request_evidence and not review.follow_up_reason
        and ((any(a.verdict == 'uncertain' and a.target_id.startswith(('H', 'A')) for a in review.assessments)
              and readiness_gaps(view.draft, review, view.purpose))
             or any(a.verdict == 'uncertain' and is_blocking(view.needs[a.need_id].model_copy(
                 update={} if a.blocking is None else {'blocking': a.blocking})) for a in review.need_assessments))
        and can_collect())


def resolve_handoff(view: ReviewRoutingContext, *, reviewed_gaps: Callable[[], Sequence[str]]) -> ReviewHandoff:
    request = view.review.request_evidence if view.review else None
    if (request and view.purpose == 'diagnosis' and not reviewed_gaps()
            and nonblocking_trigger_request(view.draft, view.review, view.needs,
                conservative=conservative(view.protection))):
        reason = '机制和有效下一步已复核；纯触发未知作为人工待核实限制保留。'
        limitation = ('已支持核查待办的结果待人工取得：' if
            (request.target_id or '').startswith('A') else '非关键触发原因待人工核实：') + request.missing_observation
        return ReviewHandoff(None, request, reason, limitation)
    return ReviewHandoff(request)


def decide_rework(view: ReviewRoutingContext, *, can_collect: Callable[[bool], bool]) -> ReworkDecision:
    if view.protocol_fallback_used:
        return ReworkDecision('protocol_stop')
    if view.reworks >= 2 or (view.reworks == 1 and not additional_rework_eligible(view)):
        return ReworkDecision('limit_stop')
    additional = view.reworks == 1
    if not can_collect(additional):
        return ReworkDecision('budget_stop')
    return ReworkDecision('plan', authorize_additional=additional)


def completion_reason(draft, review, purpose, *, diagnosis_gaps: Sequence[str],
                      observation_gaps: Sequence[str], task_gaps: Sequence[str],
                      blocking_gaps: Sequence[str]) -> str:
    if diagnosis_gaps:
        return 'DIAGNOSIS_INCOMPLETE'
    if readiness_gaps(draft, review, purpose) or observation_gaps or task_gaps or blocking_gaps:
        return 'REVIEW_INCOMPLETE'
    return 'STATUS_CHECK_COMPLETED' if purpose == 'status_check' else 'FINISHED'
