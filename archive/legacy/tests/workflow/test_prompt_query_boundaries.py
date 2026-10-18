"""Prompt delivery and existing workflow controls; scripted verdicts are not model quality.

The negative controls deliberately retain semantic mistakes that prompts alone
cannot detect. No saved experiment or fixture is changed.
"""
from copy import deepcopy
import json
import unittest

from api.auth import Principal
from agents.contracts import CHECK_EXPECTED_RESULTS, TaskRequest
from evals.independent.provider import IndependentProvider
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.workflow.test_multi_minimal_handoff import OutageProvider
from tests.workflow.test_planned_default import planned_models
from tests.workflow.test_review_feedback_boundaries import (
    RecordingRole, diagnose, future_only, hypothesis_case, outage_models, review, selected,
)
from workflow.coordinator import Collaboration, collaborate


def record(models, role, callback=None):
    models[role] = RecordingRole(callback or models[role].callback)
    return models[role]


class PromptQueryBoundaryTests(unittest.TestCase):
    def test_untrusted_exclusion_plan_and_actual_empty_ledger_reach_both_judges(self):
        models, provider = outage_models(), OutageProvider()
        provider._data['changes'] = []
        original = models['supervisor'].callback
        bad_goal = '排除部署或配置引入的问题'
        bad_expected = '若无变更，则排除此类近端原因'

        def supervisor(payload, number):
            value = original(payload, number)
            if number == 1:
                task = value['tasks'][0]
                task.update(goal=bad_goal, expected_value=bad_expected)
                task['checks'][0] = {'tool': 'get_recent_changes', 'args': {
                    k: payload['ticket']['scope'][k] for k in ('start', 'end')}}
            return value

        planner = record(models, 'supervisor', supervisor)
        diagnosis, reviewer = record(models, 'diagnosis'), record(models, 'reviewer')
        report = collaborate(models, provider, P)
        self.assertIn('不能规划', planner.messages[0][0].content)
        self.assertIn('空变更查询', diagnosis.messages[0][0].content)
        self.assertIn('goal_verified', diagnosis.messages[0][0].content)
        self.assertIn('查空不等于排除', reviewer.messages[0][0].content)
        for role in (diagnosis, reviewer):
            body = json.loads(role.messages[0][1].content)['input']
            task = next(t for t in body['task_results'] if t['goal'] == bad_goal)
            self.assertFalse(task['model_summary_verified'])
            self.assertFalse(task['completion']['goal_verified'])
            query = next(c for c in task['completion']['checks'] if c['tool'] == 'get_recent_changes')
            self.assertEqual(query['status'], 'empty')
            self.assertTrue(query['query_complete'])
            self.assertTrue(task['completion']['limitations'])
        self.assertEqual(diagnosis.history[0]['task_results'][0]['expected_value'], bad_expected)
        self.assertEqual((report['run_summary']['model_calls'], report['run_summary']['tool_calls']), (4, 3))
        self.assertFalse(report['output']['hypotheses'])

    def test_open_canonical_result_can_publish_and_semantic_mislabel_is_not_auto_fixed(self):
        for mislabel in (False, True):
            with self.subTest(mislabel=mislabel):
                models, provider = outage_models(), OutageProvider()
                original = models['reviewer'].callback

                def reviewer(payload, number):
                    value = original(payload, number)
                    if mislabel:
                        value['assessments'][0].update(verdict='uncertain', uncertainty_basis='assumed_outcome',
                            reason='判断哪些候选原因得到支持，预设了核查结果')
                    return value

                model = record(models, 'reviewer', reviewer)
                report = collaborate(models, provider, P)
                action = model.history[0]['targets']['A1']
                self.assertEqual(action['expected_result'], CHECK_EXPECTED_RESULTS['manual_check'])
                self.assertIn('常量本身不构成assumed_outcome', model.messages[0][0].content)
                self.assertIn('仍须核对action和condition', model.messages[0][0].content)
                self.assertEqual(bool(report['output']['recommended_actions']), not mislabel)
                self.assertEqual(bool(report['pending_actions']), mislabel)
                self.assertEqual(report['repair_counts']['reviewer'], 0)
                self.assertTrue(all(n['status'] == 'pending' for n in report['evidence_needs']
                                    if n['purpose'] in {'symptom', 'mechanism'}))

    def test_real_action_objections_remain_pending_despite_canonical_result(self):
        objections = {
            'assumed_outcome': '确认服务已经恢复并据此关闭问题',
            'scope_or_safety_gap': '读取授权范围外的私有请求',
            'relevance_gap': '核查与本次故障无关的库存事项',
        }
        for basis, text in objections.items():
            with self.subTest(basis=basis):
                models, provider = outage_models(), OutageProvider()
                old_diagnosis, old_review = models['diagnosis'].callback, models['reviewer'].callback

                def diagnosis(payload, number):
                    value = old_diagnosis(payload, number)
                    value['recommended_actions'][0]['action'] = text
                    return value

                def reviewer(payload, number):
                    value = old_review(payload, number)
                    value['assessments'][0].update(verdict='uncertain', uncertainty_basis=basis, reason=text)
                    return value

                record(models, 'diagnosis', diagnosis)
                record(models, 'reviewer', reviewer)
                report = collaborate(models, provider, P)
                self.assertFalse(report['output']['recommended_actions'])
                self.assertFalse(report['pending_actions'][0]['executable'])
                self.assertEqual(report['reviews'][0]['review']['assessments'][0]['uncertainty_basis'], basis)
                self.assertEqual(report['run_summary']['tool_calls'], 3)

    def test_source_errors_stay_in_ledger_and_prompt_distinguishes_business_trigger(self):
        models, provider = outage_models(), OutageProvider()
        diagnosis, reviewer = record(models, 'diagnosis'), record(models, 'reviewer')
        report = collaborate(models, provider, P)
        for role in (diagnosis, reviewer):
            self.assertIn('SOURCE_UNAVAILABLE', role.messages[0][0].content)
            body = role.history[0]
            errors = [c for c in body['collection'] if c['status'] == 'error']
            self.assertEqual(len(errors), 2)
            self.assertTrue(all(c['error']['code'] == 'SOURCE_UNAVAILABLE' for c in errors))
        self.assertIn('不把渠道缺口列为业务trigger', diagnosis.messages[0][0].content)
        self.assertFalse(report['output']['hypotheses'])
        self.assertTrue(report['output']['recommended_actions'])
        self.assertEqual(provider.calls.count('get_service_owner'), 1)
        self.assertEqual(report['status'], 'partial')

    def test_independent_current_observation_can_still_support_specific_fault(self):
        principal = Principal('synthetic_demo', 'cli_reader')
        provider = IndependentProvider('eval_001', principal)
        extra = deepcopy(provider._data['logs'][0])
        extra.update(id='local-collector-positive-control', error_code='COLLECTOR_CONFIG_INVALID',
            message='仅内存合成控制：本服务采集器加载了无效地址，当前采集请求因该地址失败。')
        provider._data['logs'].append(extra)
        c = Collaboration(planned_models(), provider, principal, limits=Limits(model_calls=16, tool_calls=8))
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        task = TaskRequest(role='investigation', goal='取得独立的当前采集器故障观测',
            checks=[{'tool': 'get_service_logs', 'args': dict(window, category='dependency')}])
        objective = c.task_objective(task, False)
        c.record_task(task, objective, c.collect_task(task, objective, c.executors['investigation'], c.context, c.events, 0))
        refs = selected(c, 'message', lambda e: e.payload.get('id') == extra['id'])
        model = diagnose(c, '本服务采集器地址无效导致本次采集失败', refs, '仅陈述当前合成日志独立记录的失败环节')
        _, report = review(c)
        self.assertIn('有独立观测时', model.messages[0][0].content)
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
        self.assertEqual(c.context.counts['tool_calls'], 1)

    def test_prompt_only_change_keeps_bounded_correction_and_false_full_limitation(self):
        models, provider = outage_models(), OutageProvider()
        original = models['reviewer'].callback
        record(models, 'reviewer', lambda p, n: future_only(original(p, n)))
        report = collaborate(models, provider, P)
        self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(report['repair_counts']['reviewer'], 1)
        self.assertEqual(report['run_summary']['tool_calls'], 3)
        self.assertFalse(report['output']['recommended_actions'])
        c = hypothesis_case()
        diagnose(c, '观测渠道不可用，已定位为监控系统自身故障', selected(c, 'message'),
            '故意错误的脚本控制：引用只有DNS日志，未提供监控系统故障证据')
        _, report = review(c, scope='full', verdict='supported')
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
        # Preserved limitation: legal declarations can still contain semantic errors.


if __name__ == '__main__':
    unittest.main()
