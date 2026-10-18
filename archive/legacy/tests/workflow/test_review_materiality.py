"""Workflow safety controls; scripted verdicts do not measure semantic accuracy."""
import json
import unittest

from agents.contracts import ReviewDecision, CHECK_EXPECTED_RESULTS
from agents.protocol import parse_review_selection, reviewer_schema
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate


class ReviewMaterialityTests(unittest.TestCase):
    def run_report(self, *, uncertain_action=False, blocking_alternative=False, result_only=False, conditional_change=False):
        models = planned_models()
        def diagnosis(response, messages, calls):
            data = json.loads(response.content)
            data['hypotheses'][0]['level'] = 'mechanism'
            data['hypotheses'].append({'cause': '依赖维护方是否调整策略尚未知',
                'level': 'trigger', 'support_refs': [], 'counter_refs': [],
                'pending_checks': ['核对维护方策略记录']})
            data['recommended_actions'][0].update(kind='manual_check', checks=[], repair=None,
                action='人工核对依赖维护方的策略和状态', condition='已观测到依赖请求失败',
                expected_result='确认维护方调整策略并取得恢复时间' if uncertain_action else
                    '取得策略与状态记录，区分是否发生调整及其影响',
                risk='只读核实', requires_approval=False)
            if uncertain_action and not result_only:
                data['recommended_actions'][0]['action'] = '要求维护方确认已经调整的策略'
            if conditional_change:
                data['recommended_actions'][0].update(kind='manual_change',
                    action='人工审批后恢复原策略', condition='仅当原始记录确认策略被调整且确认导致异常，获批后执行',
                    expected_result='条件成立时恢复原策略应减少拒绝；观察错误率，未下降时重新排查',
                    risk='可能影响其他业务，须审批并可回滚', requires_approval=True)
            return json_response(data)
        def reviewer(response, messages, calls):
            data = json.loads(response.content)
            next(a for a in data['assessments'] if a['target_id'] == 'H2').update(
                verdict='uncertain', evidence_ids=[], reason='触发来源未知')
            next(a for a in data['need_assessments'] if a['need_id'] == 'N3').update(
                verdict='uncertain', blocking=blocking_alternative, evidence_ids=[],
                reason='存在会改变下一步的竞争解释' if blocking_alternative else
                    '触发来源未知不改变近端机制与人工只读核实的价值')
            if blocking_alternative:
                next(a for a in data['assessments'] if a['target_id'] == 'H1').update(
                    verdict='uncertain', reason='竞争机制尚不能区分')
                next(a for a in data['need_assessments'] if a['need_id'] == 'N3')['blocker'] = {
                    'affected_targets': ['H1'], 'missing_observation': '能区分竞争机制的对照',
                    'decision_impact': '对照可能改变当前机制与处理方向', 'resolution': 'not_planned'}
            if uncertain_action and not result_only:
                next(a for a in data['assessments'] if a['target_id'] == 'A1').update(
                    verdict='uncertain', evidence_ids=[], reason='action预设维护方已经调整策略')
            data['request_evidence'] = None
            data['follow_up_reason'] = '人工保留未知；建议措辞需要修正时无需为此查询新证据'
            # Explicit scripted audit decisions; these do not establish model quality.
            for assessment in data['assessments']:
                if assessment['target_id'].startswith('H'):
                    assessment['supported_scope'] = 'full' if assessment['verdict']=='supported' else 'partial'
                if assessment['target_id']=='A1' and assessment['verdict']=='uncertain':
                    assessment['uncertainty_basis'] = 'assumed_outcome'
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], diagnosis)
        models['reviewer'] = AlterOutput(models['reviewer'], reviewer)
        models['reviewer'].json_output = True
        return collaborate(models, FixtureProvider('case_001', P), P), models

    def test_unknown_origin_and_future_check_result_do_not_block_a_valid_next_step(self):
        report, models = self.run_report()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 4)
        self.assertEqual(report['output']['hypotheses'][1]['status'], 'unresolved')
        self.assertTrue(report['output']['recommended_actions'])
        n3 = next(n for n in report['evidence_needs'] if n['need_id'] == 'N3')
        self.assertEqual((n3['status'], n3['blocking']), ('pending', False))
        # Independent reviewer receives questions/criteria, not earlier verdicts.
        view = json.loads(models['reviewer'].model.histories[0][1].content)['input']
        self.assertTrue(next(n for n in view['evidence_needs'] if n['need_id'] == 'N3')['review_scope'])
        self.assertTrue(all('status' not in n for n in view['evidence_needs']))

    def test_action_assumption_is_quarantined_without_demoting_supported_mechanism(self):
        report, _ = self.run_report(uncertain_action=True)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
        self.assertFalse(report['output']['recommended_actions'])
        self.assertEqual(report['pending_actions'][0]['review_verdict'], 'uncertain')
        self.assertFalse(report['pending_actions'][0]['executable'])
        self.assertEqual(report['run_summary']['model_calls'], 4)
        self.assertEqual(report['rework_rounds'], 0)

    def test_material_competitor_still_blocks_even_with_supported_next_step(self):
        report, _ = self.run_report(blocking_alternative=True)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'unresolved')
        self.assertTrue(next(n for n in report['evidence_needs'] if n['need_id'] == 'N3')['blocking'])

    def test_check_result_is_neutral_before_review_and_original_is_auditable(self):
        report, _ = self.run_report(uncertain_action=True, result_only=True)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['output']['recommended_actions'][0]['expected_result'], CHECK_EXPECTED_RESULTS['manual_check'])
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
        event = next(e for e in report['events'] if e['type'] == 'check_outcome_normalized')
        self.assertEqual(event['original_expected_result'], '确认维护方调整策略并取得恢复时间')
        self.assertFalse(event['verdicts_changed'])
        self.assertFalse(event['execution_authorized'])
        self.assertEqual(report['run_summary']['model_calls'], 4)

    def test_conditional_change_keeps_its_premise_and_conditional_effect(self):
        report, _ = self.run_report(conditional_change=True)
        self.assertEqual(report['status'], 'completed')
        action = report['output']['recommended_actions'][0]
        self.assertIn('仅当原始记录确认', action['condition'])
        self.assertIn('条件成立时', action['expected_result'])
        self.assertTrue(action['requires_approval'])
        self.assertFalse(any(e['type'] == 'check_outcome_normalized' for e in report['events']))
        self.assertEqual(report['output']['hypotheses'][1]['status'], 'unresolved')

    def test_trigger_blocker_is_advertised_out_of_scope_and_still_rejected_at_runtime(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        c.run()
        c.review_protection = 'strict_v1'
        c.draft = c.draft.model_copy(update={'hypotheses': [
            h.model_copy(update={'level': 'trigger'}) for h in c.draft.hypotheses]})
        schema = reviewer_schema(ReviewDecision.model_json_schema(), c.evidence_needs, c.targets())
        constraints = schema['$defs']['NeedAssessment']['allOf']
        eligible = next(row['then']['properties']['blocker']['anyOf'][1]['properties']['affected_targets']['contains']['enum']
                        for row in constraints if row['if']['properties']['need_id']['enum'] == ['N3'])
        self.assertNotIn('H1', eligible)
        self.assertIn('A1', eligible)  # Action applicability can still matter.
        data = c.review.model_dump(mode='json')
        next(a for a in data['assessments'] if a['target_id'] == 'H1')['verdict'] = 'uncertain'
        next(a for a in data['need_assessments'] if a['need_id'] == 'N3').update(
            verdict='uncertain', blocking=True, blocker={
                'affected_targets': ['H1'], 'missing_observation': '触发来源',
                'decision_impact': '解释为何开始', 'resolution': 'external_source'})
        with self.assertRaises(ProtocolError) as caught:
            c.validate_review(parse_review_selection(json.dumps(data), c.evidence_needs))
        self.assertEqual(caught.exception.code, 'REVIEW_BLOCKER_SCOPE')
