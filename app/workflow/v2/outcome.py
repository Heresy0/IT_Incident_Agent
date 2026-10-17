"""Separate execution, item review and bounded investigation outcomes.

This projection never infers causality from text. A mechanism is available only
when the existing typed claim and current model review say so. Collection stop
codes describe a phase boundary; they are not a substitute for the final cause
of an incomplete report.
"""
from collections import Counter

from workflow.v2.review import evidence_pack_version, review_matches


_HARD_STOPS = {
    'AUTH_ERROR', 'QUOTA_EXCEEDED', 'PROVIDER_AUTH_MISSING', 'BUDGET_EXCEEDED',
    'SCOPE_DENIED', 'TOOL_DENIED', 'WINDOW_DENIED', 'TIME_BUDGET_EXCEEDED',
    'TIMEOUT', 'MODEL_OUTPUT_INVALID', 'OUTPUT_TRUNCATED',
}


def _error_code(error):
    if isinstance(error, dict):
        return error.get('code')
    return getattr(error, 'code', None)


def classify_outcome(draft, reviews, executor, failures, collection_stop, ticket, *, collection=None):
    """Return compatible result fields plus an additive ``run_outcome``.

    ``failures`` must contain the publisher's normalized execution/review errors,
    including its recovery flags. No object, budget or review is mutated. Empty
    advice is legal; an accepted manual check cannot replace a supported typed
    mechanism. Known unknowns remain explicit even when a bounded report can be
    published successfully.
    """
    targets = {target.id: target for target in [*draft.claims, *draft.next_steps]}
    packs = {evidence_pack_version(executor)}
    if collection is not None:
        packs.add(evidence_pack_version(executor, collection=collection))
    counts = Counter(item.target_id for item in reviews.items)
    current = {item.target_id: item for item in reviews.items
               if item.target_id in targets and counts[item.target_id] == 1
               and reviews.evidence_pack_version in packs
               and review_matches(item, targets[item.target_id], reviews.evidence_pack_version)}
    valid = {key: item for key, item in current.items() if item.protocol_status == 'valid'}
    accepted_claims = {claim.id for claim in draft.claims if claim.id in valid
                       and valid[claim.id].relation in {'supported', 'contradicted'}}
    accepted_checks = {check.id for check in draft.next_steps if check.id in valid
                       and valid[check.id].relation == 'supported'
                       and valid[check.id].submitted_decision in {None, 'appropriate'}}
    assessed_ids = accepted_claims | accepted_checks
    all_reviewed = bool(targets) and set(targets) == assessed_ids
    review_status = ('passed' if all_reviewed else 'needs_information' if valid else 'not_performed')
    mechanisms = [claim.id for claim in draft.claims if claim.level == 'mechanism'
                  and claim.id in valid and valid[claim.id].relation == 'supported']
    unresolved = [error for error in failures if not error.get('recovered')]
    failure_codes = list(dict.fromkeys(str(error.get('code', 'INVALID_ITEM')) for error in unresolved))
    # A genuinely aborted collection cannot be hidden by a caller omitting the
    # duplicate failure row. Ordinary collection/reservation boundaries are not
    # execution errors and never become the final reason by themselves.
    if collection_stop in _HARD_STOPS and collection_stop not in failure_codes:
        failure_codes.append(collection_stop)
    source_limits = []
    for row in executor.check_results.values():
        if row.get('status') == 'error' or row.get('truncated'):
            source_limits.append({'tool': row['tool'], 'status': row['status'],
                                  'truncated': bool(row.get('truncated')),
                                  'error_code': _error_code(row.get('error'))})
    facts_available = bool(draft.facts)
    status_check = ticket.purpose == 'status_check'
    blockers = []
    if failure_codes:
        blockers.append('EXECUTION_FAILURE')
    if source_limits:
        blockers.append('SOURCE_LIMITED')
    if not facts_available:
        blockers.append('NO_CURRENT_OBSERVATIONS')
    if not status_check:
        if not all_reviewed:
            blockers.append('REVIEW_INCOMPLETE')
        if not mechanisms:
            blockers.append('NO_SUPPORTED_MECHANISM')
    completed = not blockers
    if failure_codes:
        reason = failure_codes[0]
    elif source_limits:
        reason = 'SOURCE_UNAVAILABLE' if any(row['status'] == 'error' for row in source_limits) else 'SOURCE_TRUNCATED'
    elif not facts_available:
        reason = 'NO_CURRENT_OBSERVATIONS'
    elif not status_check and not all_reviewed:
        reason = 'REVIEW_NEEDS_INFORMATION' if valid else 'REVIEW_NOT_PERFORMED'
    elif not status_check and not mechanisms:
        reason = 'NO_SUPPORTED_MECHANISM'
    else:
        reason = 'STATUS_CHECK_COMPLETED' if status_check else 'FINISHED'
    if completed:
        investigation_status = 'limited' if draft.gaps else 'status_checked' if status_check else 'bounded_diagnosis'
    elif source_limits or failure_codes:
        investigation_status = 'handoff'
    elif mechanisms:
        investigation_status = 'limited'
    else:
        investigation_status = 'unresolved'
    basis = ('observed_status' if status_check else 'model_reviewed_bounded_mechanism') if completed else None
    return {
        'status': 'completed' if completed else 'partial',
        'business_result': ('status_checked' if status_check else 'diagnosis_available') if completed else 'needs_information',
        'review_status': review_status,
        'reason': reason,
        'diagnosis_readiness': {
            'report_status': 'reviewed_with_limitations' if completed and draft.gaps else 'reviewed' if completed else 'incomplete',
            'meaning': '完成仅表示受限报告可交付；模型审阅不证明唯一根因、服务全面正常、问题已解决或具备修复资格。',
        },
        'run_outcome': {
            'execution': {'status': 'interrupted' if failure_codes else 'completed',
                          'collection_stop': collection_stop, 'failure_codes': failure_codes},
            'review': {'status': review_status, 'target_count': len(targets),
                       'valid_target_count': len(valid),
                       'unresolved_target_ids': sorted(set(targets) - assessed_ids)},
            'investigation': {'status': investigation_status,
                              'supported_mechanism_ids': mechanisms,
                              'known_unknowns': list(draft.gaps),
                              'source_limitations': source_limits, 'blockers': blockers},
            'completion_basis': basis,
            'human_verified': False,
            'causality_verified_by_program': False,
        },
    }
