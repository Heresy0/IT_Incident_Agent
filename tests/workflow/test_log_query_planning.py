"""Generic scoped query controls; no frozen showcase answers or paid models."""
import json
import unittest
from unittest.mock import patch

from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider, ProviderError
from tests.tools.test_incident_tools import executor, window, P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_information_gain import FocusedLeaf
from workflow.coordinator import collaborate
from workflow.task_checks import log_query_feedback, adds_information


class LogQueryPlanningTests(unittest.TestCase):
    def test_word_in_message_is_not_an_exact_error_code_and_can_relax(self):
        ex = executor()
        with patch.object(ex.provider, 'query', wraps=ex.provider.query) as query:
            ex.execute('get_service_logs', {**window(ex), 'category': 'dependency',
                                          'error_code': 'CONNECTION_REFUSED'})
            row = log_query_feedback(ex)[0]
            self.assertEqual(row['meaning'], 'no_matches_in_selected_scope')
            self.assertFalse(row['error_code_observed_in_log_fields'])
            broader = row['broader_checks'][0]
            self.assertIsNone(broader['args']['error_code'])
            self.assertIsNone(broader['args']['level'])
            self.assertEqual(broader['args']['category'], row['query_profile']['category'])
            self.assertEqual(broader['args']['start'], row['query_profile']['start'])
            self.assertEqual(broader['args']['end'], row['query_profile']['end'])
            self.assertEqual(query.call_count, 1, 'Feedback must not perform reads')
            result = ex.execute(broader['tool'], broader['args'])
        self.assertTrue(any('connection refused' in e.payload['message'] for e in result.evidence))
        self.assertFalse(any(e.payload['error_code'] == 'CONNECTION_REFUSED' for e in result.evidence))
        self.assertEqual(log_query_feedback(ex), [], 'Resolved restrictions remain in collection, not pending feedback')

    def test_real_structured_error_code_is_visible_without_forcing_a_broader_read(self):
        ex = executor()
        result = ex.execute('get_service_logs', {**window(ex), 'category': 'dependency',
                                                'error_code': 'UPSTREAM_UNREACHABLE'})
        self.assertEqual(result.status, 'ok')
        feedback = log_query_feedback(ex)[0]
        self.assertTrue(feedback['error_code_observed_in_log_fields'])
        self.assertEqual(feedback['broader_checks'], [])

    def test_complete_unfiltered_empty_read_has_no_automatic_repeat_or_global_claim(self):
        ex = executor('case_003')
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        feedback = log_query_feedback(ex)[0]
        self.assertEqual(feedback['status'], 'empty')
        self.assertTrue(feedback['query_complete'])
        self.assertEqual(feedback['restricted_by'], [])
        self.assertEqual(feedback['broader_checks'], [])
        self.assertFalse(feedback['this_query_covers_other_categories'])
        narrower = {'tool': 'get_service_logs', 'args': {**window(ex), 'category': 'dependency', 'level': 'ERROR'}}
        self.assertFalse(adds_information([narrower], ex))

    def test_failed_or_truncated_read_is_not_presented_as_complete_empty(self):
        for failure in (True, False):
            ex = executor()
            with self.subTest(failure=failure), patch.object(ex.provider, 'query',
                    side_effect=ProviderError('PROVIDER_UNAVAILABLE') if failure else None,
                    return_value=([], True)):
                ex.execute('get_service_logs', {**window(ex), 'category': 'dependency', 'error_code': 'ABSENT'})
            feedback = log_query_feedback(ex)[0]
            self.assertFalse(feedback['query_complete'])
            self.assertEqual(feedback['meaning'], 'query_failed' if failure else 'incomplete_sample')
            self.assertEqual(feedback['broader_checks'], [])

    def test_scheduler_uses_feedback_and_reviewer_receives_actual_filter_scope(self):
        provider = FixtureProvider('case_001', P)
        scoped = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        models = scripted_models()

        def plan(response, messages, calls):
            value = json.loads(response.content)
            payload = json.loads(messages[1].content)['input']
            if calls == 1:
                value.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': '核对受限查询',
                    'need_ids': ['N1'], 'checks': [
                        {'tool': 'get_service_metrics', 'args': {**scoped, 'metrics': ['dependency_error_rate']}},
                        {'tool': 'get_service_logs', 'args': {**scoped, 'category': 'dependency', 'error_code': 'ABSENT'}}]}])
            elif calls == 2:
                feedback = payload['planning_guidance']['log_query_feedback'][0]
                self.assertFalse(feedback['error_code_observed_in_log_fields'])
                value.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': '检查过滤假设',
                    'need_ids': ['N2'], 'expected_value': '放宽后有拒绝或超时记录则改变机制判断；仍空则保留类别限制',
                    'checks': feedback['broader_checks']}])
            return json_response(value)

        models['supervisor'] = AlterOutput(models['supervisor'], plan)
        models['investigation'] = FocusedLeaf()
        result = collaborate(models, provider, P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['run_summary']['tool_calls'], 3)
        review = json.loads(models['reviewer'].histories[0][1].content)['input']
        broader = [row for row in review['collection'] if row['name'] == 'get_service_logs'][-1]
        self.assertEqual(broader['status'], 'ok')
        self.assertIsNone(broader['query_profile']['error_code'])


if __name__ == '__main__':
    unittest.main()
