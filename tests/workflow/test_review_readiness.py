"""Free end-to-end report semantics and repair isolation; no live providers."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from agents.contracts import DiagnosisSelection
from agents.scripted import scripted_models
from evidence.diagnosis import ProtocolError, expand_diagnosis
from evidence.render import render
from incidents.support import IncidentError
from providers.fixtures import FixtureProvider
from repairs.service import RepairService
from workflow.coordinator import collaborate
from tests.tools.test_incident_tools import P, executor, window
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class ReviewReadinessTests(unittest.TestCase):
    def run_report(self, draft_change=None, review_change=None):
        provider = FixtureProvider('case_001', P)
        models = scripted_models()
        def diagnose(response, messages, calls):
            data = json.loads(response.content)
            data['hypotheses'][0].update(level='mechanism', evidence_explanation='当前日志与异常指标支持近端机制；具体触发原因未知。')
            scope = json.loads(messages[1].content)['input']['ticket']['scope']
            data['recommended_actions'][0].update(kind='read_only_check', requires_approval=False,
                action='核对窗口内配置变更，区分配置变化与其他触发因素。',
                condition='已取得异常日志及指标。',
                checks=[{'tool': 'get_recent_changes', 'args': {k: scope[k] for k in ('start', 'end')}}])
            if draft_change:
                draft_change(data)
            return json_response(data)
        def review(response, messages, calls):
            data = json.loads(response.content)
            data['follow_up_reason'] = '控制场景中只展示已支持结果与具体限制，不请求新读取。'
            if review_change:
                review_change(data)
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], diagnose)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        return collaborate(models, provider, P), models

    def test_refuted_alternative_is_a_completed_investigation_result(self):
        def draft(data):
            data['hypotheses'].append({**deepcopy(data['hypotheses'][0]),
                'level': 'alternative', 'cause': '已排除的竞争解释'})
        def review(data):
            next(a for a in data['assessments'] if a['target_id'] == 'H2').update(
                verdict='not_supported', reason='已有对照不支持此竞争解释。')
        result, models = self.run_report(draft, review)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['output']['hypotheses'][1]['status'], 'refuted')
        self.assertEqual(result['diagnosis_readiness']['blocking_gaps'], [])
        self.assertEqual(result['diagnosis_readiness']['report_status'], 'reviewed_with_limitations')
        self.assertEqual(models['reviewer'].calls, 1)
        self.assertIn('替代解释', render(result))

    def test_unknown_trigger_remains_visible_without_blocking_supported_mechanism(self):
        def draft(data):
            data['hypotheses'].append({**deepcopy(data['hypotheses'][0]), 'level': 'trigger',
                'cause': '待核实的触发因素', 'pending_checks': ['尚未核对触发条件。']})
        def review(data):
            next(a for a in data['assessments'] if a['target_id'] == 'H2').update(
                verdict='uncertain', reason='未知触发因素不改变当前只读核查下一步。')
        result, models = self.run_report(draft, review)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['output']['hypotheses'][1]['status'], 'unresolved')
        self.assertEqual(models['reviewer'].calls, 1)
        self.assertIn('待验证：尚未核对触发条件', render(result))

    def test_read_only_next_step_does_not_require_its_future_query_result(self):
        result, models = self.run_report()
        self.assertEqual(result['status'], 'completed')
        action = result['output']['recommended_actions'][0]
        self.assertEqual(action['kind'], 'read_only_check')
        self.assertEqual(action['checks'][0]['tool'], 'get_recent_changes')
        self.assertFalse(any(e.get('name') == 'get_recent_changes' and e['type'] == 'tool_result'
                             for e in result['events']))
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(result['run_summary']['model_calls'], 6)
        for role in ('supervisor', 'diagnosis', 'reviewer'):
            model = models[role].model if isinstance(models[role], AlterOutput) else models[role]
            data = json.loads(model.histories[0][1].content)['input']
            self.assertEqual(data['diagnosis_policy']['version'], 'diagnosis_readiness_v2')

    def test_nonblocking_uncertainty_requires_explicit_materiality_assessment(self):
        for blocking, status in ((False, 'completed'), (True, 'partial'), (None, 'partial')):
            def review(data):
                row = next(a for a in data['need_assessments'] if a['need_id'] == 'N3')
                row.update(verdict='uncertain', blocking=blocking,
                    reason='未确认的替代因素；明确说明是否改变当前机制与下一步。')
            with self.subTest(blocking=blocking):
                result, _ = self.run_report(review_change=review)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['evidence_needs'][2]['status'], 'pending')
                self.assertIn('未确认的替代因素', render(result))

    def test_symptom_and_mechanism_cannot_be_marked_nonblocking(self):
        for need_id in ('N1', 'N2'):
            def review(data):
                next(a for a in data['need_assessments'] if a['need_id'] == need_id).update(
                    verdict='uncertain', blocking=False)
            with self.subTest(need_id=need_id):
                result, _ = self.run_report(review_change=review)
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
                self.assertEqual(result['repairs'], 1)

    def test_trigger_or_refuted_only_cannot_replace_a_supported_mechanism(self):
        for mode in ('trigger', 'refuted', 'uncertain'):
            def draft(data):
                if mode == 'trigger':
                    data['hypotheses'][0]['level'] = 'trigger'
            def review(data):
                if mode != 'trigger':
                    next(a for a in data['assessments'] if a['target_id'] == 'H1')['verdict'] = (
                        'not_supported' if mode == 'refuted' else 'uncertain')
            with self.subTest(mode=mode):
                result, _ = self.run_report(draft, review)
                self.assertEqual(result['status'], 'partial')
                self.assertTrue(result['diagnosis_readiness']['blocking_gaps'])

    def test_unreviewed_change_is_preserved_separately_from_supported_next_step(self):
        def draft(data):
            data['recommended_actions'].append({**deepcopy(data['recommended_actions'][0]),
                'kind': 'manual_change', 'checks': [], 'action': '确认条件后调整配置',
                'requires_approval': True, 'condition': '余量与业务窗口均待确认。'})
        def review(data):
            next(a for a in data['assessments'] if a['target_id'] == 'A2').update(
                verdict='uncertain', reason='尚未确认变更条件。')
        result, _ = self.run_report(draft, review)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['output']['recommended_actions']), 1)
        pending = result['pending_actions'][0]
        self.assertFalse(pending['executable'])
        self.assertIsNone(pending['repair'])
        self.assertIn('未通过复核，不能据此审批或执行修复', render(result))
        workflow = Mock()
        workflow.get_run.return_value = {'config': {'incident_id': 'incident'}, 'result': result}
        with self.assertRaises(IncidentError) as caught:
            RepairService(workflow).from_recommendation('incident', {'run_id': result['run_id'], 'action_index': 0}, P)
        self.assertEqual(caught.exception.code, 'NO_EXECUTABLE_RECOMMENDATION')

    def test_read_only_proposal_is_validated_without_running_tools(self):
        ex = executor()
        # Live registrations enforce metric names; legacy fixtures allow empty queries.
        ex.provider.binding = SimpleNamespace(observations={
            'prometheus': {'url': 'http://not-used.invalid'}, 'metrics': {'db_cpu': {'unit': 'percent'}}})
        for args in ({**window(ex), 'metrics': ['not_registered']},
                     {**window(ex), 'metrics': ['db_cpu'], 'tenant_id': 'other'}):
            selection = DiagnosisSelection.model_validate({'recommended_actions': [{
                'action': '核查', 'condition': '观测异常', 'expected_result': '区分原因', 'risk': '只读',
                'requires_approval': False, 'kind': 'read_only_check',
                'checks': [{'tool': 'get_service_metrics', 'args': args}]}]})
            with self.subTest(args=args), self.assertRaises(ProtocolError):
                expand_diagnosis(selection, ex)
        self.assertEqual(ex.context.counts['tool_calls'], 0)
        self.assertFalse(ex.evidence)

    def test_unreviewed_registered_repair_keeps_intent_only_in_original_draft(self):
        from tests.workflow.test_conditional_repairs import ConditionalRepairTests
        result, *_ = ConditionalRepairTests().run_worker(verdict='uncertain')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['output']['recommended_actions'], [])
        self.assertEqual(result['drafts'][0]['draft']['recommended_actions'][0]['repair']['action'], 'restart_service')
        self.assertEqual(result['pending_actions'][0]['kind'], 'registered_repair')
        self.assertIsNone(result['pending_actions'][0]['repair'])
        self.assertFalse(result['pending_actions'][0]['executable'])

    def test_change_kind_cannot_bypass_approval_or_hide_an_executable_intent(self):
        ex = executor()
        for change in ({'kind': 'manual_change', 'requires_approval': False},
                       {'kind': 'registered_repair', 'requires_approval': True}):
            selection = DiagnosisSelection.model_validate({'recommended_actions': [{
                'action': '修改', 'condition': '待确认', 'expected_result': '恢复', 'risk': '影响业务', **change}]})
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                expand_diagnosis(selection, ex)


if __name__ == '__main__':
    unittest.main()
