"""Program-owned routing from validated review, material gaps and original quotas."""
from dataclasses import dataclass
from typing import Literal

from workflow.evidence_needs import is_blocking
from workflow.review import resolve_handoff, decide_rework


@dataclass(frozen=True)
class ReviewDisposition:
    route: Literal['publish', 'revise', 'collect', 'handoff', 'stop']
    reason: str
    authorize_additional: bool = False
    deferred_request: object = None
    limitation: str | None = None


def review_disposition(view, *, reviewed_gaps, can_collect, can_revise,
                       evidence_gaps=()):
    if view.protocol_fallback_used:
        return ReviewDisposition('stop', 'protocol_finalization_only')
    handoff = resolve_handoff(view, reviewed_gaps=reviewed_gaps)
    if handoff.deferred_request:
        return ReviewDisposition('publish', 'nonblocking_unknown',
                                deferred_request=handoff.deferred_request, limitation=handoff.limitation)
    request = handoff.request
    if request:
        external = any(item.blocker and item.blocker.resolution == 'external_source'
            and (item.need_id == request.need_id or request.target_id in item.blocker.affected_targets)
            for item in view.review.need_assessments)
        if external:
            return ReviewDisposition('handoff', 'external_information_required')
        decision = decide_rework(view, can_collect=can_collect)
        if decision.kind == 'plan':
            return ReviewDisposition('collect', 'critical_evidence_gap', decision.authorize_additional)
        return ReviewDisposition('handoff', 'rework_limit' if decision.kind == 'limit_stop' else 'collection_budget')
    blocking = any(item.verdict == 'uncertain' and is_blocking(view.needs[item.need_id].model_copy(
        update={} if item.blocking is None else {'blocking': item.blocking}))
        for item in view.review.need_assessments)
    if blocking or evidence_gaps:
        return ReviewDisposition('handoff', 'unresolved_evidence_gap')
    if getattr(view.review, 'revision_requests', ()):
        return ReviewDisposition('revise', 'existing_evidence_correction') if can_revise() else (
            ReviewDisposition('handoff', 'revision_limit_or_budget'))
    if reviewed_gaps():
        return ReviewDisposition('handoff', 'unresolved_review')
    return ReviewDisposition('publish', 'review_complete')
