"""Free reconstructed response failures from the latest live comparison.

Uses the real graph/executor, development fixtures and control responses. No raw
rejected JSON, independent gold, credentials, network or paid model is required.
"""
import copy
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from agents.contracts import SupervisorSelection
from agents.investigation import investigate
from agents.scripted import scripted_models, selector
from agents.single import single_agent
from evidence.completion import diagnosis_gaps
from evidence.render import render
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from workflow.coordinator import Collaboration, collaborate
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class FreshLedgerLeaf:
    def __init__(self): self.histories = []
    def bind_tools(self, tools): return self
    def invoke(self, messages):
        self.histories.append(list(messages))
        data = json.loads(messages[1].content)
        checks = data['objective']['required_checks']
        coverage = data['objective']['check_completion']
        if len(self.histories) == 1:
            assert all(c['status'] == 'not_executed' for c in coverage['checks'])
            return AIMessage(content='', tool_calls=[{'name': c['tool'], 'args': c['args'],
                'id': f'fresh-{n}', 'type': 'tool_call'} for n, c in enumerate(checks)])
        evidence = [e for m in messages if isinstance(m, ToolMessage) for e in json.loads(m.content)['evidence']]
        return json_response({'findings': [{'statement': '来源样本', 'refs': [selector(e, 'value')]}
            for e in evidence if 'metric' in e['payload']], 'missing_information': coverage['missing_information']})


class SingleReportModel:
    def __init__(self, *, missing=(), no_facts=False):
        self.missing, self.no_facts = missing, no_facts
        self.histories = []
    def bind_tools(self, tools): return self
    def invoke(self, messages):
        self.histories.append(list(messages))
        data = json.loads(messages[1].content)
        evidence = [e for m in messages if isinstance(m, ToolMessage) for e in json.loads(m.content)['evidence']]
        if not evidence and not self.no_facts:
            scope = data['ticket']['scope']
            return AIMessage(content='', tool_calls=[{'name': 'get_service_logs',
                'args': {k: scope[k] for k in ('start', 'end')} | {'category': 'dependency'},
                'id': 'diagnosis-source', 'type': 'tool_call'}])
        refs = [selector(evidence[0], 'message')] if evidence else []
        value = {'findings': [{'statement': '来源观测', 'refs': refs}] if refs else [],
            'hypotheses': [{'cause': '依赖调用失败可能导致请求失败', 'support_refs': refs}],
            'recommended_actions': [{'action': '人工核查依赖连通性', 'condition': '依赖错误持续发生',
                'expected_result': '确认故障范围后处置并检查请求错误恢复', 'risk': '只读核查，后续变更需独立审批',
                'requires_approval': True}]}
        if data['ticket']['purpose'] == 'status_check':
            value.update(hypotheses=[], recommended_actions=[])
        for field in self.missing: value[field] = []
        return json_response(value)


class LiveProtocolRegressions(unittest.TestCase):
    def test_each_leaf_request_receives_fresh_checks_and_collection_without_mutating_objective(self):
        provider = FixtureProvider('case_001', P)
        scope = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        checks = [
            {'tool': 'get_service_logs', 'args': {**scope, 'category': 'dependency', 'error_code': 'ABSENT'}},
            {'tool': 'get_service_metrics', 'args': {**scope, 'metrics': ['dependency_error_rate']}}]
        objective = {'required_checks': checks, 'check_completion': {'status': 'outdated'},
                     'collection': [{'name': 'old'}]}
        original = copy.deepcopy(objective)
        model = FreshLedgerLeaf()
        result = investigate(model, provider, P, objective=objective)
        fresh = json.loads(model.histories[1][1].content)
        coverage = fresh['objective']['check_completion']
        self.assertEqual(coverage['status'], 'checks_completed_empty')
        self.assertTrue(coverage['query_complete'])
        self.assertFalse(coverage['goal_verified'])
        self.assertEqual([c['status'] for c in coverage['checks']], ['empty', 'satisfied'])
        self.assertEqual(fresh['objective']['query_state_source'], 'server_read_ledger')
        self.assertEqual([c['status'] for c in fresh['collection']], ['empty', 'ok'])
        self.assertEqual(fresh['objective']['collection'], fresh['collection'])
        self.assertTrue(coverage['limitations'])
        self.assertEqual(result['output']['missing_information'], [])
        self.assertEqual(objective, original)
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (2, 2))

    def test_failed_and_truncated_reads_refresh_as_limits_not_success(self):
        for variant in ('failed', 'truncated'):
            provider = FixtureProvider('case_001', P)
            scope = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
            model = FreshLedgerLeaf()
            original = provider.query
            def query(*args, **kwargs):
                if variant == 'failed': raise ProviderError('PROVIDER_UNAVAILABLE', False)
                rows, _ = original(*args, **kwargs)
                return rows, True
            with self.subTest(variant=variant), patch.object(provider, 'query', side_effect=query):
                result = investigate(model, provider, P, objective={'required_checks': [{
                    'tool': 'get_service_metrics', 'args': {**scope, 'metrics': ['dependency_error_rate']}}]})
                coverage = json.loads(model.histories[-1][1].content)['objective']['check_completion']
                self.assertEqual(coverage['checks'][0]['status'], variant)
                self.assertFalse(coverage['query_complete'])
                self.assertTrue(coverage['missing_information'])
                self.assertNotEqual(result['status'], 'completed')
                self.assertEqual(result['run_summary']['tool_calls'], 1)

    def test_non_dispatch_redundant_tasks_are_ignored_without_spending_correction(self):
        models = scripted_models()
        def conflict(response, messages, calls):
            value = json.loads(response.content)
            if calls == 2:
                value.update(action='diagnose', tasks=[{'role': 'investigation', 'goal': '旧查询'}])
            return json_response(value)
        models['supervisor'] = AlterOutput(models['supervisor'], conflict)
        result = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['repairs'], 0)
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (6, 2))
        self.assertTrue(any(e.get('code') == 'UNEXPECTED_TASKS' for e in result['events']))
        self.assertEqual(len(result['task_results']), 1, 'Conflicting task was not executed')
        for model in (models['supervisor'].model, models['diagnosis'], models['reviewer']):
            payload = json.loads(model.histories[-1][1].content)['input']
            self.assertEqual(payload.get('tasks', payload.get('task_results'))[0]['query_state_source'], 'server_read_ledger')
            self.assertFalse(payload.get('tasks', payload.get('task_results'))[0]['model_summary_verified'])

    def test_explicit_non_dispatch_action_is_preserved_and_attached_tasks_never_execute(self):
        stops = {'diagnose': 'DIAGNOSIS_WITHOUT_OBSERVATIONS', 'request_info': 'NEEDS_INFORMATION',
                 'escalate': 'ESCALATED', 'finish': 'UNREVIEWED_FINISH'}
        for action, stop in stops.items():
            models = scripted_models()
            models['supervisor'] = AlterOutput(models['supervisor'], lambda *_: json_response({
                'action': action, 'reason': '重建动作与任务冲突',
                'tasks': [{'role': 'investigation', 'goal': '不应执行的任务'}]}))
            with self.subTest(action=action):
                result = collaborate(models, FixtureProvider('case_001', P), P)
                self.assertEqual(result['run_summary']['termination_reason'], stop)
                self.assertEqual((result['repairs'], result['run_summary']['model_calls'],
                    result['run_summary']['tool_calls']), (0, 2 if action == 'diagnose' else 1, 0))
                self.assertEqual(result['task_results'], [])

    def test_dispatch_without_tasks_also_uses_bounded_correction(self):
        models = scripted_models()
        models['supervisor'] = AlterOutput(models['supervisor'], lambda response, _, calls:
            json_response({'action': 'dispatch', 'reason': '缺少任务'}) if calls == 1 else response)
        result = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['run_summary']['tool_calls'], 2)

    def test_single_missing_sections_keeps_sources_and_returns_partial_without_extra_calls(self):
        for missing in (('hypotheses',), ('recommended_actions',), ('hypotheses', 'recommended_actions')):
            model = SingleReportModel(missing=missing)
            with self.subTest(missing=missing):
                result = single_agent(model, FixtureProvider('case_001', P), P)
                self.assertEqual((result['status'], result['business_result']), ('partial', 'needs_information'))
                self.assertEqual(result['run_summary']['termination_reason'], 'DIAGNOSIS_INCOMPLETE')
                self.assertTrue(result['output']['findings'])
                self.assertEqual(result['repairs'], 0)
                self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (2, 1))
                self.assertEqual(len(diagnosis_gaps(result['output'], 'diagnosis')), len(missing))
                event = next(e for e in result['events'] if e['type'] == 'diagnosis_gate')
                self.assertTrue(set(event['missing_information']) <= set(result['output']['missing_information']))
                ended = next(e for e in result['events'] if e['type'] == 'task_completed')
                self.assertEqual((ended['status'], ended['reason']), ('partial', 'DIAGNOSIS_INCOMPLETE'))
                self.assertEqual(json.loads(model.histories[0][1].content)['completion_requirements']['required_sections'],
                    ['findings', 'hypotheses', 'recommended_actions'])

    def test_single_without_current_fact_cannot_complete(self):
        result = single_agent(SingleReportModel(no_facts=True), FixtureProvider('case_001', P), P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['termination_reason'], 'DIAGNOSIS_INCOMPLETE')
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        self.assertIn('故障诊断缺少可复核的当前观测。', result['output']['missing_information'])

    def test_complete_single_is_still_unreviewed_and_status_check_does_not_require_cause(self):
        for purpose, business in [('diagnosis', 'diagnosis_available'), ('status_check', 'status_checked')]:
            provider = FixtureProvider('case_001', P)
            provider._data['ticket']['purpose'] = purpose
            model = SingleReportModel()
            with self.subTest(purpose=purpose):
                result = single_agent(model, provider, P)
                self.assertEqual((result['status'], result['business_result']), ('completed', business))
                self.assertEqual(result['review_status'], 'not_performed')
                self.assertEqual(result['purpose'], purpose)
                self.assertEqual(diagnosis_gaps(result['output'], purpose), [])
                if purpose == 'status_check':
                    self.assertEqual(result['output']['hypotheses'], [])
                    self.assertEqual(result['output']['recommended_actions'], [])
                    self.assertIn('服务状态核查报告', render(result))


if __name__ == '__main__':
    unittest.main()
