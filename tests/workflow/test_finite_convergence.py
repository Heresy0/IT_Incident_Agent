"""Local controls for actionable failures and useful next reads; no model calls."""
import json
import unittest

from agents.contracts import DiagnosisSelection, NeedAssessment
from agents.scripted import scripted_models
from evidence.diagnosis import ProtocolError, expand_diagnosis
from evidence.render import render
from providers.fixtures import FixtureProvider
from tests.tools.test_incident_tools import P, executor, window
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import collaborate
from workflow.evidence_needs import initial_needs, validate_assessments


class FiniteConvergenceTests(unittest.TestCase):
    def test_core_assessment_reports_both_conditions_without_copying_reason(self):
        needs = initial_needs(FixtureProvider('case_001', P).ticket())
        assessment = NeedAssessment(need_id='N2', verdict='not_required', blocking=False,
                                    reason='secret-canary')
        with self.assertRaises(ProtocolError) as caught:
            validate_assessments(needs, [assessment], {})
        details = caught.exception.details
        self.assertEqual([d['path'] for d in details],
                         ['need_assessments.0.blocking', 'need_assessments.0.verdict'])
        self.assertTrue(all(d['need_id'] == 'N2' for d in details))
        self.assertNotIn('secret-canary', json.dumps(details))

    def action(self, ex, args):
        # The collaboration registry exposes role executors; Single keeps its contract.
        ex.executors = {'investigation': ex, 'knowledge': ex}
        return expand_diagnosis(DiagnosisSelection(recommended_actions=[{
            'action': '核查', 'condition': '当前异常', 'expected_result': '区分解释',
            'risk': '只读', 'requires_approval': False, 'kind': 'read_only_check',
            'checks': [{'tool': 'get_service_logs', 'args': args}]}]), ex)

    def test_complete_broad_log_read_rejects_narrower_advice(self):
        ex = executor()
        args = {**window(ex), 'category': 'dependency'}
        ex.execute('get_service_logs', args)
        with self.assertRaises(ProtocolError) as caught:
            self.action(ex, {**args, 'level': 'ERROR'})
        self.assertEqual(caught.exception.code, 'READ_ONLY_ACTION_ALREADY_COVERED')
        self.assertEqual(caught.exception.details[0]['path'], 'recommended_actions.0.checks.0')

    def test_complete_empty_read_also_rejects_repeat(self):
        ex = executor('case_003')
        args = {**window(ex), 'category': 'dependency'}
        ex.execute('get_service_logs', args)
        with self.assertRaises(ProtocolError):
            self.action(ex, args)

    def test_restricted_empty_read_allows_broader_advice(self):
        ex = executor()
        args = {**window(ex), 'category': 'dependency'}
        ex.execute('get_service_logs', {**args, 'error_code': 'ABSENT'})
        draft = self.action(ex, args)
        self.assertEqual(len(draft.recommended_actions), 1)

    def test_diagnosis_replaces_repeated_advice_with_new_registered_check(self):
        models = scripted_models()
        histories = []
        def advice(response, messages, calls):
            histories.append(messages)
            data = json.loads(response.content)
            payload = json.loads(messages[1].content)['input']
            scope = payload['ticket']['scope']
            args = {key: scope[key] for key in ('start', 'end')}
            previous = next(row['query_profile'] for row in payload['collection']
                            if row['name'] == 'get_service_metrics')
            check = ({'tool': 'get_service_metrics', 'args': {**args, 'metrics': previous['metrics']}}
                     if calls == 1 else {'tool': 'get_recent_changes', 'args': args})
            data['recommended_actions'][0].update(kind='read_only_check',
                requires_approval=False, checks=[check])
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], advice)
        result = collaborate(models, FixtureProvider('case_001', P), P, investigation_mode='agent')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['repairs'], 1)
        self.assertIn('READ_ONLY_ACTION_ALREADY_COVERED', histories[1][-1].content)
        self.assertEqual(result['output']['recommended_actions'][0]['checks'][0]['tool'],
                         'get_recent_changes')
        self.assertEqual(result['run_summary']['tool_calls'], 2, 'Advice does not execute')

    def test_reviewer_receives_precise_correction_and_recovers_in_original_budget(self):
        models = scripted_models()
        history = []
        def invalid_once(response, messages, calls):
            history.append(messages)
            data = json.loads(response.content)
            if calls == 1:
                data['need_assessments'][0].update(verdict='not_required', blocking=False)
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], invalid_once)
        result = collaborate(models, FixtureProvider('case_001', P), P, investigation_mode='agent')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['repairs'], 1)
        self.assertIn('need_assessments.0.verdict', history[1][-1].content)
        self.assertIn('uncertain', history[1][-1].content)

    def test_exhausted_correction_preserves_observations_draft_and_failure_location(self):
        models = scripted_models()
        def classify(response, *_):
            data = json.loads(response.content)
            for action in data['recommended_actions']:
                action.update(kind='manual_change', requires_approval=True)
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], classify)
        def invalid(response, *_):
            data = json.loads(response.content)
            data['need_assessments'][0].update(verdict='not_required', blocking=False)
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], invalid)
        result = collaborate(models, FixtureProvider('case_001', P), P, investigation_mode='agent')
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['unreviewed_observations'])
        self.assertTrue(result['drafts'])
        self.assertTrue(result['pending_actions'])
        self.assertFalse(result['output']['recommended_actions'])
        self.assertIn('need_assessments.0.verdict', render(result))
        self.assertEqual(result['repairs'], 1)


if __name__ == '__main__':
    unittest.main()
