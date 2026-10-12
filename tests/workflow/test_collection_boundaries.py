"""Executed retrieval plans, early dispatch validation and independent corrections."""
import copy
import json
import unittest
from unittest.mock import patch

from agents.contracts import SupervisorSelection
from agents.protocol import corrections_within_policy, ROLE_REPAIR_POLICY
from agents.scripted import scripted_models
from langchain_core.messages import AIMessage
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits, activate, ExecutionError
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate


class Replies:
    def __init__(self, values):
        self.values, self.histories = iter(values), []
    def invoke(self, messages):
        self.histories.append(list(messages))
        value = next(self.values)
        return AIMessage(content=value if isinstance(value, str) else json.dumps(value))


class CollectionBoundariesTests(unittest.TestCase):
    def knowledge_plan(self, c):
        return c.supervisor_decision(SupervisorSelection(action='dispatch', reason='Find context', tasks=[{
            'role': 'knowledge', 'goal': 'Search useful runbooks', 'need_ids': ['N3'],
            'checks': [{'tool': 'search_runbooks', 'args': {'query': 'pool', 'category': 'resource'}}],
            'expected_value': 'Provide historical clues, not proof of the current cause'}])).tasks[0]

    def collect(self, c, task):
        objective = c.task_objective(task, False)
        result = c.collect_task(task, objective, c.executors['knowledge'], c.context, c.events, 4)
        c.record_task(task, objective, result)
        return result

    def test_knowledge_executes_registered_search_with_no_model_or_correction(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_003', P), P)
        task = self.knowledge_plan(c)
        with patch.object(c.models['knowledge'], 'invoke', side_effect=AssertionError('No knowledge LLM')):
            result = self.collect(c, task)
            reused = self.collect(c, task)
        self.assertEqual(result['task_counts']['tool_calls'], 1)
        self.assertEqual(reused['task_counts']['tool_calls'], 0)
        self.assertEqual(c.context.counts['model_calls'], 0)
        self.assertEqual(c.repairs['repairs'], 0)
        self.assertTrue(c.evidence)
        self.assertEqual(result['output']['findings'], [])
        self.assertEqual(result['output']['tentative_hypotheses'], [])
        self.assertEqual(c.evidence_needs['N3'].status, 'pending')
        self.assertTrue(all(e['role'] == 'knowledge' for e in c.events if e['type'] == 'tool_result'))

    def test_search_failure_is_preserved_without_retry_or_claimed_empty_success(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_003', P), P)
        task = self.knowledge_plan(c)
        with patch.object(c.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')) as query:
            first, second = self.collect(c, task), self.collect(c, task)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(second['status'], 'partial')
        self.assertIn('查询失败', json.dumps(second['output'], ensure_ascii=False))
        self.assertEqual(c.task_results()[0]['completion']['checks'][0]['status'], 'failed')
        self.assertEqual(c.repairs['repairs'], 0)

    def test_two_rework_tasks_are_corrected_before_dispatch_and_then_one_query_runs(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
                          limits=Limits(model_calls=16, tool_calls=8))
        c.phase, c.pending = 'rework', {'need_id': 'N2', 'checks': [], 'planning_required': True,
                                      'missing_observation': 'Current load', 'expected_value': 'Distinguish causes',
                                      'challenge_id': 'C1', 'evidence_ids': [], 'target_role': 'investigation'}
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        task = {'role': 'investigation', 'goal': 'Current load', 'need_ids': ['N2'],
                'checks': [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu']}}],
                'expected_value': 'Distinguish causes'}
        knowledge = {'role': 'knowledge', 'goal': 'Find clues', 'need_ids': ['N2'],
                     'checks': [{'tool': 'search_runbooks', 'args': {'query': 'pool'}}],
                     'expected_value': 'Find clues'}
        model = Replies([{'action': 'dispatch', 'reason': 'Collect', 'tasks': [task, knowledge]},
                         {'action': 'dispatch', 'reason': 'Choose one', 'tasks': [task]}])
        c.models['supervisor'] = model
        with activate(c.context):
            decision = c.ask('supervisor', SupervisorSelection, {}, validate=c.supervisor_decision)
            self.assertEqual(c.context.counts['tool_calls'], 0)
            c.prepare_dispatch(decision)
            c.execute_task('investigation')
        self.assertEqual(len(decision.tasks), 1)
        self.assertEqual(c.context.counts['tool_calls'], 1)
        self.assertEqual(c.repairs['role_repairs'], {'supervisor': 1})
        schema = json.loads(model.histories[0][1].content)['output_schema']
        self.assertEqual(schema['properties']['tasks']['maxItems'], 1)
        self.assertIn('补查只允许一个任务', model.histories[1][-1].content)

    def test_all_three_model_roles_can_correct_once_without_stealing_allowances(self):
        models = planned_models()
        for role in ('supervisor', 'diagnosis', 'reviewer'):
            models[role] = AlterOutput(models[role], lambda response, messages, calls:
                AIMessage(content='invalid JSON') if calls == 1 else response)
        report = collaborate(models, FixtureProvider('case_001', P), P,
                             limits=Limits(model_calls=12, tool_calls=4))
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['run_summary']['model_calls'], 7)
        self.assertEqual(report['repair_policy'], ROLE_REPAIR_POLICY)
        self.assertEqual(report['repair_counts'], {'supervisor': 1, 'diagnosis': 1, 'reviewer': 1})
        self.assertTrue(corrections_within_policy(report))
        bad = copy.deepcopy(report)
        bad['events'].append({'type': 'validation_repair', 'role': 'knowledge', 'repair_role': 'knowledge'})
        self.assertFalse(corrections_within_policy(bad))
        bad = copy.deepcopy(report)
        bad['repair_counts']['diagnosis'] = 0
        self.assertFalse(corrections_within_policy(bad))

    def test_role_correction_does_not_bypass_total_call_limit(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
                          limits=Limits(model_calls=1))
        model = Replies(['invalid JSON', {'action': 'request_info', 'reason': 'Need information'}])
        c.models['supervisor'] = model
        with activate(c.context), self.assertRaises(ExecutionError):
            c.ask('supervisor', SupervisorSelection, {}, terminal=True, validate=c.supervisor_decision)
        self.assertEqual(c.context.counts['model_calls'], 1)
        self.assertEqual(len(model.histories), 1)
        self.assertFalse(c.context.claim_repair(c.repairs, repair_role='supervisor'))

    def test_supervisor_plans_but_cannot_overwrite_review_owned_assessments(self):
        models = planned_models()
        report = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertEqual(report['status'], 'completed')
        first = json.loads(models['supervisor'].model.histories[0][1].content)
        self.assertNotIn('need_updates', first['output_schema']['properties'])
        self.assertNotIn('EvidenceNeedUpdate', first['output_schema']['$defs'])
        last = report['scheduling_decisions'][-1]['evidence_needs']
        self.assertTrue(all(n['status'] == 'pending' for n in last))
        self.assertTrue(all(n['status'] == 'supported' for n in report['evidence_needs']))
        self.assertEqual(report['collection_exits'][0]['reason'], 'collected_for_review')
        self.assertEqual(report['run_summary']['model_calls'], 4)

    def test_default_graph_merges_parallel_observation_and_knowledge_reads(self):
        models = planned_models()
        def knowledge(response, messages, calls):
            data = json.loads(response.content)
            if data['action'] == 'dispatch':
                data['tasks'].append({'role': 'knowledge', 'goal': 'Find contextual runbook',
                    'need_ids': ['N3'], 'checks': [{'tool': 'search_runbooks', 'args': {'query': 'dependency'}}],
                    'expected_value': 'Historical clues must be checked against current observations'})
            return json_response(data)
        models['supervisor'] = AlterOutput(models['supervisor'], knowledge)
        with patch.object(models['knowledge'], 'invoke', side_effect=AssertionError('No knowledge LLM')), \
                patch.object(models['investigation'], 'invoke', side_effect=AssertionError('No investigation LLM')):
            report = collaborate(models, FixtureProvider('case_001', P), P,
                                 limits=Limits(model_calls=16, tool_calls=8))
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['collection_execution']['parallel_batches'], 1)
        self.assertEqual({t['role'] for t in report['task_results']}, {'investigation', 'knowledge'})
        self.assertTrue(all(t['model_calls'] == 0 for t in report['task_results']))
        self.assertEqual(report['run_summary']['tool_calls'], 3)
        self.assertEqual(report['run_summary']['model_calls'], 4)
        self.assertTrue(any(e.get('role') == 'knowledge' and e['type'] == 'tool_result' for e in report['events']))
