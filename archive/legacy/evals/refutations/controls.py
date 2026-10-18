"""Declared classifications test program behavior, not model semantic accuracy."""
from datetime import datetime, timezone
from agents.contracts import Assessment, ReviewDecision, RefutationBasis
from evidence.contracts import Evidence, IncidentScope
from workflow.review_protection import protect_refutations


def run_controls():
    now = datetime(2026, 10, 13, 9, 10, tzinfo=timezone.utc)
    scope = IncidentScope(tenant_id='synthetic_demo', user_id='cli_reader', service='control-service',
        environment='staging', service_version='1', start=now.replace(minute=0), end=now.replace(minute=30))
    pairs = [
        ('探活成功不能排除业务超时', '业务请求存在超时', '探活请求成功，未覆盖业务请求', 'weakens_explanation', 'uncertain'),
        ('同次探活结果可否定探活失败', '这一次探活请求失败', '这一次探活返回成功', 'direct_contradiction', 'not_supported'),
        ('低处理器使用率不能排除下游阻塞', '下游请求可能阻塞', '处理器使用率为百分之三十', 'weakens_explanation', 'uncertain'),
        ('同一样本可否定高处理器使用率', '该样本处理器使用率超过百分之八十', '该样本处理器使用率为百分之三十', 'direct_contradiction', 'not_supported'),
        ('缺少记录不能否定远端变化', '远端配置曾变化', '本地查询没有远端变更记录', 'absence_of_support', 'uncertain'),
        ('同次请求失败可否定请求成功', '这一次业务请求成功', '这一次业务请求返回拒绝状态', 'direct_contradiction', 'not_supported'),
    ]
    rows = []
    for title, claim, observation, kind, expected in pairs:
        evidence = Evidence(evidence_id='EV1', kind='observation', provider='declared_synthetic_control',
            service=scope.service, environment=scope.environment, observed_from=now, observed_to=now,
            retrieved_at=now, payload={'message': observation}, excerpt=observation,
            locator='synthetic-control', data_version='control-v1', hash='synthetic')
        review = ReviewDecision(assessments=[Assessment(target_id='H1', verdict='not_supported',
            reason='显式构造的分类输入，未运行模型语义判断', evidence_ids=['EV1'],
            refutation_basis=RefutationBasis(kind=kind, claim=claim))])
        outcomes = {}
        for bounded in (False, True):
            events = []
            def event(name, role, **data):
                events.append({'type': name, 'role': role, **data})
            accepted = protect_refutations(review, {'EV1': evidence}, event, bounded=bounded, scope=scope)
            outcomes['bounded' if bounded else 'baseline'] = {
                'verdict': accepted.assessments[0].verdict, 'events': events}
        assert outcomes['bounded']['verdict'] == expected
        rows.append({'title': title, 'claim': claim, 'observation': observation,
            'declared_kind': kind, 'expected': expected, **outcomes})
    return {'method': 'declared_refutation_classification_controls_v1', 'rows': rows,
            'paid_model_calls': 0, 'tool_calls': 0,
            'semantic_accuracy_measured': False, 'limitations': [
                '反证类别由控制材料显式提供，不能证明模型会正确分类。',
                '直接矛盾保留不等于程序已证明语义冲突，需实际模型和人工审阅。']}
