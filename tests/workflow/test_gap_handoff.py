"""Free role-output replays of gap handoff, planning, coverage and safety."""
import json
import unittest

from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import collaborate
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_information_gain import action_models, FocusedLeaf


class GapHandoffTests(unittest.TestCase):
    def run_gap(self, *, hint=None, unsafe_plan=False, repeated_plan=False, unavailable=False,
                need_only=False, limits=None, diagnose_exit=False):
        provider = FixtureProvider('case_003', P)
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        models = action_models(provider, invalid='A1')

        def gap(response, messages, calls):
            value = json.loads(response.content)
            if value.get('request_evidence'):
                # Reviewer identifies the issue without designing a query.
                value['request_evidence'] = {'need_id': 'N2' if need_only else None,
                    'target_id': None if need_only else 'A1',
                    'missing_observation': '缺少当前连接池压力，建议条件尚未核实',
                    'target_role': 'knowledge' if hint and hint['tool'].startswith('search_') else 'investigation',
                    'checks': [hint] if hint else []}
                if need_only:
                    next(n for n in value['need_assessments'] if n['need_id'] == 'N2').update(
                        verdict='uncertain', reason='缺少当前池压力，需要调度者规划')
            return json_response(value)

        def plan(response, messages, calls):
            value = json.loads(response.content)
            payload = json.loads(messages[1].content)['input']
            if payload['phase'] == 'rework':
                challenge = payload['pending_challenge']
                self.assertTrue(challenge['planning_required'])
                self.assertIsNone(challenge['hypothesis_id'])
                if diagnose_exit and (not repeated_plan or calls > 3):
                    value.update(action='diagnose', tasks=[], reason='没有可执行的新查询，保留信息缺口')
                elif unavailable:
                    value.update(action='request_info', tasks=[],
                        reason='现有渠道不能取得所需证据，交由人工核实',
                        missing_information=[challenge['missing_observation']])
                else:
                    args = {**window, 'metrics': ['db_cpu'] if repeated_plan else ['pool_usage', 'pool_wait']}
                    if unsafe_plan:
                        args['start'] = '2020-01-01T00:00:00Z'
                    value.update(action='dispatch', tasks=[{'role': 'investigation',
                        'goal': '补齐建议条件的池压力观测', 'need_ids': ['N2'] if need_only else [],
                        'expected_value': '池压力持续升高才支持当前建议的适用条件',
                        'checks': [{'tool': 'get_service_metrics', 'args': args}]}])
            return json_response(value)

        models['reviewer'] = AlterOutput(models['reviewer'], gap)
        models['supervisor'] = AlterOutput(models['supervisor'], plan)
        return collaborate(models, provider, P, **({'limits': limits} if limits else {}))

    def test_bare_action_gap_is_planned_by_supervisor_and_reviewed_again(self):
        result = self.run_gap()
        self.assertEqual((result['status'], result['review_status'], result['rework_rounds']),
                         ('completed', 'passed', 1))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (10, 3))
        self.assertEqual(result['reviews'][0]['review']['request_evidence']['checks'], [])
        approved = next(e for e in result['events'] if e['type'] == 'rework_plan_approved')
        read = next(e for e in result['events'] if e.get('execution_source') == 'approved_rework')
        self.assertLess(approved['seq'], read['seq'])
        self.assertEqual(len(result['reviews']), 2)

    def test_shared_question_gap_does_not_require_a_hypothesis_identifier(self):
        result = self.run_gap(need_only=True)
        self.assertEqual((result['status'], result['rework_rounds']), ('completed', 1))
        self.assertEqual(result['task_results'][-1]['need_ids'], ['N2'])
        self.assertEqual(result['evidence_needs'][1]['status'], 'supported')

    def test_valid_knowledge_hint_can_be_replaced_with_current_observation_check(self):
        hint = {'tool': 'search_runbooks', 'args': {'query': 'capacity safety'}}
        result = self.run_gap(hint=hint)
        self.assertEqual((result['status'], result['rework_rounds']), ('completed', 1))
        request = result['reviews'][0]['review']['request_evidence']
        self.assertEqual(request['target_role'], 'knowledge')
        self.assertEqual(request['checks'][0]['tool'], 'search_runbooks')
        task = result['task_results'][-1]
        self.assertEqual(task['role'], 'investigation')
        self.assertEqual(task['required_checks'][0]['tool'], 'get_service_metrics')
        self.assertEqual(result['run_summary']['tool_calls'], 3)

    def test_unsafe_hint_does_not_discard_gap_or_authorize_a_read(self):
        hint = {'tool': 'get_service_logs', 'args': {'category': 'resource',
            'start': '2020-01-01T00:00:00Z', 'end': '2026-10-09T00:00:00Z'}}
        result = self.run_gap(hint=hint)
        self.assertEqual((result['status'], result['rework_rounds']), ('completed', 1))
        self.assertTrue(any(e['type'] == 'review_hint_ignored' for e in result['events']))
        self.assertEqual(result['run_summary']['tool_calls'], 3)
        approved = next(e for e in result['events'] if e['type'] == 'rework_plan_approved')
        self.assertEqual(approved['checks'][0]['tool'], 'get_service_metrics')

    def test_supervisor_cannot_execute_duplicate_plan(self):
        result = self.run_gap(repeated_plan=True)
        self.assertEqual((result['status'], result['rework_rounds']), ('partial', 0))
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        failure = next(e for e in result['events'] if e['type'] == 'validation_failure')
        self.assertEqual(failure['details'][0]['code'], 'DUPLICATE_TOOL')
        self.assertFalse(any(e['type'] == 'rework_plan_approved' for e in result['events']))

    def test_rework_diagnose_exit_preserves_review_without_claiming_no_observations(self):
        for repeated_plan in (False, True):
            with self.subTest(repeated_plan=repeated_plan):
                result = self.run_gap(diagnose_exit=True, repeated_plan=repeated_plan)
                self.assertEqual((result['status'], result['review_status'], result['rework_rounds']),
                                 ('partial', 'needs_information', 0))
                self.assertEqual(result['run_summary']['termination_reason'], 'REWORK_NOT_EXECUTED')
                self.assertEqual(result['run_summary']['tool_calls'], 2)
                self.assertEqual((len(result['drafts']), len(result['reviews'])), (1, 1))
                self.assertTrue(result['output']['findings'])
                self.assertIn('复核补查未执行', '\n'.join(result['output']['missing_information']))
                denied = next(e for e in result['events'] if e['type'] == 'rework_denied')
                self.assertEqual(denied['code'], 'REWORK_NOT_EXECUTED')
                self.assertFalse(any(e.get('code') == 'DIAGNOSIS_WITHOUT_OBSERVATIONS'
                                     for e in result['events']))
                self.assertFalse(any(e['type'] == 'rework_plan_approved' for e in result['events']))
                self.assertEqual(result['repairs'], int(repeated_plan))

    def test_diagnosis_without_actual_observations_still_reports_original_error(self):
        provider = FixtureProvider('case_003', P)
        models = scripted_models()

        def diagnose(response, messages, calls):
            value = json.loads(response.content)
            value.update(action='diagnose', tasks=[], reason='直接诊断')
            return json_response(value)

        models['supervisor'] = AlterOutput(models['supervisor'], diagnose)
        result = collaborate(models, provider, P)
        self.assertEqual(result['run_summary']['termination_reason'], 'DIAGNOSIS_WITHOUT_OBSERVATIONS')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        self.assertEqual((result['drafts'], result['reviews']), ([], []))

    def test_supervisor_cannot_expand_scope(self):
        result = self.run_gap(unsafe_plan=True)
        self.assertEqual((result['status'], result['rework_rounds']), ('partial', 0))
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertTrue(any(e['type'] == 'validation_failure' and e['reason'] == 'INVALID_TASK_CHECK'
                            for e in result['events']))

    def test_unavailable_evidence_can_end_without_invented_queries(self):
        result = self.run_gap(unavailable=True)
        self.assertEqual((result['status'], result['rework_rounds']), ('partial', 0))
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertIn('缺少当前连接池压力', '\n'.join(result['output']['missing_information']))
        self.assertFalse(any(e['type'] == 'rework_plan_approved' for e in result['events']))

    def test_small_budget_retains_gap_without_futile_dispatch(self):
        result = self.run_gap(limits=Limits(model_calls=8, tool_calls=5,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual((result['status'], result['rework_rounds']), ('partial', 0))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (6, 2))
        self.assertTrue(any(e['type'] == 'rework_denied' and e['code'] == 'BUDGET_EXCEEDED'
                            for e in result['events']))

    def test_coverage_reminder_precedes_diagnosis_and_collects_missing_channel(self):
        provider = FixtureProvider('case_001', P)
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        models = scripted_models()
        log = {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}}

        def plan(response, messages, calls):
            value = json.loads(response.content)
            if calls <= 3:
                for need in value['evidence_needs']:
                    need.update(status='pending', evidence_ids=[], reason='关键机制尚需日志支持')
                if calls == 1:
                    checks = [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['dependency_error_rate']}}]
                    value.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': '核对异常指标',
                        'checks': checks, 'need_ids': ['N1']}])
                elif calls == 2:
                    value.update(action='diagnose', tasks=[])
                else:
                    self.assertIn('关键问题', messages[-1].content)
                    value.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': '核对机制日志',
                        'checks': [log], 'need_ids': ['N2'], 'expected_value': '错误日志可区分依赖失败机制'}])
            return json_response(value)

        models['supervisor'] = AlterOutput(models['supervisor'], plan)
        models['investigation'] = FocusedLeaf()
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(sum(e['type'] == 'collection_follow_up' for e in result['events']), 1)
        gate = next(e for e in result['events'] if e['type'] == 'collection_coverage_gate')
        self.assertFalse(gate['limited'])
        self.assertEqual(gate['reason'], 'questions_assessed')
        self.assertEqual([e['name'] for e in result['events'] if e['type'] == 'tool_result'],
                         ['get_service_metrics', 'get_service_logs'])


if __name__ == '__main__':
    unittest.main()
