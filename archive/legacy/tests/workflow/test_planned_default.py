"""Default planning/execution boundary and independent review, using real guards."""
import json
import unittest
from unittest.mock import patch

from agents.contracts import ReviewDecision, SupervisorSelection
from agents.models import build_roles
from agents.scripted import scripted_models
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import Collaboration, collaborate
from workflow.evidence_needs import is_blocking


def planned_models():
    models = scripted_models()
    def plan(response, messages, calls):
        data = json.loads(response.content)
        payload = json.loads(messages[1].content)['input']
        window = {k: payload['ticket']['scope'][k] for k in ('start', 'end')}
        for task in data.get('tasks', []):
            task.update(checks=[
                {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}},
                {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['dependency_error_rate']}}],
                expected_value='取得失败环节和症状观测，供独立复核')
        return json_response(data)
    models['supervisor'] = AlterOutput(models['supervisor'], plan)
    return models


class PlannedDefaultTests(unittest.TestCase):
    def test_default_executes_plan_without_leaf_model_and_reviews_raw_evidence(self):
        models = planned_models()
        with patch.object(models['investigation'], 'invoke', side_effect=AssertionError('No leaf LLM')):
            result = collaborate(models, FixtureProvider('case_001', P), P,
                                 limits=Limits(model_calls=12, tool_calls=4))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['collection_execution']['investigation_mode'], 'programmatic')
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(result['run_summary']['model_calls'], 4)
        self.assertFalse(result['blind_review_enabled'])
        self.assertEqual(result['observation_reviews'], [])
        self.assertTrue(result['reviews'])
        self.assertTrue(result['output']['recommended_actions'])
        self.assertEqual(result['task_results'][0]['model_calls'], 0)
        payload = json.loads(models['reviewer'].histories[0][1].content)
        self.assertTrue(payload['input']['evidence'])
        self.assertTrue(payload['input']['collection'])
        for need in payload['input']['evidence_needs']:
            self.assertNotIn('status', need)
        for task in payload['input']['task_results']:
            self.assertNotIn('candidate_mechanisms', task)

    def test_missing_concrete_plan_fails_before_any_query(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P)
        proposal = SupervisorSelection(action='dispatch', reason='Need observations',
            tasks=[{'role': 'investigation', 'goal': 'Investigate', 'need_ids': ['N2']}])
        with self.assertRaises(ProtocolError) as caught:
            c.supervisor_decision(proposal)
        self.assertEqual(caught.exception.code, 'INVALID_TASK_CHECK')
        self.assertEqual(c.context.counts['tool_calls'], 0)

    def test_core_flag_is_server_owned_without_promoting_unknown_or_allowing_bad_refs(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P)
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        c.run()
        calls_before = c.context.counts['model_calls']
        review = ReviewDecision(assessments=[{'target_id': key, 'verdict': 'uncertain', 'reason': '待核实'}
                                            for key in c.targets()], need_assessments=[{
            'need_id': 'N2', 'verdict': 'uncertain', 'reason': '缺少机制证据', 'blocking': False}])
        accepted = c.validate_review(review)
        n2 = next(a for a in accepted.need_assessments if a.need_id == 'N2')
        self.assertEqual(n2.verdict, 'uncertain')
        self.assertIsNone(n2.blocking)
        self.assertTrue(is_blocking(c.evidence_needs['N2']))
        self.assertEqual(c.context.counts['model_calls'], calls_before)
        bad = review.model_copy(update={'need_assessments': [n2.model_copy(update={
            'evidence_ids': ['EV_NOT_OBSERVED'], 'blocking': False})]})
        with self.assertRaises(ProtocolError):
            c.validate_review(bad)
        unsupported = review.model_copy(update={'need_assessments': [n2.model_copy(update={
            'verdict': 'not_required'})]})
        with self.assertRaises(ProtocolError):
            c.validate_review(unsupported)

    def test_direct_live_review_still_requires_explained_blocking_gap(self):
        models = planned_models()
        c = Collaboration(models, FixtureProvider('case_001', P), P)
        c.run()
        models['reviewer'].json_output = True
        self.assertFalse(c.blind_review)
        with self.assertRaises(ProtocolError) as caught:
            c.validate_review(ReviewDecision(assessments=[
                {'target_id': key, 'verdict': 'uncertain', 'reason': '待核实'} for key in c.targets()],
                need_assessments=[]))
        self.assertEqual(caught.exception.code, 'REVIEW_BLOCKER_REQUIRED')

    def test_role_builder_defaults_direct_and_blind_remains_available(self):
        with patch('agents.models.build_model') as factory:
            factory.return_value.blind_review = True
            models = build_roles('test-only')
        self.assertFalse(models['reviewer'].blind_review)
        c = Collaboration(models, FixtureProvider('case_001', P), P, blind_review=True)
        self.assertTrue(c.blind_review)

    def test_rework_reuses_attempted_reads_and_hands_off_without_approving(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
                window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
                check = {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}}
                if failed:
                    with patch.object(c.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')):
                        c.executors['investigation'].execute(check['tool'], check['args'])
                else:
                    c.executors['investigation'].execute(check['tool'], check['args'])
                c.phase, c.pending = 'rework', {'need_id': 'N2', 'missing_observation': '还需区分故障机制'}
                proposal = SupervisorSelection(action='dispatch', reason='Collect more', tasks=[{
                    'role': 'investigation', 'goal': 'Collect', 'need_ids': ['N2'],
                    'checks': [check], 'expected_value': '区分机制'}])
                decision = c.supervisor_decision(proposal)
                self.assertEqual(decision.action, 'request_info')
                self.assertEqual(decision.tasks, [])
                self.assertEqual(c.context.counts['tool_calls'], 1)
                self.assertEqual(c.evidence_needs['N2'].status, 'pending')
                self.assertIn('还需区分故障机制', decision.missing_information)
                self.assertIsNone(c.review)

    def test_mixed_rework_executes_only_new_checks_and_still_rejects_unsafe_scope(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P,
                          limits=Limits(model_calls=32, tool_calls=8))
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        old = {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}}
        new = {'tool': 'get_recent_changes', 'args': window}
        c.executors['investigation'].execute(old['tool'], old['args'])
        c.phase, c.pending = 'rework', {'need_id': 'N2', 'missing_observation': '变化原因'}
        proposal = SupervisorSelection(action='dispatch', reason='Collect more', tasks=[{
            'role': 'investigation', 'goal': 'Collect', 'need_ids': ['N2'],
            'checks': [old, new], 'expected_value': '查变更以区分原因'}])
        decision = c.supervisor_decision(proposal)
        self.assertEqual(decision.action, 'dispatch')
        self.assertEqual([check.tool for check in decision.tasks[0].checks], ['get_recent_changes'])
        bad = proposal.model_copy(deep=True)
        bad.tasks[0].checks[1].args['start'] = '2020-01-01T00:00:00Z'
        with self.assertRaises(ProtocolError) as caught:
            c.supervisor_decision(bad)
        self.assertEqual(caught.exception.code, 'INVALID_TASK_CHECK')
