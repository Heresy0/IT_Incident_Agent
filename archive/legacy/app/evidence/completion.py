"""Shared content completeness, independent of references, semantics and approval."""


def required_sections(purpose):
    return ['findings'] if purpose == 'status_check' else ['findings', 'hypotheses', 'recommended_actions']


def diagnosis_gaps(draft, purpose):
    messages = {
        'findings': '故障诊断缺少可复核的当前观测。',
        'hypotheses': '故障诊断尚未形成有当前证据支持的原因假设；仅复核观测不能完成诊断。',
        'recommended_actions': '故障诊断尚未形成处理建议；需要说明适用条件、预期验证、风险和人工审批要求。',
    }
    if purpose == 'status_check':
        messages['findings'] = '状态核查缺少可复核的当前观测。'
    return [messages[field] for field in required_sections(purpose)
            if not (draft.get(field) if isinstance(draft, dict) else getattr(draft, field))]
