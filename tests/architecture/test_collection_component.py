"""Collection runs without a Coordinator and never commits shared workflow state."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from agents.contracts import TaskRequest
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import ExecutionError, Limits, RunContext
from tests.tools.test_incident_tools import P
from tools.executor import ToolExecutor
from workflow.collection import CollectionRequest, CollectionRunner, join_parallel


class CollectionComponentTests(unittest.TestCase):
    def setUp(self):
        self.provider = FixtureProvider('case_001', P)
        self.ticket = self.provider.ticket()
        self.events = []
        self.context = RunContext(limits=Limits(model_calls=16, tool_calls=8, reserve_model_calls=3),
                                  scope=self.ticket.scope, emit=self.events.append)
        self.executors = {role: ToolExecutor(self.provider, P, self.ticket.scope, self.context, role=role)
                          for role in ('investigation', 'knowledge')}
        self.runner = CollectionRunner({}, self.provider, P, {'repairs': 0},
            modes={'investigation': 'programmatic', 'knowledge': 'programmatic'})
        window = self.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        self.tasks = [TaskRequest(role='investigation', goal='Inspect load', need_ids=['N1'],
            checks=[{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu']}}]),
            TaskRequest(role='knowledge', goal='Find historical clues', need_ids=['N3'],
                checks=[{'tool': 'search_runbooks', 'args': {'query': 'pool'}}])]
        # Collection receives normalized plans from Planning, including argument defaults.
        self.tasks = [task.model_copy(update={'checks': [check.model_copy(update={
            'args': self.executors[task.role].validate_args(check.tool, check.args).model_dump(mode='json')})
            for check in task.checks]}) for task in self.tasks]

    @staticmethod
    def objective(task):
        return {'goal': task.goal, 'required_checks': [c.model_dump(mode='json') for c in task.checks],
                'approved_checks': None, 'need_ids': list(task.need_ids), 'expected_value': task.expected_value}

    def prepare(self):
        return self.runner.prepare_parallel(list(reversed(self.tasks)), context=self.context,
            executors=self.executors, model_reserve=3, objective_for=self.objective)

    def test_programmatic_read_returns_ledger_and_counts_without_a_model(self):
        task = self.tasks[0]
        request = CollectionRequest(task, self.objective(task), 'programmatic', 0)
        executor = self.executors[task.role]
        with patch.object(self.provider, 'query', wraps=self.provider.query) as query:
            first = self.runner.collect(request, executor=executor, context=self.context, event_log=self.events)
            second = self.runner.collect(request, executor=executor, context=self.context, event_log=self.events)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(first.counts['tool_calls'], 1)
        self.assertEqual(second.counts['tool_calls'], 0)
        self.assertEqual(first.counts['model_calls'], 0)
        self.assertTrue(first.evidence and first.references and first.query_results)
        self.assertEqual(first.result['output']['findings'], [])
        self.assertEqual(first.result['output']['tentative_hypotheses'], [])

    def test_parallel_preparation_freezes_objectives_without_charging_calls(self):
        objectives = {task.role: self.objective(task) for task in self.tasks}
        batch = self.runner.prepare_parallel(self.tasks, context=self.context, executors=self.executors,
            model_reserve=3, objective_for=lambda task: objectives[task.role])
        objectives['investigation']['required_checks'].clear()
        self.assertTrue(batch.plans['investigation'].request.objective['required_checks'])
        self.assertEqual(self.context.counts, {'model_calls': 0, 'tool_calls': 0, 'web_calls': 0})
        for role, plan in batch.plans.items():
            self.assertIsNot(plan.executor, self.executors[role])
            self.assertIs(plan.executor.context, plan.context)
            self.assertIs(plan.context.parent, self.context)

    def test_join_is_stable_and_does_not_install_branch_ledgers(self):
        batch = self.prepare()
        outcomes = {role: self.runner.collect_parallel(plan, batch.batch_id)
                    for role, plan in reversed(list(batch.plans.items()))}
        joined = join_parallel(batch.batch_id, batch.plans, outcomes)
        self.assertEqual([row.request.task.role for row in joined.collections], ['investigation', 'knowledge'])
        self.assertFalse(joined.incomplete)
        self.assertEqual(sum(row.counts['tool_calls'] for row in joined.collections),
                         self.context.counts['tool_calls'])
        for role, plan in batch.plans.items():
            self.assertFalse(self.executors[role].evidence)
            self.assertFalse(self.executors[role].check_results)
            self.assertIs(plan.executor.context, plan.context)

    def test_join_rejects_missing_wrong_batch_and_swapped_branches(self):
        batch = self.prepare()
        outcomes = {role: self.runner.collect_parallel(plan, batch.batch_id)
                    for role, plan in batch.plans.items()}
        invalid = [
            {'investigation': outcomes['investigation']},
            {**outcomes, 'knowledge': replace(outcomes['knowledge'], batch_id='older-batch')},
            {'investigation': outcomes['knowledge'], 'knowledge': outcomes['investigation']},
        ]
        for payload in invalid:
            with self.subTest(payload=list(payload)), self.assertRaises(ProtocolError) as error:
                join_parallel(batch.batch_id, batch.plans, payload)
            self.assertEqual(error.exception.code, 'INVALID_TASK_ROUTE')
        self.assertTrue(all(not executor.evidence for executor in self.executors.values()))

    def test_source_failure_retains_sibling_evidence_and_attempt_counts(self):
        original = self.provider.query
        def unavailable(tool, *args, **kwargs):
            if tool == 'search_runbooks':
                raise ProviderError('SOURCE_UNAVAILABLE')
            return original(tool, *args, **kwargs)
        batch = self.prepare()
        with patch.object(self.provider, 'query', side_effect=unavailable) as query:
            outcomes = {role: self.runner.collect_parallel(plan, batch.batch_id)
                        for role, plan in batch.plans.items()}
        joined = join_parallel(batch.batch_id, batch.plans, outcomes)
        self.assertEqual(query.call_count, 2)
        self.assertTrue(joined.incomplete)
        self.assertIsNone(joined.fatal_code)
        self.assertTrue(outcomes['investigation'].collection.evidence)
        failed = outcomes['knowledge'].collection
        self.assertEqual(failed.result['status'], 'partial')
        self.assertEqual(failed.coverage['checks'][0]['status'], 'failed')
        self.assertEqual(failed.counts['tool_calls'], 1)

    def test_unexpected_branch_failure_is_sanitized_and_joinable(self):
        class BrokenModel:
            def bind_tools(self, tools):
                raise RuntimeError('private-error-canary')
        self.runner.models = {'knowledge': BrokenModel()}
        self.runner.modes = {**self.runner.modes, 'knowledge': 'agent'}
        batch = self.prepare()
        outcomes = {role: self.runner.collect_parallel(plan, batch.batch_id)
                    for role, plan in batch.plans.items()}
        joined = join_parallel(batch.batch_id, batch.plans, outcomes)
        self.assertEqual(joined.stop_codes, ('INTERNAL_ERROR',))
        self.assertNotIn('private-error-canary', json.dumps(outcomes['knowledge'].collection.result))
        self.assertEqual(outcomes['knowledge'].collection.counts['model_calls'], 0)
        self.assertTrue(outcomes['investigation'].collection.evidence)

    def test_branch_allocations_preserve_original_finalization_reserve(self):
        self.context.limits = Limits(model_calls=7, tool_calls=4, reserve_model_calls=3)
        batch = self.prepare()
        for plan in batch.plans.values():
            for _ in range(2):
                plan.context.reserve('model')
        self.assertEqual(self.context.counts['model_calls'], 4)
        with self.assertRaises(ExecutionError):
            batch.plans['investigation'].context.reserve('model', terminal=True)
        self.assertEqual(self.context.counts['model_calls'], 4)
        for _ in range(3):
            self.context.reserve('model', terminal=True)
        self.assertEqual(self.context.counts['model_calls'], 7)

    def test_insufficient_budget_does_not_create_or_charge_branches(self):
        self.context.limits = Limits(model_calls=6, tool_calls=4, reserve_model_calls=3)
        with self.assertRaises(ExecutionError) as error:
            self.prepare()
        self.assertEqual(error.exception.code, 'BUDGET_EXCEEDED')
        self.assertEqual(self.context.counts['model_calls'], 0)
        self.assertEqual(self.context.counts['tool_calls'], 0)

    def test_mismatched_executor_cannot_charge_a_different_context(self):
        other = RunContext(scope=self.ticket.scope)
        request = CollectionRequest(self.tasks[0], self.objective(self.tasks[0]), 'programmatic', 0)
        with self.assertRaisesRegex(ValueError, 'shared executor mismatch'):
            self.runner.collect(request, executor=self.executors['investigation'], context=other,
                                event_log=[])
        self.assertEqual(other.counts['tool_calls'], 0)
        self.assertEqual(self.context.counts['tool_calls'], 0)
