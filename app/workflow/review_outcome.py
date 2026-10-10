"""Deterministic report readiness; semantic verdicts remain the reviewer's duty."""


def review_policy():
    return {
        'version': 'diagnosis_readiness_v2',
        'cause_levels': ['mechanism', 'trigger', 'alternative'],
        'action_kinds': ['read_only_check', 'manual_change', 'registered_repair'],
        'completion': '复核观测、至少一个有支持的机制及一个有效下一步；排除候选原因是调查结果。',
        'limitations': '触发原因未知可保留；会改变机制或下一步的关键歧义仍阻塞。',
        'read_only_advice': '核查建议只需有依据、范围合法，不要求预先取得它将要查询的结果。',
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
