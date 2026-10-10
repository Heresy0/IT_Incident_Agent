"""Free response replay of premature final JSON, using the actual graph/tools."""
import json
import unittest
from unittest.mock import patch

from agents.investigation import investigate
from agents.scripted import ScriptedRole, scripted_models, selector
from langchain_core.messages import AIMessage
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from workflow.coordinator import collaborate
from tests.agents.test_incident_investigation import Responses, selected
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_task_checks import MetricDiagnosis, models_with_checks


class PrematureFinal:
    def __init__(self, *, recover=False, role='investigation', only_goal=None):
        self.recover, self.only_goal = recover, only_goal
        self.fallback = ScriptedRole(role)
        self.histories = []
        self.premature = 0

    def bind_tools(self, tools):
        self.fallback = self.fallback.bind_tools(tools)
        return self

    def invoke(self, messages):
        self.histories.append(list(messages))
        objective = json.loads(messages[1].content).get('objective') or {}
        if (self.only_goal is None or objective['goal'] == self.only_goal) and (not self.recover or self.premature == 0):
            self.premature += 1
            # Replay the actual failure's valid empty result: missing requirements
            # were restated as a final answer without any tool call.
            return json_response({'findings': [], 'tentative_hypotheses': [],
                'missing_information': ['get_recent_changes：尚未执行；不能据此认定任务目标已满足。'] * 2})
        return self.fallback.invoke(messages)


class LeafProgressTests(unittest.TestCase):
    def test_recorded_no_tool_response_gets_one_correction_and_no_redispatch(self):
        provider = FixtureProvider('case_003', P)
        models = scripted_models()
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        def first_only(response, _, calls):
            self.assertEqual(calls, 1, 'Supervisor must not retry the same stalled task')
            return json_response({'action': 'dispatch', 'reason': '重放真实失败的调度', 'tasks': [{
                'role': 'investigation',
                'goal': '检查服务的最新更改以确定是否有任何可能导致连接等待增加的配置或部署更改。',
                'checks': [{'tool': 'get_recent_changes', 'args': {**window, 'category': category}}
                           for category in ('deployment', 'configuration')]}]})
        models['supervisor'] = AlterOutput(models['supervisor'], first_only)
        leaf = models['investigation'] = PrematureFinal()
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('partial', 'not_performed'))
        self.assertEqual(result['run_summary']['termination_reason'], 'NO_TOOL_PROGRESS')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (3, 0))
        self.assertEqual(result['supervisor_decisions'], 1)
        self.assertEqual(len(leaf.histories), 2)
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 1)
        self.assertIn('实际调用', leaf.histories[-1][-1].content)
        self.assertTrue(result['task_results'][0]['completion']['missing_information'])
        self.assertFalse(any(e.get('code') == 'DUPLICATE_TASK' for e in result['events']))

    def test_correction_can_execute_tools_then_finish_within_existing_steps(self):
        provider = FixtureProvider('case_001', P)
        models = models_with_checks(provider)
        leaf = models['investigation'] = PrematureFinal(recover=True)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['task_results'][0]['completion']['status'], 'checks_satisfied')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (7, 2))
        self.assertEqual(result['task_results'][0]['model_calls'], 3)
        self.assertEqual(result['repairs'], 0, 'Execution correction does not consume structure-repair allowance')
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 1)
        self.assertTrue(result['output']['findings'])

    def test_short_step_budget_exits_without_a_futile_last_step_tool_request(self):
        provider = FixtureProvider('case_001', P)
        models = models_with_checks(provider)
        models['investigation'] = PrematureFinal(recover=True)
        result = collaborate(models, provider, P, limits=Limits(model_calls=6, tool_calls=2,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['run_summary']['termination_reason'], 'NO_TOOL_PROGRESS')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (2, 0))
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 0)
        self.assertEqual(result['status'], 'partial')

    def test_empty_or_failed_actual_read_is_not_reissued_as_an_unexecuted_check(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                provider = FixtureProvider('case_001', P)
                window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
                args = {**window, 'category': 'dependency', 'error_code': 'ABSENT'}
                model = Responses([selected('get_service_logs', args), json_response({'findings': [],
                    'missing_information': ['没有取得可用日志，保留缺口。']})])
                original = provider.query
                def query(*a, **kw):
                    if failed:
                        raise ProviderError('PROVIDER_UNAVAILABLE', False)
                    return original(*a, **kw)
                with patch.object(provider, 'query', side_effect=query):
                    result = investigate(model, provider, P, objective={'required_checks': [
                        {'tool': 'get_service_logs', 'args': args}]})
                self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (2, 1))
                self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 0)
                self.assertEqual(result['status'], 'partial' if failed else 'completed')

    def test_stalled_later_task_preserves_prior_observations_and_finalizes(self):
        provider = FixtureProvider('case_001', P)
        models = scripted_models()
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        def batch(response, _, calls):
            self.assertEqual(calls, 1)
            return json_response({'action': 'dispatch', 'reason': '先取证再核查变更', 'tasks': [
                {'role': 'investigation', 'goal': '取得当前依赖观测'},
                {'role': 'investigation', 'goal': '补充变更', 'checks': [
                    {'tool': 'get_recent_changes', 'args': window}]}]})
        models['supervisor'] = AlterOutput(models['supervisor'], batch)
        models['investigation'] = PrematureFinal(only_goal='补充变更')
        models['diagnosis'] = MetricDiagnosis()
        result = collaborate(models, provider, P)
        self.assertEqual(result['supervisor_decisions'], 1)
        self.assertEqual(result['task_results'][-1]['termination_reason'], 'NO_TOOL_PROGRESS')
        self.assertEqual((result['status'], result['review_status']), ('partial', 'needs_information'))
        self.assertTrue(result['output']['findings'])
        self.assertEqual(len(result['reviews']), 1)
        self.assertTrue(any(e['type'] == 'finalization_started' and e['reason'] == 'leaf_no_tool_progress'
                            for e in result['events']))

    def test_knowledge_role_uses_same_bounded_correction(self):
        provider = FixtureProvider('case_003', P)
        leaf = PrematureFinal(recover=True, role='knowledge')
        result = investigate(leaf, provider, P, role='knowledge', objective={'required_checks': [
            {'tool': 'search_runbooks', 'args': {'query': 'pool waiting latency database', 'category': 'resource'}}]})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (3, 2))
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 1)

    def test_already_satisfied_checks_can_reuse_evidence_without_new_tools_or_correction(self):
        provider = FixtureProvider('case_001', P)
        models = models_with_checks(provider)
        checked_supervisor = models['supervisor']
        first_checks = []
        def reuse(response, messages, calls):
            data = json.loads(response.content)
            if calls == 1:
                first_checks.extend(data['tasks'][0]['checks'])
            elif calls == 2:
                data = {'action': 'dispatch', 'reason': '复用已取得的观测', 'tasks': [
                    {'role': 'investigation', 'goal': '核对已有来源', 'checks': first_checks}]}
            return json_response(data)
        models['supervisor'] = AlterOutput(checked_supervisor, reuse)
        def leaf(response, messages, _):
            objective = json.loads(messages[1].content)['objective']
            if objective['goal'] == '核对已有来源':
                item = next(e for e in objective['evidence'] if 'metric' in e['payload'])
                return json_response({'findings': [{'statement': '复用指标观测', 'refs': [selector(item, 'value')]}]})
            return response
        models['investigation'] = AlterOutput(ScriptedRole('investigation'), leaf)
        result = collaborate(models, provider, P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['task_results']), 1)
        self.assertEqual(sum(e['type'] == 'task_reused' for e in result['events']), 1)
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 0)


if __name__ == '__main__':
    unittest.main()
