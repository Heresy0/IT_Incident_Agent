"""Free graph/executor scenarios; do not treat scripted verdicts as model quality."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from agents.scripted import ScriptedRole, ScriptedSingle, scripted_models, selector, control_need_assessments
from agents.single import single_agent
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits, RunContext
from tools.executor import ToolExecutor
from workflow.coordinator import collaborate
from workflow.task_checks import completion
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class QueryReuseTests(unittest.TestCase):
    def setUp(self):
        self.provider = FixtureProvider('case_003', P)
        self.scope = self.provider.ticket().scope
        self.events = []
        self.context = RunContext(scope=self.scope, emit=self.events.append)
        self.executor = ToolExecutor(self.provider, P, self.scope, self.context)
        self.window = self.scope.model_dump(mode='json', include={'start', 'end'})

    def read(self, **extra):
        return self.executor.execute('get_service_logs', {**self.window, 'category': 'dependency', **extra})

    def test_complete_empty_superset_is_reused_without_provider_attempt(self):
        with patch.object(self.provider, 'query', wraps=self.provider.query) as query:
            self.assertEqual(self.read().status, 'empty')
            self.assertEqual(self.read(level='ERROR').status, 'empty')
            self.assertEqual(query.call_count, 1)
        self.assertEqual(self.context.counts['tool_calls'], 1)
        self.assertEqual(sum(e['type'] == 'query_reused' for e in self.events), 1)
        check = {'tool': 'get_service_logs', 'args': {**self.window, 'category': 'dependency', 'level': 'ERROR'}}
        self.assertEqual(completion([check], self.executor)['checks'][0]['status'], 'empty')
        coverage = completion([check], self.executor)
        self.assertEqual(coverage['status'], 'checks_completed_empty')
        self.assertTrue(coverage['query_complete'])
        self.assertFalse(coverage['goal_verified'])
        self.assertEqual(coverage['missing_information'], [])
        self.assertTrue(coverage['limitations'])
        self.assertEqual(len(self.executor.check_results), 1, 'Keep the original read as provenance')

    def test_restricted_empty_cannot_replace_broader_query(self):
        self.read(level='ERROR')
        self.read()
        self.assertEqual(self.context.counts['tool_calls'], 2)
        self.assertFalse(self.executor.log_gaps)
        self.assertFalse(any(e['type'] == 'query_reused' for e in self.events))

    def test_failed_or_truncated_query_does_not_prove_empty_superset(self):
        for variant in ('failed', 'truncated'):
            self.setUp()
            with self.subTest(variant=variant), patch.object(self.provider, 'query',
                    side_effect=ProviderError('PROVIDER_UNAVAILABLE') if variant == 'failed' else None,
                    return_value=([], True)):
                self.read()
                self.read(level='ERROR')
                self.assertEqual(self.context.counts['tool_calls'], 2)
                self.assertFalse(any(e['type'] == 'query_reused' for e in self.events))

    def test_category_window_and_permission_boundaries_remain(self):
        self.read(start=(self.scope.start + timedelta(minutes=1)).isoformat())
        self.read(level='ERROR')  # Larger window has not been covered.
        self.read(category='resource')
        self.assertEqual(self.context.counts['tool_calls'], 3)
        denied = self.read(start=(self.scope.start - timedelta(minutes=1)).isoformat())
        self.assertEqual(denied.error.code, 'WINDOW_DENIED')
        self.assertEqual(self.context.counts['tool_calls'], 3)
        other = ToolExecutor(self.provider, P, self.scope, RunContext(scope=self.scope))
        other.execute('get_service_logs', {**self.window, 'category': 'dependency'})
        self.assertEqual(other.context.counts['tool_calls'], 1, 'Run ledgers must stay isolated')

    def test_single_version_tracks_shared_diagnosis_contract_and_tools_remain_unchanged(self):
        result = single_agent(ScriptedSingle(), self.provider, P)
        # Cause levels and action types change the shared schema, not SINGLE_SYSTEM.
        self.assertEqual(result['prompt_version'], '95b20de3c161')
        ex = ToolExecutor(self.provider, P, self.scope, RunContext(scope=self.scope), role='single')
        ex.execute('get_service_logs', {**self.window, 'category': 'dependency'})
        ex.execute('get_service_logs', {**self.window, 'category': 'dependency', 'level': 'ERROR'})
        self.assertEqual(ex.context.counts['tool_calls'], 2)


class FocusedLeaf:
    def __init__(self):
        self.histories = []

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.histories.append(list(messages))
        data = json.loads(messages[1].content)
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return AIMessage(content='', tool_calls=[{'name': c['tool'], 'args': c['args'],
                'id': f'focused-{n}', 'type': 'tool_call'} for n, c in enumerate(data['objective']['required_checks'])])
        facts = []
        for item in [e for r in results for e in r['evidence']][:8]:
            field = 'value' if 'metric' in item['payload'] else 'message'
            facts.append({'statement': '当前来源观测', 'refs': [selector(item, field)]})
        return json_response({'findings': facts, 'tentative_hypotheses': ['应用连接池饱和是候选机制']})


class PoolDiagnosis:
    def invoke(self, messages):
        data = json.loads(messages[1].content)['input']
        log = next(e for e in data['evidence'] if e['payload'].get('error_code') == 'POOL_ACQUIRE_TIMEOUT')
        refs = [selector(log, 'message')]
        return json_response({'findings': [{'statement': '观测连接获取等待', 'refs': refs}],
            'hypotheses': [{'cause': '当前应用连接池饱和', 'support_refs': refs}],
            'recommended_actions': [{'action': '人工核查容量条件后评估调整连接池',
                'condition': '池压力已核实且数据库连接预算允许', 'expected_result': '连接等待降低',
                'risk': '超过数据库容量可能导致拥塞', 'requires_approval': True}]})


class ActionReview:
    def __init__(self, window, *, respond=True, reason=None, invalid=None):
        self.window, self.respond, self.reason, self.invalid = window, respond, reason, invalid
        self.histories = []

    def invoke(self, messages):
        self.histories.append(list(messages))
        data = json.loads(messages[1].content)['input']
        has_pressure = any(e['payload'].get('metric') == 'pool_usage' for e in data['evidence'])
        assessments = [{'target_id': target, 'verdict': 'uncertain' if target == 'A1' and not has_pressure else 'supported',
            'reason': '核对当前来源与建议条件'} for target in data['required_target_ids']]
        result = {'assessments': assessments, 'need_assessments': control_need_assessments(data)}
        if not has_pressure:
            result['missing_information'] = ['建议适用条件缺少当前池压力指标']
            if self.reason:
                result['follow_up_reason'] = self.reason
            elif (len(messages) > 2 and self.respond) or self.invalid:
                ids = [e['evidence_id'] for e in data['evidence'] if e['payload'].get('error_code') == 'POOL_ACQUIRE_TIMEOUT']
                result['request_evidence'] = {'hypothesis_id': 'H1', 'target_id': self.invalid or 'A1',
                    'evidence_ids': ids, 'missing_observation': '建议条件缺少池压力观测',
                    'proposed_check': '核查当前池使用率和等待', 'expected_value': '区分持续池压力与偶发等待',
                    'checks': [{'tool': 'get_service_metrics', 'args': {**self.window, 'metrics': ['pool_usage', 'pool_wait']}}]}
        return json_response(result)


def action_models(provider, **kwargs):
    window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
    models = scripted_models()
    def schedule(response, _, calls):
        if calls == 1:
            return json_response({'action': 'dispatch', 'reason': '先区分池等待与DB负载', 'tasks': [{
                'role': 'investigation', 'goal': '核查连接获取日志与正常对照',
                'expected_value': '连接等待与DB负载对照可区分应用池压力和DB负载异常',
                'checks': [{'tool': 'get_service_logs', 'args': {**window, 'category': 'resource'}},
                           {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu']}}]}]})
        return response
    models['supervisor'] = AlterOutput(models['supervisor'], schedule)
    models['investigation'] = FocusedLeaf()
    models['diagnosis'] = PoolDiagnosis()
    models['reviewer'] = ActionReview(window, **kwargs)
    return models


class InformationGainGraphTests(unittest.TestCase):
    def test_action_gap_can_request_rework_while_supported_hypothesis_stays_supported(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual((result['rework_rounds'], result['repairs']), (1, 0))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (11, 3))
        review = result['reviews'][0]['review']
        self.assertEqual(review['request_evidence']['target_id'], 'A1')
        self.assertEqual(next(a for a in review['assessments'] if a['target_id'] == 'H1')['verdict'], 'supported')
        self.assertEqual(sum(e['type'] == 'review_follow_up_correction' for e in result['events']), 1)
        approved = [e for e in result['events'] if e.get('execution_source') == 'approved_rework' and e['type'] == 'tool_result']
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['query_profile']['metrics'], ['pool_usage', 'pool_wait'])
        objective = json.loads(models['investigation'].histories[0][1].content)['objective']
        self.assertIn('区分', objective['expected_value'])
        second_plan = json.loads(models['supervisor'].model.histories[1][1].content)['input']
        self.assertEqual(second_plan['tasks'][0]['candidate_mechanisms'], ['应用连接池饱和是候选机制'])

    def test_follow_up_is_bounded_when_model_does_not_propose_a_check(self):
        provider = FixtureProvider('case_003', P)
        result = collaborate(action_models(provider, respond=False), provider, P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['rework_rounds'], 0)
        self.assertEqual(result['run_summary']['model_calls'], 7)
        self.assertEqual(sum(e['type'] == 'review_follow_up_correction' for e in result['events']), 1)
        self.assertTrue(result['output']['missing_information'])

    def test_explained_human_only_gap_does_not_trigger_extra_model_call(self):
        provider = FixtureProvider('case_003', P)
        result = collaborate(action_models(provider, reason='需要人工容量实验，登记只读观测无法确认数据库总容量'), provider, P)
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(any(e['type'] == 'review_follow_up_correction' for e in result['events']))

    def test_small_budget_retains_gap_without_futile_review_clarification(self):
        provider = FixtureProvider('case_003', P)
        result = collaborate(action_models(provider), provider, P,
            limits=Limits(model_calls=8, tool_calls=5, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (6, 2))
        self.assertFalse(any(e['type'] == 'review_follow_up_correction' for e in result['events']))

    def test_unknown_or_supported_disputed_target_cannot_authorize_rework(self):
        for target in ('A4', 'H1'):
            provider = FixtureProvider('case_003', P)
            with self.subTest(target=target):
                result = collaborate(action_models(provider, invalid=target), provider, P)
                self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
                self.assertEqual(result['run_summary']['tool_calls'], 2)
                self.assertEqual(result['rework_rounds'], 0)

    def test_same_batch_completed_task_is_skipped_before_starting_leaf(self):
        provider = FixtureProvider('case_001', P)
        models = scripted_models()
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        check = {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}}
        models['supervisor'] = AlterOutput(models['supervisor'], lambda *_: json_response({
            'action': 'dispatch', 'reason': '先取证再检查', 'tasks': [
                {'role': 'investigation', 'goal': '获取依赖观测', 'checks': [check]},
                {'role': 'investigation', 'goal': '另一次同范围日志检查', 'checks': [check]}]}))
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(len(result['task_results']), 1)
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (5, 2))
        self.assertEqual(sum(e['type'] == 'task_reused' for e in result['events']), 1)


if __name__ == '__main__':
    unittest.main()
