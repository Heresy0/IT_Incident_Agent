"""Deterministic human handoff from published results and actual read ledgers."""
from runtime.context import MESSAGES


REASONS = {'external_information_required': '需要获取当前授权范围之外的信息',
           'unresolved_evidence_gap': '关键证据缺口尚未解决', 'unresolved_review': '复核仍有未解决问题',
           'revision_limit_or_budget': '已达到修稿次数上限或没有足够修稿额度',
           'collection_budget': '没有足够额度完成补查和后续复核', 'rework_limit': '已达到补查次数上限',
           'nonblocking_unknown': '非关键未知保留为人工待核实项',
           'no_current_observations': '没有取得可用的当前观测', 'embedding_budget': '检索额度不足',
           'protocol_finalization_only': '模型协议失败后仅保留有效结果收尾'}


def build_handoff(*, run_id, revision, reason, publication, needs, evidence, executors, review):
    owners = [{'team': item.payload['team'], 'evidence_id': item.evidence_id}
              for item in evidence.values() if item.payload.get('team')]
    unresolved = []
    for need in needs.values():
        if need.status not in {'supported', 'not_required'}:
            assessment = next((item for item in review.need_assessments if item.need_id == need.need_id), None) if review else None
            blocker = assessment.blocker if assessment else None
            unresolved.append({'need_id': need.need_id, 'question': need.question,
                'missing_observation': blocker.missing_observation if blocker else need.reason,
                'decision_impact': blocker.decision_impact if blocker else None})
    request = review.request_evidence if review else None
    steps = [item['missing_observation'] for item in unresolved]
    if request:
        steps.append(request.missing_observation)
    if review and review.follow_up_reason:
        steps.append(review.follow_up_reason)
    steps.extend(item.required_change for item in getattr(review, 'revision_requests', ()))
    checks = [{'role': role, 'tool': row['tool'], 'status': row['status'],
               'truncated': row['truncated'], 'evidence_ids': list(row['evidence_ids']),
               'error_code': row.get('error')}
              for role, executor in executors.items() for row in executor.check_results.values()]
    return {'run_id': run_id, 'draft_revision': revision, 'reason': reason,
            'reason_label': REASONS.get(reason, MESSAGES.get(reason, '本次自动诊断仍有未解决限制')),
            'blocking': publication.report_status == 'incomplete',
            'supported_findings': publication.findings,
            'supported_hypotheses': [item for item in publication.hypotheses if item['status'] == 'supported'],
            'reviewed_actions': publication.actions, 'pending_actions': publication.pending_actions,
            'unresolved_questions': unresolved, 'limitations': list(publication.missing_information),
            'attempted_checks': checks, 'next_steps': list(dict.fromkeys(steps)),
            'owners': owners, 'owner_resolved': bool(owners),
            'operation_boundary': '核实未知信息；任何变更仍需独立审批与执行确认，不自动关闭工单。'}
