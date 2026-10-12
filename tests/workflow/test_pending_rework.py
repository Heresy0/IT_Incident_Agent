"""Pending checks must retain scope, question ownership and bounded execution."""
import json
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage
from agents.contracts import ReadOnlyCheck
from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from workflow.coordinator import Collaboration


class InterpretOnly:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(content='{"findings":[],"missing_information":["Observations require semantic review"]}')


class PendingReworkTests(unittest.TestCase):
    def setup_rework(self, limits=None):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
            limits=limits or Limits(model_calls=16, tool_calls=8, reserve_model_calls=3), investigation_mode='agent')
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        check = {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu']}}
        c.evidence_needs['N2'] = c.evidence_needs['N2'].model_copy(update={
            'next_check': ReadOnlyCheck.model_validate_json(json.dumps(check))})
        c.phase = 'rework'
        c.pending = {'challenge_id': 'C1', 'need_id': 'N2', 'target_id': 'H1',
            'evidence_ids': [], 'checks': [], 'expected_value': 'Compare current database load with the candidate mechanism.',
            'missing_observation': 'Current database load', 'planning_required': True}
        return c, check

    def test_reuses_valid_need_check_and_executes_without_planning_call(self):
        c, check = self.setup_rework()
        c.models['investigation'] = InterpretOnly()
        with patch.object(c, 'ask', side_effect=AssertionError('No new planning call')):
            c.supervise()
        self.assertEqual(c.context.counts['model_calls'], 0)
        self.assertEqual(c.reworks, 1)
        self.assertEqual(c.supervisor_calls, 1)
        self.assertEqual(c.next_node, 'investigation')
        c.execute_task('investigation')
        self.assertEqual(c.context.counts['tool_calls'], 1)
        self.assertEqual(c.context.counts['model_calls'], 1)
        self.assertTrue(c.evidence)
        self.assertNotEqual(c.evidence_needs['N2'].status, 'supported')
        self.assertTrue(any(e['type'] == 'rework_existing_checks_selected' for e in c.events))
        self.assertEqual(c.next_node, 'diagnosis')

    def test_never_retries_completed_or_failed_checks(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                c, check = self.setup_rework()
                if failed:
                    with patch.object(c.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')):
                        c.executors['investigation'].execute(check['tool'], check['args'])
                else:
                    c.executors['investigation'].execute(check['tool'], check['args'])
                self.assertIsNone(c.pending_rework_selection())
                self.assertEqual(c.context.counts['tool_calls'], 1)

    def test_unrelated_need_and_exhausted_budget_are_not_reused(self):
        c, check = self.setup_rework()
        c.pending['need_id'] = 'N3'
        self.assertIsNone(c.pending_rework_selection())
        c, check = self.setup_rework(Limits(model_calls=2, tool_calls=8))
        self.assertIsNone(c.pending_rework_selection())
        c, check = self.setup_rework()
        c.reworks = 1
        self.assertIsNone(c.pending_rework_selection())

    def test_invalid_scope_and_wrong_role_are_not_dispatched(self):
        c, check = self.setup_rework()
        c.evidence_needs['N2'] = c.evidence_needs['N2'].model_copy(update={'role': 'knowledge'})
        self.assertIsNone(c.pending_rework_selection())
        c, check = self.setup_rework()
        invalid = {**check, 'args': {**check['args'], 'start': '2020-01-01T00:00:00Z'}}
        c.evidence_needs['N2'] = c.evidence_needs['N2'].model_copy(update={
            'next_check': ReadOnlyCheck.model_validate_json(json.dumps(invalid))})
        self.assertIsNone(c.pending_rework_selection())

    def test_existing_task_check_is_reused_with_original_expectation(self):
        c, check = self.setup_rework()
        c.evidence_needs['N2'] = c.evidence_needs['N2'].model_copy(update={'next_check': None})
        c.tasks = [{'role': 'investigation', 'need_ids': ['N2'], 'required_checks': [check],
                    'expected_value': 'Existing task expectation'}]
        selection = c.pending_rework_selection()
        self.assertEqual(selection.tasks[0].expected_value, 'Existing task expectation')
        self.assertEqual(selection.tasks[0].need_ids, ['N2'])


if __name__ == '__main__':
    unittest.main()
