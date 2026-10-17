"""Fixed-trajectory budget properties, without treating doubles as semantic evidence."""
from datetime import timedelta
import json
import unittest

from langchain_core.messages import AIMessage

from api.auth import Principal
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.v2.engine import investigate


P = Principal('team_a', 'alice')


class LegalTrajectory:
    """Identical lawful proposals at every budget, each window providing new resolution."""
    def __init__(self, scope):
        self.scope = scope
        self.index = 0

    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        if request.get('stage') == 'investigate':
            if self.index < 8:
                value = {'kind': 'query', 'tool': 'get_service_metrics',
                         'args': {'metrics': ['db_cpu'],
                                  'start': (self.scope.start + timedelta(minutes=self.index)).isoformat()}}
                self.index += 1
            else:
                value = {'kind': 'finish'}
        elif request.get('stage') == 'draft':
            value = {'claims': [], 'next_steps': [],
                     'gaps': ['This test measures budget admission, without a semantic verdict.']}
        else:
            raise AssertionError('No semantic targets were submitted by this budget test')
        return AIMessage(content=json.dumps(value), response_metadata={'finish_reason': 'stop'})


class ClosedLoopBudgetTests(unittest.TestCase):
    def run_trajectory(self, budget, *, challenge=True, tools=8):
        provider = FixtureProvider('case_001', P)
        model = LegalTrajectory(provider.ticket().scope)
        return investigate(model, provider, P,
            limits=Limits(model_calls=budget, tool_calls=tools, seconds=180),
            enable_rework=True, enable_challenge=challenge)

    def test_model_budgets_three_through_sixteen_preserve_monotone_query_prefix(self):
        for challenge in (False, True):
            with self.subTest(challenge=challenge):
                traces = []
                counts = []
                for budget in range(3, 17):
                    result = self.run_trajectory(budget, challenge=challenge)
                    traces.append([(row['name'], row['query_profile']) for row in result['collection']])
                    counts.append(result['run_summary']['tool_calls'])
                    self.assertLessEqual(result['run_summary']['model_calls'], budget)
                    self.assertLessEqual(result['run_summary']['tool_calls'], 8)
                    self.assertTrue(result['output']['findings'])
                    self.assertFalse(result['repair_eligible'])
                self.assertEqual(counts, sorted(counts))
                for smaller, larger in zip(traces, traces[1:]):
                    self.assertEqual(smaller, larger[:len(smaller)])

    def test_small_budget_retains_source_facts_without_claiming_a_finished_closed_loop(self):
        for budget in (3, 4, 5):
            with self.subTest(budget=budget):
                result = self.run_trajectory(budget)
                self.assertGreaterEqual(result['run_summary']['tool_calls'], 1)
                self.assertTrue(result['evidence'])
                self.assertTrue(result['output']['findings'])
                self.assertEqual(result['status'], 'partial')
                self.assertNotEqual(result['review_status'], 'passed')
                self.assertLessEqual(result['run_summary']['model_calls'], budget)
                self.assertEqual(result['rework_rounds'], 0)

    def test_tool_cap_is_shared_and_does_not_expand_with_a_larger_model_budget(self):
        for budget in (3, 8, 12, 16):
            with self.subTest(budget=budget):
                result = self.run_trajectory(budget, tools=1)
                self.assertEqual(result['run_summary']['tool_calls'], 1)
                self.assertEqual(len(result['collection']), 1)
                self.assertTrue(result['output']['findings'])
                self.assertLessEqual(result['run_summary']['model_calls'], budget)


if __name__ == '__main__':
    unittest.main()
