"""Deterministic report readiness; semantic verdicts remain the reviewer's duty."""


def review_policy():
    return {
        'version': 'diagnosis_readiness_v2',
        'cause_levels': ['mechanism', 'trigger', 'alternative'],
        'action_kinds': ['read_only_check', 'manual_check', 'manual_change', 'registered_repair'],
        'completion': '复核观测、至少一个有支持的机制及一个有效下一步；排除候选原因是调查结果。',
        'limitations': '触发原因未知可保留；会改变机制或下一步的关键歧义仍阻塞。',
        'blockers': '阻塞未知须指明受影响F/H/A、缺失观测及改变判断的结果；N2不包含深层触发原因。查询状态由程序台账确认，不自动证明缺口必要性。',
        'read_only_advice': '核查建议须有依据、范围合法且增加新覆盖；复用已有结果，不要求预先取得新查询结果。',
        'changes': '支持有条件建议不等于条件已满足、获批或执行。未支持的修改只展示为待确认项。',
    }


def readiness_gaps(draft, review, purpose):
    verdicts = {a.target_id: a.verdict for a in review.assessments}
    gaps = []
    if not any(verdicts.get(f'F{n}') == 'supported' for n, _ in enumerate(draft.findings, 1)):
        gaps.append('尚无经过复核的当前观测。')
    if any(verdicts.get(f'F{n}') != 'supported' for n, _ in enumerate(draft.findings, 1)):
        gaps.append('仍有观测陈述未通过复核。')
    if purpose == 'status_check':
        return gaps
    supported_mechanism = any(h.level in {'mechanism', 'unspecified'}
        and verdicts.get(h.hypothesis_id) == 'supported' for h in draft.hypotheses)
    if not supported_mechanism:
        gaps.append('尚无经过复核的故障机制；触发原因或被排除的候选不能代替机制。')
    if any(h.level in {'mechanism', 'unspecified'} and verdicts.get(h.hypothesis_id) == 'uncertain'
           for h in draft.hypotheses):
        gaps.append('仍有影响当前判断的候选机制待核实。')
    if not any(verdicts.get(f'A{n}') == 'supported' for n, _ in enumerate(draft.recommended_actions, 1)):
        gaps.append('尚无经过复核的有效下一步；可以是有依据的只读核查或有条件的处理建议。')
    return gaps


def nonblocking_trigger_request(draft, review, needs, *, conservative=False):
    """A target-only trigger gap is optional only after independent readiness."""
    request = review.request_evidence
    if request is None:
        return False
    target_id = request.target_id or request.hypothesis_id
    target = next((h for h in draft.hypotheses if h.hypothesis_id == target_id), None)
    verdicts = {a.target_id: a.verdict for a in review.assessments}
    action = next((a for n, a in enumerate(draft.recommended_actions, 1) if f'A{n}' == target_id), None)
    future_check = (conservative and action is not None and action.kind in {'manual_check', 'read_only_check'}
                    and verdicts.get(target_id) == 'supported')
    if not future_check and (target is None or target.level != 'trigger' or verdicts.get(target_id) != 'uncertain'):
        return False
    if request.need_id is not None and needs[request.need_id].purpose != 'trigger':
        if not conservative or is_material_need(needs[request.need_id]):
            return False
    if any(
            key.startswith('A') and value == 'uncertain' for key, value in verdicts.items()):
        return False
    # A gap linked to a mechanism or an action condition is not a pure trigger
    # limitation, even if the request itself names a trigger hypothesis.
    for assessment in review.need_assessments:
        if conservative and not is_material_need(needs[assessment.need_id]):
            continue
        if assessment.blocker and any(
                not key.startswith('H') or next((h.level for h in draft.hypotheses
                    if h.hypothesis_id == key), None) != 'trigger'
                for key in assessment.blocker.affected_targets):
            return False
    return True


def is_material_need(need):
    from workflow.evidence_needs import is_blocking
    return need.status not in {'supported', 'not_required'} and is_blocking(need)


def action_projection(draft, review):
    verdicts = {a.target_id: a for a in review.assessments} if review else {}
    approved, pending = [], []
    for n, action in enumerate(draft.recommended_actions, 1):
        assessment = verdicts.get(f'A{n}')
        if assessment and assessment.verdict == 'supported':
            approved.append(action.model_dump(mode='json'))
        elif action.kind != 'unspecified' and (assessment is None or assessment.verdict == 'uncertain'):
            # Never publish an unreviewed executable repair candidate in the actions list.
            pending.append({**action.model_dump(mode='json'), 'repair': None,
                'target_id': f'A{n}', 'review_verdict': 'uncertain' if assessment else 'not_performed',
                'review_reason': assessment.reason if assessment else '尚未复核。', 'executable': False})
    return approved, pending
