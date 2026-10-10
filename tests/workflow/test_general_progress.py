"""Free graph regressions for generic planning and shared-budget boundaries."""
import json
import time
import unittest

from agents.contracts import ReadOnlyCheck
from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import Collaboration, collaborate
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_information_gain import action_models, FocusedLeaf
from tests.workflow.test_task_checks import MetricDiagnosis


class GeneralProgressTests(unittest.TestCase):
    def test_repeated_empty_request_returns_once_to_planner_and_switches_channel(self):
        for tool in ('get_recent_changes', 'get_service_logs'):
            provider = FixtureProvider('case_001', P)
            window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
            empty = {'tool': tool, 'args': {**window, **({'category': 'resource', 'error_code': 'ABSENT'}
                if tool == 'get_service_logs' else {'category': 'configuration'})}}
            metric = {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['dependency_error_rate']}}
            new = {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}}
            models = scripted_models()
            def schedule(response, messages, calls):
                data = json.loads(response.content)
                if calls < 3:
                    for need in data['evidence_needs']:
                        need.update(status='pending', evidence_ids=[], reason='需要区分故障机制')
                if calls == 3:
                    guidance = json.loads(messages[1].content)['input']['planning_guidance']
                    self.assertTrue(guidance['follow_up_used'])
                    self.assertEqual(guidance['recent_reused_requests'][0]['checks'][0]['tool'], tool)
                    self.assertIn('N3', [n['need_id'] for n in guidance['unresolved_core_needs']])
                if calls <= 3:
                    data.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': f'检查{calls}',
                        'checks': [metric, empty] if calls == 1 else [empty] if calls == 2 else [new],
                        'need_ids': ['N1', 'N2', 'N3'], 'expected_value': '新观测可区分候选原因'}])
                return json_response(data)
            models['supervisor'] = AlterOutput(models['supervisor'], schedule)
            models['investigation'] = FocusedLeaf()
            with self.subTest(tool=tool):
                result = collaborate(models, provider, P)
                self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
                self.assertEqual((result['supervisor_decisions'], result['run_summary']['model_calls'],
                    result['run_summary']['tool_calls']), (4, 10, 3))
                events = [e for e in result['events'] if e['type'] == 'collection_follow_up']
                self.assertEqual([e['reason'] for e in events], ['no_new_read'])
                self.assertEqual(sum(e['type'] == 'task_reused' for e in result['events']), 1)
                self.assertEqual(len(result['task_results']), 2)
                self.assertEqual(result['repairs'], 0)

    def test_persistent_repeat_has_no_new_reads_and_no_unbounded_retry(self):
        provider = FixtureProvider('case_001', P)
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        empty = {'tool': 'get_recent_changes', 'args': {**window, 'category': 'configuration'}}
        metric = {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['dependency_error_rate']}}
        models = scripted_models()
        def schedule(response, _, calls):
            value = json.loads(response.content)
            value.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': '重复空结果',
                'checks': [metric, empty] if calls == 1 else [empty]}])
            for need in value['evidence_needs']:
                need.update(status='pending', evidence_ids=[], reason='仍有关键歧义')
            return json_response(value)
        def review(response, *_):
            value = json.loads(response.content)
            next(n for n in value['need_assessments'] if n['need_id'] == 'N2').update(
                verdict='uncertain', evidence_ids=[], reason='没有新观测')
            value['follow_up_reason'] = '脚本仍没有选择新检查，保留缺口'
            return json_response(value)
        models.update(supervisor=AlterOutput(models['supervisor'], schedule), investigation=FocusedLeaf(),
                      diagnosis=MetricDiagnosis(), reviewer=AlterOutput(models['reviewer'], review))
        result = collaborate(models, provider, P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((result['supervisor_decisions'], result['run_summary']['model_calls'],
            result['run_summary']['tool_calls']), (3, 7, 2))
        self.assertEqual(sum(e['type'] == 'collection_follow_up' for e in result['events']), 1)
        self.assertEqual(len(result['task_results']), 1)

    def test_pending_alternative_can_be_assessed_without_forced_new_queries(self):
        models = scripted_models()
        def schedule(response, messages, calls):
            value = json.loads(response.content)
            if calls == 2:
                next(n for n in value['evidence_needs'] if n['need_id'] == 'N3').update(
                    status='pending', evidence_ids=[], reason='尚未评估对照')
            if calls == 3:
                self.assertIn('expected_value', messages[-1].content)
                next(n for n in value['evidence_needs'] if n['need_id'] == 'N3').update(
                    status='not_required', evidence_ids=[], reason='控制响应评估已有观测后无需新的对照查询')
            return json_response(value)
        def review(response, *_):
            value = json.loads(response.content)
            next(n for n in value['need_assessments'] if n['need_id'] == 'N3').update(
                verdict='not_required', evidence_ids=[], reason='控制复核独立判断无需额外查询')
            return json_response(value)
        models['supervisor'] = AlterOutput(models['supervisor'], schedule)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        result = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['evidence_needs'][2]['status'], 'not_required')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (7, 2))
        self.assertEqual(sum(e['type'] == 'collection_follow_up' for e in result['events']), 1)
        self.assertEqual(result['repairs'], 0)

    def test_planning_hint_never_promotes_unresolved_need_and_small_budget_skips_it(self):
        for cap, expected in ((16, 7), (6, 6)):
            models = scripted_models()
            def schedule(response, *_):
                value = json.loads(response.content)
                for n in value['evidence_needs']:
                    n.update(status='pending', evidence_ids=[], reason='控制响应不评估')
                return json_response(value)
            def review(response, *_):
                value = json.loads(response.content)
                for n in value['need_assessments']:
                    n.update(verdict='uncertain', evidence_ids=[], reason='控制复核不支持')
                value['follow_up_reason'] = '控制模型未选择可执行的新检查'
                return json_response(value)
            models['supervisor'] = AlterOutput(models['supervisor'], schedule)
            models['reviewer'] = AlterOutput(models['reviewer'], review)
            with self.subTest(cap=cap):
                result = collaborate(models, FixtureProvider('case_001', P), P,
                    limits=Limits(model_calls=cap, tool_calls=3, reserve_model_calls=3, reserve_seconds=20))
                self.assertEqual(result['status'], 'partial')
                self.assertTrue(all(n['status'] == 'pending' for n in result['evidence_needs']))
                self.assertEqual(result['run_summary']['model_calls'], expected)
                self.assertEqual(sum(e['type'] == 'collection_follow_up' for e in result['events']), int(cap == 16))

    def test_six_remaining_calls_allow_one_review_hint_and_approved_rework(self):
        provider = FixtureProvider('case_003', P)
        result = collaborate(action_models(provider), provider, P,
            limits=Limits(model_calls=12, tool_calls=4, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (11, 3))
        self.assertEqual((result['rework_rounds'], result['repairs']), (1, 0))
        self.assertEqual(result['task_results'][-1]['model_calls'], 1)
        self.assertEqual(sum(e['type'] == 'review_follow_up_correction' for e in result['events']), 1)
        self.assertEqual(len(result['reviews']), 2)
        self.assertEqual(len([e for e in result['events'] if e.get('execution_source') == 'approved_rework'
                              and e['type'] == 'tool_result']), 1)

    def test_five_remaining_calls_do_not_spend_a_futile_review_hint(self):
        provider = FixtureProvider('case_003', P)
        result = collaborate(action_models(provider), provider, P,
            limits=Limits(model_calls=11, tool_calls=4, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertFalse(any(e['type'] == 'review_follow_up_correction' for e in result['events']))
        self.assertEqual(result['rework_rounds'], 0)

    def test_two_approved_checks_cannot_partially_execute_with_one_tool_slot(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider, invalid='A1')
        def two(response, messages, calls):
            value = json.loads(response.content)
            if value.get('request_evidence'):
                value['request_evidence']['checks'].append({'tool': 'get_recent_changes',
                    'args': provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})})
            return json_response(value)
        models['reviewer'] = AlterOutput(models['reviewer'], two)
        result = collaborate(models, provider, P, limits=Limits(model_calls=12, tool_calls=3,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(result['rework_rounds'], 0)
        self.assertFalse(any(e.get('execution_source') == 'approved_rework' for e in result['events']))

    def test_budget_and_next_check_guidance_are_read_only_and_respect_time_and_limits(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=16, tool_calls=4, reserve_model_calls=3, reserve_seconds=20))
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        check = ReadOnlyCheck(tool='get_recent_changes', args=window)
        c.evidence_needs['N2'] = c.evidence_needs['N2'].model_copy(update={'next_check': check})
        before = dict(c.context.counts)
        self.assertTrue(c.planning_guidance()['unresolved_core_needs'][1]['adds_information'])
        self.assertEqual(c.context.counts, before)
        c.executors['investigation'].execute(check.tool, check.args)
        before = dict(c.context.counts)
        self.assertFalse(c.planning_guidance()['unresolved_core_needs'][1]['adds_information'])
        self.assertEqual(c.context.counts, before)
        c.context.counts['model_calls'] = 10
        plan = c.rework_budget(pending_model_calls=1)
        self.assertEqual(plan['required_model_calls'], 6)
        self.assertTrue(plan['can_collect'])
        c.context.counts['model_calls'] = 11
        self.assertFalse(c.rework_budget(pending_model_calls=1)['can_collect'])
        self.assertTrue(c.rework_budget()['can_collect'])
        c.supervisor_calls = 4
        self.assertFalse(c.rework_budget()['can_collect'])
        c.supervisor_calls = 2
        c.context.started = time.monotonic() - 161
        self.assertFalse(c.rework_budget()['can_collect'])
        self.assertFalse(c.can_replan())


if __name__ == '__main__':
    unittest.main()
