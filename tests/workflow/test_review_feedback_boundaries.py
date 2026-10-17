"""Six free families for manual-check feedback and claim/citation boundaries.

Semantic verdicts below are explicitly scripted controls, not LLM accuracy.
Counterfactual observations live only in memory; frozen records are never edited.
"""
from copy import deepcopy
import json
import unittest

from api.auth import Principal
from agents.contracts import DiagnosisSelection, ObservationReview, ReviewDecision, TaskRequest
from agents.protocol import correction_instruction, reviewer_schema
from agents.scripted import selector
from evals.independent.provider import IndependentProvider
from runtime.context import Limits, activate
from tests.tools.test_incident_tools import P
from tests.workflow.test_multi_minimal_handoff import LocalRole, OutageProvider, closure_models
from tests.workflow.test_planned_default import planned_models
from tests.workflow.test_review_scope_contract import ask_review, dns_review
from workflow.coordinator import Collaboration, collaborate


class RecordingRole(LocalRole):
    def __init__(self, callback):
        super().__init__(callback)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(deepcopy(messages))
        return super().invoke(messages)


def future_only(value):
    a = next(a for a in value['assessments'] if a['target_id'] == 'A1')
    a.update(verdict='uncertain', uncertainty_basis='future_result_only', reason='尚未执行，结果未知')
    return value


def outage_models(*, owner_finding=False):
    models = closure_models()
    if owner_finding:
        base = models['diagnosis'].callback
        def diagnosis(payload, number):
            value = base(payload, number)
            owner = next(e for e in payload['evidence'] if 'team' in e['payload'])
            value['findings'] = [{'statement': '当前登记负责人', 'refs': [selector(owner, 'team')]}]
            value['escalation_ref'] = selector(owner, 'team')
            return value
        models['diagnosis'] = RecordingRole(diagnosis)
    return models


def hypothesis_case(*, timeout=False, metrics=False, baseline=True):
    principal = Principal('synthetic_demo', 'cli_reader')
    provider = IndependentProvider('eval_001', principal)
    if timeout:
        extra = deepcopy(provider._data['logs'][0])
        extra.update(id='local-timeout-control', error_code='REQUEST_TIMEOUT',
            message='合成控制：同一调用在解析重试后超时，elapsed_ms=3000。')
        provider._data['logs'].append(extra)
    if not baseline:
        provider._data['metrics'] = [row for row in provider._data['metrics'] if row['id'].endswith('-after')]
    c = Collaboration(planned_models(), provider, principal, limits=Limits(model_calls=16, tool_calls=8))
    window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
    checks = [{'tool': 'get_service_logs', 'args': dict(window, category='dependency')}]
    if metrics:
        checks.append({'tool': 'get_service_metrics', 'args': dict(window,
            metrics=['request_error_rate', 'upstream_connect_failures'], granularity='1m')})
    task = TaskRequest(role='investigation', goal='本地边界控制取证', checks=checks)
    objective = c.task_objective(task, False)
    result = c.collect_task(task, objective, c.executors['investigation'], c.context, c.events, 0)
    c.record_task(task, objective, result)
    return c


def selected(c, field, predicate=lambda _: True):
    return [key for key, ref in c.references.items() if ref.field_path == field
            and predicate(c.evidence[ref.evidence_id])]


def diagnose(c, cause, refs, explanation):
    value = {'findings': [{'statement': '当前日志观测', 'refs': [{'reference_id': refs[0]}]}],
        'hypotheses': [{'cause': cause, 'level': 'mechanism',
            'support_refs': [{'reference_id': ref} for ref in refs], 'evidence_explanation': explanation}]}
    model = c.models['diagnosis'] = RecordingRole(lambda *_: deepcopy(value))
    with activate(c.context):
        c.draft = c.ask('diagnosis', DiagnosisSelection, {'ticket': c.ticket.model_dump(mode='json'),
            'evidence': c.sources(), 'collection': c.collection()}, terminal=True, validate=c.diagnosis)
    return model


def review(c, *, scope='full', verdict='supported'):
    c.models['reviewer'] = RecordingRole(lambda *_: dns_review(c, scope=scope, verdict=verdict))
    checked = ask_review(c)
    c.review, c.stop = checked, 'FINISHED' if verdict == 'supported' else 'NEEDS_INFORMATION'
    return checked, c.result()


class ReviewFeedbackBoundaryTests(unittest.TestCase):
    def test_family_1_future_manual_task_can_publish_without_business_result(self):
        models, provider = outage_models(), OutageProvider()
        base = models['reviewer'].callback
        models['reviewer'] = RecordingRole(base)
        report = collaborate(models, provider, P)
        self.assertEqual((report['run_summary']['model_calls'], report['run_summary']['tool_calls']), (4, 3))
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(report['output']['recommended_actions'])
        self.assertEqual(report['output']['recommended_actions'][0]['kind'], 'manual_check')
        self.assertFalse(report['output']['hypotheses'])
        self.assertTrue(all(n['status'] == 'pending' for n in report['evidence_needs']
                            if n['purpose'] in {'symptom', 'mechanism'}))
        self.assertEqual(provider.calls.count('get_service_owner'), 1)
        self.assertEqual(len(models['reviewer'].messages), 1)

    def test_family_2_illegal_tuple_gets_targeted_feedback_and_original_schema_in_second_request(self):
        for owner_finding in (False, True):
            with self.subTest(owner_finding=owner_finding):
                models, provider = outage_models(owner_finding=owner_finding), OutageProvider()
                base = models['reviewer'].callback
                first = []
                def callback(payload, number):
                    value = base(payload, number)
                    if number == 1:
                        future_only(value)
                        first.append(deepcopy(value))
                    return value
                model = models['reviewer'] = RecordingRole(callback)
                report = collaborate(models, provider, P)
                self.assertEqual(len(model.messages), 2)
                hint = model.messages[1][-1].content
                for text in ('人工核查纠正', 'A1', '业务证据需求', 'partial', '不新增查询', '具体缺陷', '不得仅更换'):
                    self.assertIn(text, hint)
                self.assertNotIn('证据不足时评估为 uncertain', hint)
                self.assertNotIn('纠正引用时', hint)
                self.assertEqual(model.messages[0][:2], model.messages[1][:2])
                prior = json.loads(model.messages[1][2].content)
                self.assertEqual(prior, first[0])
                body = json.loads(model.messages[1][1].content)
                rules = body['output_schema']['$defs']['Assessment']['allOf']
                rule = next(r for r in rules if r['then'].get('required') == ['uncertainty_basis'])
                self.assertEqual(rule['if']['properties']['target_id']['enum'], ['A1'])
                self.assertEqual(rule['if']['properties']['verdict']['const'], 'uncertain')
                self.assertEqual(set(rule['then']['properties']['uncertainty_basis']['enum']),
                    {'relevance_gap', 'scope_or_safety_gap', 'assumed_outcome'})
                rebuilt = next(e for e in report['events'] if e['type'] == 'correction_input_rebuilt')
                self.assertEqual(rebuilt['previous_output_mode'], 'full')
                self.assertFalse(rebuilt['evidence_omitted'])
                self.assertFalse(rebuilt['schema_changed'])
                self.assertEqual(report['repair_counts']['reviewer'], 1)
                self.assertEqual((report['run_summary']['model_calls'], report['run_summary']['tool_calls']), (5, 3))
                self.assertTrue(report['output']['recommended_actions'])
                self.assertEqual(next(a for a in first[0]['assessments'] if a['target_id'] == 'A1')['verdict'], 'uncertain')

    def test_family_3_persistent_future_only_stops_without_promotion_or_extra_queries(self):
        models, provider = outage_models(owner_finding=True), OutageProvider()
        base = models['reviewer'].callback
        model = models['reviewer'] = RecordingRole(lambda payload, number: future_only(base(payload, number)))
        report = collaborate(models, provider, P)
        self.assertEqual(len(model.messages), 2)
        self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(report['repair_counts']['reviewer'], 1)
        self.assertEqual((report['run_summary']['model_calls'], report['run_summary']['tool_calls']), (5, 3))
        self.assertEqual(provider.calls.count('get_service_owner'), 1)
        self.assertEqual(report['reviews'], [])
        self.assertFalse(report['output']['recommended_actions'])
        self.assertTrue(report['pending_actions'])
        self.assertTrue(all(not a['executable'] for a in report['pending_actions']))
        self.assertEqual([e['reason'] for e in report['events'] if e['type'] == 'validation_failure'],
                         ['MANUAL_CHECK_FUTURE_RESULT_ONLY'] * 2)

    def test_family_3_actual_objections_after_correction_remain_uncertain(self):
        objections = {
            'relevance_gap': '人工核查与本工单无关的库存事项',
            'scope_or_safety_gap': '人工读取授权窗口之外的私有原始请求',
            'assumed_outcome': '人工核查服务已经恢复，并将恢复作为既定事实'}
        for basis, action in objections.items():
            with self.subTest(basis=basis):
                models, provider = outage_models(), OutageProvider()
                base_diagnosis, base_review = models['diagnosis'].callback, models['reviewer'].callback
                def diagnosis(payload, number):
                    value = base_diagnosis(payload, number)
                    value['recommended_actions'][0]['action'] = action
                    return value
                def callback(payload, number):
                    value = base_review(payload, number)
                    if number == 1:
                        return future_only(value)
                    value['assessments'][0].update(verdict='uncertain', uncertainty_basis=basis,
                        reason='原建议的具体缺陷：' + action)
                    return value
                models['diagnosis'] = RecordingRole(diagnosis)
                models['reviewer'] = RecordingRole(callback)
                report = collaborate(models, provider, P)
                self.assertEqual(report['run_summary']['termination_reason'], 'NEEDS_INFORMATION')
                self.assertFalse(report['output']['recommended_actions'])
                self.assertTrue(report['pending_actions'])
                self.assertEqual(report['pending_actions'][0]['review_verdict'], 'uncertain')
                self.assertEqual(report['reviews'][0]['review']['assessments'][0]['uncertainty_basis'], basis)
                self.assertEqual(report['repair_counts']['reviewer'], 1)
                self.assertEqual(report['run_summary']['tool_calls'], 3)

    def test_family_4_unobserved_timeout_can_remain_unresolved_after_scripted_partial_audit(self):
        c = hypothesis_case()
        refs = selected(c, 'message')
        model = diagnose(c, '解析失败，请求尚未建连并超时', refs, '解析失败日志不能独立确认超时因果')
        self.assertFalse(any(e.payload.get('error_code') == 'REQUEST_TIMEOUT' for e in c.evidence.values()))
        checked, report = review(c, scope='partial', verdict='uncertain')
        self.assertEqual(checked.assessments[1].verdict, 'uncertain')
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'unresolved')
        self.assertIn('工单描述', model.messages[0][0].content)
        self.assertIn('超时', model.messages[0][0].content)
        self.assertIn('完整cause', c.models['reviewer'].messages[0][0].content)
        self.assertEqual(c.context.counts['tool_calls'], 1)

    def test_family_4_actual_timeout_observation_and_narrow_mechanism_are_not_keyword_blocked(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                c = hypothesis_case(timeout=timeout)
                refs = selected(c, 'message')
                cause = '解析失败，日志显示请求尚未建连' + ('；同一调用有超时记录' if timeout else '')
                diagnose(c, cause, refs, '仅陈述所引当前日志直接记录的失败环节，触发来源未知')
                _, report = review(c)
                self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
                cited = {ref.evidence_id for ref in c.draft.hypotheses[0].support_refs}
                if timeout:
                    self.assertTrue(any(c.evidence[e].payload.get('error_code') == 'REQUEST_TIMEOUT' for e in cited))

    def test_family_4_false_full_declaration_is_still_a_semantic_limitation(self):
        c = hypothesis_case()
        diagnose(c, '解析失败并超时', selected(c, 'message'), '刻意无超时观测的负对照')
        _, report = review(c, scope='full', verdict='supported')
        # The program checks labels, not the truth of natural-language assertions.
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')

    def test_family_5_missing_comparison_refs_are_visible_to_scripted_audit(self):
        for baseline in (False, True):
            with self.subTest(baseline=baseline):
                c = hypothesis_case(metrics=True, baseline=baseline)
                after = selected(c, 'value', lambda e: e.payload['id'].endswith('-after'))
                diagnose(c, '解析失败，指标同步上升', selected(c, 'message') + after, '未完整引用前后对照的合成控制')
                _, report = review(c, scope='partial', verdict='uncertain')
                payload = c.models['reviewer'].messages[0][1].content
                body = json.loads(payload)['input']
                cited = {r['evidence_id'] for r in body['targets']['H1']['support_refs']}
                before = {e.evidence_id for e in c.evidence.values() if e.payload.get('id', '').endswith('-before')}
                self.assertEqual(bool(before), baseline)
                self.assertTrue(before.isdisjoint(cited))
                self.assertEqual(report['output']['hypotheses'][0]['status'], 'unresolved')

    def test_family_5_complete_two_point_comparison_keeps_both_samples_and_limits(self):
        c = hypothesis_case(metrics=True)
        refs = selected(c, 'message') + selected(c, 'value')
        model = diagnose(c, '解析失败，请求尚未建连', refs,
            '09:55至10:10两采样点错误率0.01至0.94、连接失败0至28；不证明连续趋势或精确同步')
        _, report = review(c)
        body = json.loads(c.models['reviewer'].messages[0][1].content)['input']
        cited = {r['evidence_id'] for r in body['targets']['H1']['support_refs']}
        metrics = {e.evidence_id for e in c.evidence.values() if e.locator.startswith('get_service_metrics/')}
        self.assertEqual(len(metrics), 4)
        self.assertTrue(metrics <= cited)
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
        self.assertIn('前后样本', model.messages[0][0].content)
        self.assertIn('连续趋势', c.models['reviewer'].messages[0][0].content)
        self.assertEqual(c.context.counts['tool_calls'], 2)

    def test_family_6_static_legacy_basis_and_unrelated_corrections_remain_readable(self):
        legacy = ReviewDecision.model_validate({'assessments': [{'target_id': 'A1', 'verdict': 'uncertain',
            'reason': '历史审阅', 'uncertainty_basis': 'future_result_only'}]})
        self.assertEqual(legacy.assessments[0].uncertainty_basis, 'future_result_only')
        old = ReviewDecision.model_validate({'assessments': [{'target_id': 'H1', 'verdict': 'uncertain', 'reason': '历史缺口'}]})
        self.assertIsNone(old.assessments[0].supported_scope)
        for role, schema in [('diagnosis', DiagnosisSelection), ('reviewer', ObservationReview)]:
            hint = correction_instruction(role, schema, {'reason': 'MANUAL_CHECK_FUTURE_RESULT_ONLY', 'details': []}, 'diagnosis')
            self.assertNotIn('人工核查纠正', hint)
        hint = correction_instruction('reviewer', ReviewDecision, {'reason': 'UNKNOWN_REFERENCE', 'details': []}, 'diagnosis')
        self.assertIn('证据不足时评估为 uncertain', hint)
        self.assertIn('纠正引用时', hint)

    def test_family_6_diagnosis_reference_feedback_and_status_scope_remain(self):
        for path, kind, expected in [('findings.1.refs', 'too_short', 'finding 引用列表为空'),
                ('findings.1.refs.3.reference_id', 'string_pattern_mismatch', '引用编号格式错误')]:
            hint = correction_instruction('diagnosis', DiagnosisSelection,
                {'reason': 'schema_mismatch', 'details': [{'path': path, 'type': kind}]}, 'status_check')
            self.assertIn(expected, hint)
            self.assertIn('hypotheses 和 recommended_actions 必须为空列表', hint)


if __name__ == '__main__':
    unittest.main()
