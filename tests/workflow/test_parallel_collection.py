"""Actual graph concurrency and harness boundaries, with zero model/network fees."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from runtime.branch import BranchContext
from runtime.context import ExecutionError, Limits, RunContext
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import collaborate
from workflow.execution import IncidentAdapter
from workflow.graph import WORKFLOW_VERSION


class GatedModel:
    def __init__(self, model, barrier):
        self.model, self.barrier, self.first = model, barrier, True

    def bind_tools(self, tools):
        return GatedModel(self.model.bind_tools(tools), self.barrier)

    def invoke(self, messages):
        if self.first:
            self.first = False
            self.barrier.wait(timeout=3)
        return self.model.invoke(messages)


class FailingModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        raise ExecutionError('NETWORK_ERROR', 'model')


def optional_knowledge_models():
    models = scripted_models()
    def schedule(response, _, calls):
        if calls == 1:
            value = json.loads(response.content)
            value['tasks'].append({'role': 'knowledge', 'goal': '检索可选历史线索'})
            return json_response(value)
        return response
    models['supervisor'] = AlterOutput(models['supervisor'], schedule)
    return models


class ParallelCollectionTests(unittest.TestCase):
    def test_graph_overlaps_roles_joins_once_and_preserves_rework(self):
        models = scripted_models()
        barrier = Barrier(2)
        # Only the first task is synchronized: reviewer follow-up remains serial.
        class FirstTaskGate(GatedModel):
            def bind_tools(self, tools):
                if not self.first:
                    return self.model.bind_tools(tools)
                self.first = False
                return GatedModel(self.model.bind_tools(tools), self.barrier)
        for role in ('investigation', 'knowledge'):
            models[role] = FirstTaskGate(models[role], barrier)
        result = collaborate(models, FixtureProvider('case_003', P), P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['collection_execution']['parallel_batches'], 1)
        events = result['events']
        starts = [e for e in events if e['type'] == 'call_start' and e.get('kind') == 'task']
        ends = [e for e in events if e['type'] == 'call_end' and e.get('kind') == 'task']
        initial = starts[:2]
        self.assertEqual({e['name'] for e in initial}, {'investigation', 'knowledge'})
        span_ends = {e['span_id']: e for e in ends}
        self.assertLess(max(e['seq'] for e in initial), min(span_ends[e['span_id']]['seq'] for e in initial))
        joined = [e for e in events if e['type'] == 'parallel_collection_joined']
        self.assertEqual(len(joined), 1)
        self.assertGreater(joined[0]['seq'], max(span_ends[e['span_id']]['seq'] for e in initial))
        self.assertTrue(all(e['seq'] > joined[0]['seq'] for e in events
                            if e['type'] == 'call_start' and e.get('name') == 'diagnosis'))
        self.assertEqual([t['role'] for t in result['task_results']], ['investigation', 'knowledge', 'investigation'])
        self.assertEqual(result['rework_rounds'], 1)
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (12, 5))
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        known_spans = {e['span_id'] for e in events if e['type'] == 'call_start'}
        self.assertTrue(all(e['parent_span_id'] in known_spans for e in events
            if e['type'] == 'call_start' and e.get('kind') != 'workflow'))
        # Attribute calls by actual enclosing task span, not global count deltas.
        parent = {e['span_id']: e['parent_span_id'] for e in events if e['type'] == 'call_start'}
        for task in result['task_results']:
            task_span = next(e['span_id'] for e in events
                if e['type'] == 'task_started' and e['task_id'] == task['task_id'])
            for kind in ('model', 'tool'):
                calls = [e for e in events if e['type'] == 'call_start' and e.get('kind') == kind
                         and parent[e['span_id']] == task_span]
                self.assertEqual(len(calls), task[kind + '_calls'])

    def test_knowledge_failure_keeps_current_observations_and_runs_review(self):
        models = optional_knowledge_models()
        models['knowledge'] = FailingModel()
        result = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertTrue(any(e['kind'] == 'observation' for e in result['evidence']))
        task = next(t for t in result['task_results'] if t['role'] == 'knowledge')
        self.assertEqual(task['termination_reason'], 'NETWORK_ERROR')
        self.assertEqual(task['model_calls'], 1)
        self.assertEqual(task['tool_calls'], 0)
        self.assertTrue(result['drafts'] and result['reviews'])
        self.assertIn('NETWORK_ERROR', '\n'.join(result['output']['missing_information']))
        self.assertEqual(sum(e['type'] == 'parallel_collection_joined' for e in result['events']), 1)

    def test_failed_observation_branch_cannot_diagnose_from_history_alone(self):
        models = scripted_models()
        models['investigation'] = FailingModel()
        result = collaborate(models, FixtureProvider('case_003', P), P)
        self.assertTrue(result['evidence'])
        self.assertFalse(any(e['kind'] == 'observation' for e in result['evidence']))
        self.assertEqual(result['drafts'], [])
        self.assertEqual(result['reviews'], [])
        self.assertEqual(result['run_summary']['termination_reason'], 'NETWORK_ERROR')

    def test_unexpected_branch_exception_is_sanitized_and_join_still_finishes(self):
        class BrokenBinding:
            def bind_tools(self, tools):
                raise RuntimeError('synthetic-secret-canary')
        models = optional_knowledge_models()
        models['knowledge'] = BrokenBinding()
        result = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertNotIn('synthetic-secret-canary', json.dumps(result))
        task = next(t for t in result['task_results'] if t['role'] == 'knowledge')
        self.assertEqual(task['termination_reason'], 'INTERNAL_ERROR')
        self.assertEqual(task['model_calls'], 0)
        self.assertTrue(result['drafts'] and result['reviews'])

    def test_serial_comparison_switch_preserves_original_counts(self):
        result = collaborate(scripted_models(), FixtureProvider('case_003', P), P, parallel_collection=False)
        self.assertEqual(result['collection_execution']['mode'], 'serial')
        self.assertEqual(result['collection_execution']['parallel_batches'], 0)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (12, 5))

    def test_single_role_and_low_tool_budget_remain_serial(self):
        for case, limits in [('case_001', Limits(model_calls=6, tool_calls=2, reserve_model_calls=3)),
                             ('case_003', Limits(model_calls=16, tool_calls=2, reserve_model_calls=3))]:
            result = collaborate(scripted_models(), FixtureProvider(case, P), P, limits=limits)
            self.assertEqual(result['collection_execution']['parallel_batches'], 0)
            self.assertLessEqual(result['run_summary']['tool_calls'], limits.tool_calls)
            self.assertFalse(any(e['type'] == 'parallel_collection_started' for e in result['events']))

    def test_branch_caps_preserve_global_finalization_reserve_and_unused_budget(self):
        parent = RunContext(limits=Limits(model_calls=7, tool_calls=4, reserve_model_calls=3))
        a, b = [BranchContext(parent, model_calls=2, tool_calls=2) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda branch: [branch.reserve('model') for _ in range(2)], (a, b)))
        self.assertEqual(parent.counts['model_calls'], 4)
        with self.assertRaises(ExecutionError):
            a.reserve('model', terminal=True)
        self.assertEqual(parent.counts['model_calls'], 4)
        for _ in range(3):
            parent.reserve('model', terminal=True)
        self.assertEqual(parent.counts['model_calls'], 7)
        state = {'repairs': 0}
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(lambda branch: branch.claim_repair(state), (a, b))), 1)
        self.assertEqual(state['repairs'], 1)
        # Allocating a branch does not charge imaginary model calls or tools.
        fresh = RunContext(limits=Limits(model_calls=10, tool_calls=4, reserve_model_calls=3))
        child = BranchContext(fresh, model_calls=2, tool_calls=2)
        child.reserve('model')
        child.add_usage({'input_tokens': 10, 'output_tokens': 3})
        self.assertEqual((fresh.counts['model_calls'], child.counts['model_calls']), (1, 1))
        self.assertEqual(fresh.tokens, child.tokens)
        self.assertEqual(fresh.stop_reason, '')

    def test_metadata_and_result_use_the_same_workflow_version(self):
        result = collaborate(scripted_models(), FixtureProvider('case_001', P), P)
        metadata = IncidentAdapter(None).metadata({'model_budget': 6, 'tool_budget': 2,
                                                  'execution_mode': 'scripted_control_only'})
        self.assertEqual(result['workflow_version'], WORKFLOW_VERSION)
        self.assertEqual(metadata['workflow_version'], WORKFLOW_VERSION)


if __name__ == '__main__':
    unittest.main()
