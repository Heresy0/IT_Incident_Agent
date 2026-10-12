"""Program executes explicit reads; causal assessments remain outside this stage."""
import json
import unittest
from unittest.mock import patch
from agents.contracts import TaskRequest
from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from workflow.coordinator import Collaboration


class ProgrammaticInvestigationTests(unittest.TestCase):
    def prepared(self, checks=True):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=16, tool_calls=8), investigation_mode='programmatic')
        w = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        task = TaskRequest.model_validate_json(json.dumps({'role': 'investigation', 'goal': 'Read current load',
            'need_ids': ['N2'], 'checks': [{'tool': 'get_service_metrics', 'args': {**w, 'metrics': ['db_cpu'], 'granularity': '1m'}}] if checks else []}))
        return c, task

    def read(self, c, task):
        objective = c.task_objective(task, False)
        with patch.object(c.models['investigation'], 'invoke', side_effect=AssertionError('Investigation must not invoke a model')):
            result = c.collect_task(task, objective, c.executors['investigation'], c.context, c.events, 4)
        c.record_task(task, objective, result)
        return result

    def test_registered_read_executes_with_zero_leaf_model_calls(self):
        c, task = self.prepared()
        r = self.read(c, task)
        self.assertEqual(r['status'], 'completed')
        self.assertEqual(r['task_counts']['model_calls'], 0)
        self.assertEqual(r['task_counts']['tool_calls'], 1)
        self.assertTrue(c.evidence)
        self.assertEqual(r['output']['findings'], [])
        self.assertEqual(r['output']['tentative_hypotheses'], [])
        self.assertNotEqual(c.evidence_needs['N2'].status, 'supported')
        self.assertEqual(self.read(c, task)['task_counts']['tool_calls'], 0)

    def test_failure_is_preserved_and_not_retried(self):
        c, task = self.prepared()
        with patch.object(c.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')) as query:
            a, b = self.read(c, task), self.read(c, task)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(a['status'], 'partial')
        self.assertEqual(b['status'], 'partial')
        self.assertEqual(c.context.counts['tool_calls'], 1)
        self.assertIn('查询失败', json.dumps(b['output'], ensure_ascii=False))

    def test_missing_plan_and_tool_budget_do_not_trigger_unplanned_read(self):
        c, task = self.prepared(False)
        self.assertEqual(self.read(c, task)['run_summary']['termination_reason'], 'NO_TOOL_PROGRESS')
        self.assertEqual(c.context.counts['tool_calls'], 0)
        c, task = self.prepared()
        c.context.counts['tool_calls'] = c.context.limits.tool_calls
        r = self.read(c, task)
        self.assertEqual(r['status'], 'partial')
        self.assertEqual(r['run_summary']['termination_reason'], 'BUDGET_EXCEEDED')
        self.assertFalse(c.evidence)

    def test_invalid_scope_uses_existing_executor_guard(self):
        c, task = self.prepared()
        invalid = task.model_copy(update={'checks': [task.checks[0].model_copy(update={
            'args': {**task.checks[0].args, 'start': '2020-01-01T00:00:00Z'}})]})
        r = self.read(c, invalid)
        self.assertEqual(r['status'], 'partial')
        self.assertFalse(c.evidence)
        self.assertEqual(c.context.counts['tool_calls'], 0)


if __name__ == '__main__':
    unittest.main()
