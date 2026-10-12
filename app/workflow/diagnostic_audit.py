"""Separate review claims, actual query coverage and execution termination.

These checks validate accounting and internal consistency, not causal truth.
"""
from evidence.diagnosis import ProtocolError
from runtime.context import ExecutionError
from workflow.evidence_needs import is_blocking
from workflow.task_checks import completion
from workflow.task_checks import adds_information
from agents.contracts import EvidenceRequest

VERSION = 'diagnostic_audit_v1'


def validate_blockers(needs, review, executors, *, required=False, targets=None):
    verdicts = {a.target_id: a.verdict for a in review.assessments}
    for assessment in review.need_assessments:
        need = needs[assessment.need_id]
        blocking = is_blocking(need.model_copy(update={} if assessment.blocking is None
                                               else {'blocking': assessment.blocking}))
        if assessment.verdict != 'uncertain' or not blocking:
            continue
        blocker = assessment.blocker
        requested = review.request_evidence and review.request_evidence.need_id == need.need_id
        if blocker is None:
            if required and not requested:
                raise ProtocolError('REVIEW_BLOCKER_REQUIRED', [{'need_id': need.need_id,
                    'path': 'need_assessments.blocker',
                    'instruction': '阻塞未知须给cause_level、affected_targets、missing_observation、decision_impact、resolution；不可为通过而编造支持。'}])
            continue
        if len(set(blocker.affected_targets)) != len(blocker.affected_targets) or not set(blocker.affected_targets) <= set(verdicts):
            raise ProtocolError('REVIEW_BLOCKER_TARGET', [{'need_id': need.need_id, 'allowed': list(verdicts)}])
        trigger_only = targets is not None and all(
            key.startswith('H') and targets[key].level == 'trigger'
            or key.startswith('A') and verdicts[key] == 'supported'
            for key in blocker.affected_targets)
        if need.purpose in {'mechanism', 'alternative'} and (blocker.cause_level == 'trigger' or trigger_only):
            raise ProtocolError('REVIEW_BLOCKER_SCOPE', [{'need_id': need.need_id,
                'path': 'need_assessments.blocker.affected_targets',
                'instruction': '此问题须评估当前机制或会改变下一步的替代解释；纯trigger未知与已支持待办的未来结果不能阻塞该问题。重新独立评估此N，触发细节保留missing_information；真正影响行动条件时关联未支持A。不得自动提升结论。'}])
        if all(verdicts[target] == 'supported' for target in blocker.affected_targets):
            raise ProtocolError('REVIEW_BLOCKER_CONFLICT', [{'need_id': need.need_id,
                'instruction': '声称缺口影响的目标全部supported，与阻塞矛盾。独立复核目标或问题，不得自动提升任一结论。'}])
        if blocker.resolution == 'scoped_check':
            if not blocker.checks:
                raise ProtocolError('REVIEW_BLOCKER_CHECK_REQUIRED', [{'need_id': need.need_id,
                    'path': 'blocker.checks',
                    'instruction': 'scoped_check须提供至少一项合法只读check及精确args；外部服务信息用external_source，未规划用not_planned。'}])
            for check in blocker.checks:
                try:
                    executors[blocker.target_role].validate_args(check.tool, check.args)
                except ExecutionError as exc:
                    raise ProtocolError('INVALID_BLOCKER_CHECK', [{'need_id': need.need_id, 'code': exc.code}]) from None
        elif blocker.checks:
            raise ProtocolError('REVIEW_BLOCKER_CHANNEL_CONFLICT')
    return review


def blocker_follow_up(needs, review, executors):
    """Route a validated, useful blocker to planning; never execute its hint."""
    if review.request_evidence:
        return None
    verdicts = {a.target_id: a.verdict for a in review.assessments}
    for assessment in review.need_assessments:
        blocker = assessment.blocker
        if assessment.verdict != 'uncertain' or not blocker or blocker.resolution != 'scoped_check':
            continue
        need = needs[assessment.need_id]
        if not is_blocking(need.model_copy(update={} if assessment.blocking is None
                                         else {'blocking': assessment.blocking})):
            continue
        ex = executors[blocker.target_role]
        checks = [check for check in blocker.checks if adds_information([check.model_dump(mode='json')], ex)]
        if not checks:
            continue
        target = next((key for key in blocker.affected_targets
                       if key.startswith(('H', 'A')) and verdicts[key] != 'supported'), None)
        return EvidenceRequest(need_id=need.need_id, target_id=target,
            hypothesis_id=target if target and target.startswith('H') else None,
            evidence_ids=assessment.evidence_ids, missing_observation=blocker.missing_observation,
            expected_value=blocker.decision_impact, target_role=blocker.target_role, checks=checks)
    return None


def diagnostic_audit(needs, review, draft, executors, tasks, *, stop, events):
    verdicts = {a.target_id: a.verdict for a in review.assessments} if review else {}
    assessments = {a.need_id: a for a in review.need_assessments} if review else {}
    checks = []
    for task in tasks:
        coverage = completion(task.get('required_checks', []), executors[task['role']])
        checks.extend({**row, 'role': task['role'], 'need_ids': task.get('need_ids', []),
                       'goal': task['goal'], 'expected_value': task.get('expected_value')}
                      for row in coverage['checks'])
    issues = []
    for event in events:
        if event.get('type') == 'validation_failure':
            code = event.get('reason', '')
            if code.startswith('REVIEW_BLOCKER_'):
                issues.append({'code': code, 'model_call_index': event.get('model_call_index')})
    gaps = []
    for need in needs.values():
        if need.status in {'supported', 'not_required'} and need.need_id in assessments:
            continue
        assessment = assessments.get(need.need_id)
        blocker = assessment.blocker if assessment else None
        query_checks = []
        if blocker and blocker.checks:
            query_checks = completion([c.model_dump(mode='json') for c in blocker.checks],
                                      executors[blocker.target_role])['checks']
        elif need.next_check:
            query_checks = completion([need.next_check.model_dump(mode='json')], executors[need.role])['checks']
        else:
            query_checks = [row for row in checks if need.need_id in row['need_ids']]
        effective = is_blocking(need)
        if not effective:
            category = 'nonblocking_limitation'
        elif any(c['status'] == 'not_executed' for c in query_checks):
            category = 'investigation_incomplete'
        elif any(c['status'] == 'failed' for c in query_checks):
            category = 'retrieval_failed'
        elif any(c['status'] == 'truncated' for c in query_checks):
            category = 'incomplete_sample'
        elif not review:
            category = 'review_not_completed'
        elif blocker and blocker.resolution == 'external_source':
            category = 'external_input_required'
        elif any(c['status'] in {'empty', 'missing_samples'} for c in query_checks):
            category = 'selected_scope_insufficient'
        else:
            category = 'unresolved_review' if blocker else 'unexplained_blocker'
        gaps.append({'need_id': need.need_id, 'purpose': need.purpose, 'blocking': effective,
            'category': category, 'query_checks': query_checks,
            'blocker': blocker.model_dump(mode='json') if blocker else None,
            'reason': need.reason, 'semantic_necessity_verified_by_program': False,
            'category_basis': 'scoped_external_source_claim' if category == 'external_input_required' else
                'query_ledger' if category in {'investigation_incomplete', 'retrieval_failed',
                    'incomplete_sample', 'selected_scope_insufficient'} else 'review_state'})
    mechanism = bool(draft and any(h.level in {'mechanism', 'unspecified'}
        and verdicts.get(h.hypothesis_id) == 'supported' for h in draft.hypotheses))
    next_step = bool(draft and any(verdicts.get(f'A{n}') == 'supported'
        for n, _ in enumerate(draft.recommended_actions, 1)))
    suspicious = [row['need_id'] for row in gaps if row['purpose'] == 'mechanism' and row['blocking']
        and mechanism and next_step and (not row['blocker'] or row['blocker']['cause_level'] == 'trigger')]
    origin = ('none' if stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} else
        'budget_or_workflow_limit' if stop in {'BUDGET_EXCEEDED', 'TIME_BUDGET_EXCEEDED',
            'SUPERVISOR_LIMIT', 'REWORK_LIMIT', 'INPUT_BUDGET_EXCEEDED'} else
        'protocol_or_execution_failure' if stop in {'MODEL_OUTPUT_INVALID', 'MODEL_ERROR', 'AUTH_ERROR',
            'SCOPE_DENIED', 'TOOL_DENIED', 'NETWORK_ERROR', 'TIMEOUT', 'INTERNAL_ERROR'} else
        'unfinished_investigation' if any(g['category'] == 'investigation_incomplete' for g in gaps) else
        'review_or_evidence_limitations')
    return {'version': VERSION, 'partial_origin': origin,
        'mechanism_status': 'supported' if mechanism else 'unconfirmed',
        'next_step_status': 'supported' if next_step else 'unconfirmed',
        'investigation_status': 'not_declared' if not checks else
            'incomplete' if any(not row['query_complete'] or row['status'] == 'missing_samples' for row in checks)
            else 'declared_queries_completed',
        'query_ledger': checks, 'gaps': gaps, 'review_consistency_issues': issues,
        'overblocking_suspected_need_ids': suspicious,
        'termination_reason': stop,
        'meaning': '分类依据查询台账和复核契约；查空、预算停止不证明渠道缺失。因果正确性与缺口必要性仍需语义评估；不自动更改完成状态。'}
