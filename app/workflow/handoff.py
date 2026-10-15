"""Deterministic human handoff from published results and actual read ledgers."""
from runtime.context import MESSAGES
from workflow.collection_recovery import CHANNEL_FAILURES, POLICY


REASONS = {'external_information_required': '需要获取当前授权范围之外的信息',
           'source_recovery_required': '观测渠道已失败，需要人工恢复渠道或取得替代观测',
           'unresolved_evidence_gap': '关键证据缺口尚未解决', 'unresolved_review': '复核仍有未解决问题',
           'revision_limit_or_budget': '已达到修稿次数上限或没有足够修稿额度',
           'collection_budget': '没有足够额度完成补查和后续复核', 'rework_limit': '已达到补查次数上限',
           'nonblocking_unknown': '非关键未知保留为人工待核实项',
           'no_current_observations': '没有取得可用的当前观测', 'embedding_budget': '检索额度不足',
           'protocol_finalization_only': '模型协议失败后仅保留有效结果收尾'}


def recovery_tasks(*, checks, unresolved, owners, service, environment, reviewed_actions):
    tasks = []
    owner = owners[0]['team'] if owners else None
    seen = set()
    for check in checks:
        if check['status'] != 'error' or check['error_code'] not in CHANNEL_FAILURES or check['tool'] in seen:
            continue
        seen.add(check['tool'])
        tasks.append({'kind': 'manual_check', 'source': 'program_query_ledger', 'model_reviewed': False,
            'action': f"由人工核查{service}/{environment}的{check['tool']}观测渠道：确认最近成功查询、渠道可用性和登记接入状态，记录本次{check['error_code']}，必要时联系经确认的观测负责人。",
            'condition': '本轮该观测渠道查询失败；不能以相同自动查询或更换过滤代替渠道恢复。',
            'requested_observation': {'tool': check['tool'], 'query_profile': check['query_profile']},
            'completion_criteria': '提供渠道失败/恢复核查记录；若获准恢复渠道，在新诊断中取得原范围观测；仍不可用时提供带对象、时间和来源的人工替代观测，继续保留未知。',
            'owner': owner, 'owner_resolved': owner is not None,
            'risk': '本任务仅核查并取证；任何凭据、接入配置或服务变更须另行审批。',
            'requires_approval': False, 'changes_require_separate_approval': True})
    if unresolved and not tasks and not reviewed_actions:
        missing = unresolved[0]['missing_observation']
        tasks.append({'kind': 'manual_check', 'source': 'program_unresolved_need', 'model_reviewed': False,
            'action': f'人工确认{service}/{environment}的维护负责人，按授权范围取得待核实观测：{missing}',
            'condition': '自动诊断未取得关键观测；未确认具体负责人，不能假定某团队已接单。',
            'requested_observation': {'need_id': unresolved[0]['need_id'], 'description': missing},
            'completion_criteria': '提供记录的对象、时间、来源和实际结果，再对照受影响判断复核；没有记录时继续保留不确定，不能声称修复完成。',
            'owner': owner, 'owner_resolved': owner is not None, 'risk': '只读核实；任何变更另行审批。',
            'requires_approval': False, 'changes_require_separate_approval': True})
    return [{'task_id': f'HC{index + 1}', **task} for index, task in enumerate(tasks)]


def build_handoff(*, run_id, revision, reason, publication, needs, evidence, executors, review, scope):
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
               'query_profile': row['args'],
               'error_code': row.get('error')}
              for role, executor in executors.items() for row in executor.check_results.values()]
    recovery = recovery_tasks(checks=checks, unresolved=unresolved, owners=owners,
        service=scope.service, environment=scope.environment, reviewed_actions=publication.actions)
    steps = [*[task['action'] for task in recovery], *steps]
    return {'run_id': run_id, 'draft_revision': revision, 'reason': reason,
            'reason_label': REASONS.get(reason, MESSAGES.get(reason, '本次自动诊断仍有未解决限制')),
            'blocking': publication.report_status == 'incomplete',
            'supported_findings': publication.findings,
            'supported_hypotheses': [item for item in publication.hypotheses if item['status'] == 'supported'],
            'reviewed_actions': publication.actions, 'pending_actions': publication.pending_actions,
            'unresolved_questions': unresolved, 'limitations': list(publication.missing_information),
            'attempted_checks': checks, 'next_steps': list(dict.fromkeys(steps)),
            'owners': owners, 'owner_resolved': bool(owners),
            'recovery_policy': POLICY, 'recovery_tasks': recovery,
            'recovery_tasks_meaning': '程序依据实际失败台账或未解问题生成的人工核查待办，未获得模型复核或变更授权，不改变诊断完成状态。',
            'operation_boundary': '核实未知信息；任何变更仍需独立审批与执行确认，不自动关闭工单。'}
