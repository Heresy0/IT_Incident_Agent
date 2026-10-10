"""Actual graph/executor regressions; scripted verdicts are not model-quality scores."""
import json
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from agents.scripted import scripted_models, selector
from evidence.render import render
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits, RunContext, activate
from tools.executor import ToolExecutor
from workflow.coordinator import collaborate
from workflow.task_checks import completion
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class MetricDiagnosis:
    def __init__(self):
        self.histories = []

    def invoke(self, messages):
        self.histories.append(messages)
        data = json.loads(messages[1].content)['input']
        metric = next(e for e in data['evidence'] if 'metric' in e['payload'])
        refs = [selector(metric, 'value')]
        return json_response({'findings': [{'statement': '依赖指标观测', 'refs': refs}],
            'hypotheses': [{'cause': '依赖异常可能与请求失败有关', 'support_refs': refs}],
            'recommended_actions': [{'action': '人工核对依赖状态', 'condition': '已独立确认依赖故障',
                'expected_result': '请求恢复', 'risk': '误判可能延误处理', 'requires_approval': True}]})


def models_with_checks(provider, *, rework=False, invalid_args=None):
    models = scripted_models()
    window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
    checks = [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['dependency_error_rate']}},
              {'tool': 'get_service_logs', 'args': invalid_args or {**window, 'category': 'dependency'}}]

    def schedule(response, _, calls):
        data = json.loads(response.content)
        if calls == 1 or invalid_args:
            data['tasks'][0].update(goal='核查依赖错误指标和依赖日志', checks=checks)
        return json_response(data)

    models['supervisor'] = AlterOutput(models['supervisor'], schedule)
    models['investigation'] = AlterOutput(models['investigation'], lambda response, *_:
        response.model_copy(update={'tool_calls': [c for c in response.tool_calls
            if c['name'] != 'get_service_logs']}) if response.tool_calls else response)
    models['diagnosis'] = MetricDiagnosis()

    def review(response, messages, _):
        data = json.loads(messages[1].content)['input']
        value = json.loads(response.content)
        if rework and data['task_completion_gaps'] and data['budget']['can_collect'] and not data['rework_used']:
            h = next(a for a in value['assessments'] if a['target_id'] == 'H1')
            h['verdict'] = 'uncertain'
            value['request_evidence'] = {'hypothesis_id': 'H1', 'evidence_ids': h['evidence_ids'],
                'missing_observation': '缺少当前依赖日志', 'proposed_check': '核查依赖失败事件',
                'expected_value': '区分实际依赖失败与仅有指标异常', 'target_role': 'investigation',
                'checks': [checks[1]]}
        return json_response(value)

    models['reviewer'] = AlterOutput(models['reviewer'], review)
    return models


class TaskCoverageGraphTests(unittest.TestCase):
    def test_metric_only_task_cannot_pass_declared_log_goal_even_if_reviewer_approves(self):
        provider = FixtureProvider('case_001', P)
        models = models_with_checks(provider)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('partial', 'needs_information'))
        task = result['task_results'][0]
        self.assertEqual((task['execution_status'], task['status']), ('completed', 'partial'))
        self.assertEqual(task['completion']['status'], 'checks_incomplete')
        self.assertEqual([c['status'] for c in task['completion']['checks']], ['satisfied', 'not_executed'])
        self.assertFalse(task['completion']['goal_verified'])
        self.assertIn('任务目标未满足', '\n'.join(result['output']['missing_information']))
        self.assertIn('声明的检查尚未满足', render(result))
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        for model in (models['diagnosis'], models['reviewer'].model):
            data = json.loads(model.histories[0][1].content)['input']
            self.assertEqual(data['task_results'][0]['goal'], task['goal'])
            self.assertTrue(data['task_completion_gaps'])
        data = json.loads(models['reviewer'].model.histories[0][1].content)['input']
        self.assertEqual(data['ticket']['symptoms'], provider.ticket().symptoms)

    def test_one_rework_executes_missing_check_and_closes_original_task_gap(self):
        provider = FixtureProvider('case_001', P)
        models = models_with_checks(provider, rework=True)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['rework_rounds'], 1)
        self.assertEqual(len(result['reviews']), 2)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(result['task_results'][0]['completion']['status'], 'checks_satisfied')
        self.assertFalse(any('任务目标未满足' in g for g in result['output']['missing_information']))
        actual = [e for e in result['events'] if e['type'] == 'tool_result' and e['name'] == 'get_service_logs']
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]['execution_source'], 'approved_rework')
        self.assertEqual(json.loads(models['reviewer'].model.histories[-1][1].content)['input']['task_completion_gaps'], [])

    def test_declared_checks_do_not_add_calls_when_initial_leaf_already_satisfies_them(self):
        provider = FixtureProvider('case_001', P)
        models = models_with_checks(provider)
        models['investigation'] = scripted_models()['investigation']
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['task_results'][0]['completion']['status'], 'checks_satisfied')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (6, 2))

    def test_small_budget_preserves_gap_without_extra_calls(self):
        provider = FixtureProvider('case_001', P)
        result = collaborate(models_with_checks(provider, rework=True), provider, P,
            limits=Limits(model_calls=6, tool_calls=2, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual((result['status'], result['rework_rounds']), ('partial', 0))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (6, 1))
        self.assertTrue(result['task_results'][0]['completion']['missing_information'])

    def test_invalid_task_scope_is_rejected_before_tool_attempt(self):
        provider = FixtureProvider('case_001', P)
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        models = models_with_checks(provider, invalid_args={**window, 'category': 'dependency', 'user_id': 'forged'})
        result = collaborate(models, provider, P)
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertTrue(any(e.get('reason') == 'INVALID_TASK_CHECK' for e in result['events']))

    def test_legacy_unscoped_goal_is_explicitly_not_verified(self):
        result = collaborate(scripted_models(), FixtureProvider('case_001', P), P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['task_results'][0]['completion']['status'], 'not_specified')
        self.assertIn('程序未核验目标完成', render(result))


class CheckLedgerTests(unittest.TestCase):
    def setUp(self):
        self.provider = FixtureProvider('case_001', P)
        scope = self.provider.ticket().scope
        self.executor = ToolExecutor(self.provider, P, scope, RunContext(scope=scope))
        self.window = scope.model_dump(mode='json', include={'start', 'end'})

    def check(self, tool, **args):
        parsed = self.executor.validate_args(tool, args)
        return {'tool': tool, 'args': parsed.model_dump(mode='json')}

    def read(self, check):
        with activate(self.executor.context):
            return self.executor.execute(check['tool'], check['args'])

    def test_subset_metrics_reuse_and_missing_metric_samples(self):
        requested = self.check('get_service_metrics', **self.window,
            metrics=['dependency_error_rate', 'request_latency'])
        original = self.provider.query
        def missing(*args, **kwargs):
            rows, truncated = original(*args, **kwargs)
            return [row for row in rows if row['metric'] != 'request_latency'], truncated
        with patch.object(self.provider, 'query', side_effect=missing):
            self.read(requested)
        self.assertEqual(completion([requested], self.executor)['checks'][0]['status'], 'missing_samples')
        subset = self.check('get_service_metrics', **self.window, metrics=['dependency_error_rate'])
        self.assertEqual(completion([subset], self.executor)['status'], 'checks_satisfied')
        self.assertEqual(self.executor.context.counts['tool_calls'], 1)

    def test_restrictive_logs_cannot_cover_unfiltered_goal(self):
        requested = self.check('get_service_logs', **self.window, category='dependency')
        filtered = self.check('get_service_logs', **self.window, category='dependency', level='ERROR')
        self.read(filtered)
        self.assertEqual(completion([requested], self.executor)['checks'][0]['status'], 'not_executed')
        self.read(requested)
        self.assertEqual(completion([requested], self.executor)['status'], 'checks_satisfied')
        absent = self.check('get_service_logs', **self.window, category='dependency', error_code='ABSENT')
        self.assertEqual(completion([absent], self.executor)['checks'][0]['status'], 'empty')

    def test_empty_truncated_and_failed_queries_never_satisfy_goal(self):
        for variant in ('empty', 'truncated', 'failed'):
            self.setUp()
            requested = self.check('get_service_logs', **self.window, category='dependency')
            original = self.provider.query
            def query(*args, **kwargs):
                if variant == 'failed':
                    raise ProviderError('PROVIDER_UNAVAILABLE', False)
                rows, _ = original(*args, **kwargs)
                return ([], False) if variant == 'empty' else (rows, True)
            with self.subTest(variant=variant), patch.object(self.provider, 'query', side_effect=query):
                self.read(requested)
                self.assertEqual(completion([requested], self.executor)['checks'][0]['status'], variant)

    def test_duplicate_attempt_does_not_replace_successful_coverage(self):
        check = self.check('get_service_logs', **self.window, category='dependency')
        self.read(check)
        self.assertEqual(self.read(check).error.code, 'DUPLICATE_TOOL')
        self.assertEqual(completion([check], self.executor)['status'], 'checks_satisfied')
        self.assertEqual(self.executor.context.counts['tool_calls'], 1)

    def test_wider_read_without_samples_in_required_window_is_not_coverage(self):
        broad = self.check('get_service_metrics', **self.window, metrics=['dependency_error_rate'])
        original = self.provider.query
        def outside(*args, **kwargs):
            rows, _ = original(*args, **kwargs)
            return [min(rows, key=lambda row: row['timestamp'])], False
        with patch.object(self.provider, 'query', side_effect=outside):
            self.read(broad)
        requested = self.check('get_service_metrics', start=(datetime.fromisoformat(self.window['end']) - timedelta(minutes=1)).isoformat(),
            end=self.window['end'], metrics=['dependency_error_rate'])
        self.assertEqual(completion([requested], self.executor)['checks'][0]['status'], 'missing_samples')


if __name__ == '__main__':
    unittest.main()
