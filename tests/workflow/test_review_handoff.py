"""Question-owned metadata and conservative trigger handoff through the graph."""
import json
import unittest

from agents.contracts import ReviewDecision
from agents.protocol import parse_review_selection, reviewer_schema
from evidence.diagnosis import ProtocolError
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate
from workflow.review_outcome import nonblocking_trigger_request


class ReviewHandoffTests(unittest.TestCase):
    def run_report(self, change=None, budget=12):
        models = planned_models()
        def diagnosis(response, messages, calls):
            data = json.loads(response.content)
            data['hypotheses'][0]['level'] = 'mechanism'
            data['hypotheses'].append({'cause': '为何此时开始发生仍未知', 'level': 'trigger',
                'support_refs': [], 'counter_refs': [], 'pending_checks': ['核对触发记录']})
            data['recommended_actions'][0].update(kind='manual_check', requires_approval=False,
                action='人工核对依赖服务状态', risk='只读核实')
            return json_response(data)
        def reviewer(response, messages, calls):
            data = json.loads(response.content)
            next(a for a in data['assessments'] if a['target_id'] == 'H2').update(
                verdict='uncertain', evidence_ids=[], reason='触发细节未知，不改变当前下一步')
            data['request_evidence'] = {'target_id': 'H2',
                'missing_observation': '故障开始时的触发记录', 'expected_value': '解释为何开始发生'}
            if change:
                change(data)
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], diagnosis)
        models['reviewer'] = AlterOutput(models['reviewer'], reviewer)
        return collaborate(models, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=budget, tool_calls=4)), models

    def test_optional_trigger_request_remains_visible_without_extra_calls(self):
        report, models = self.run_report()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 4)
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(models['reviewer'].calls, 1)
        self.assertEqual(report['output']['hypotheses'][1]['status'], 'unresolved')
        self.assertEqual(report['reviews'][0]['review']['request_evidence']['target_id'], 'H2')
        self.assertEqual(report['deferred_review_requests'][0]['request']['missing_observation'], '故障开始时的触发记录')
        self.assertIn('非关键触发原因待人工核实', render(report))
        self.assertEqual(report['diagnosis_readiness']['report_status'], 'reviewed_with_limitations')
        self.assertTrue(report['output']['recommended_actions'])

    def test_unknown_mechanism_action_or_blocking_need_cannot_be_deferred(self):
        for field in ('mechanism', 'action', 'need'):
            def change(data):
                if field in {'mechanism', 'action'}:
                    key = 'H1' if field == 'mechanism' else 'A1'
                    next(a for a in data['assessments'] if a['target_id'] == key).update(
                        verdict='uncertain', evidence_ids=[], reason='关键证据不足')
                else:
                    next(a for a in data['need_assessments'] if a['need_id'] == 'N2').update(
                        verdict='uncertain', evidence_ids=[], reason='机制尚不明确')
            with self.subTest(field=field):
                report, _ = self.run_report(change, budget=6)
                self.assertEqual(report['status'], 'partial')
                self.assertFalse(report['deferred_review_requests'])

    def test_bad_reference_is_not_normalized_into_success(self):
        def bad(data):
            data['request_evidence']['evidence_ids'] = ['EV_MISSING']
        report, _ = self.run_report(bad)
        self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertFalse(report['deferred_review_requests'])
        self.assertFalse(report['output']['recommended_actions'])

    def prepared(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        c.run()
        return c

    def test_omitted_category_is_derived_without_changing_verdict_or_prose(self):
        c = self.prepared()
        schema = reviewer_schema(ReviewDecision.model_json_schema(), c.evidence_needs, c.targets())
        self.assertNotIn('cause_level', schema['$defs']['ReviewBlocker']['properties'])
        self.assertNotIn('cause_level', schema['$defs']['ReviewBlocker']['required'])
        data = c.review.model_dump(mode='json')
        n2 = next(a for a in data['need_assessments'] if a['need_id'] == 'N2')
        n2.update(verdict='uncertain', reason='失败机制待核实', blocker={
            'affected_targets': ['H1'], 'missing_observation': '失败请求记录',
            'decision_impact': '记录可能改变机制判断', 'resolution': 'not_planned'})
        next(a for a in data['assessments'] if a['target_id'] == 'H1')['verdict'] = 'uncertain'
        result = parse_review_selection(json.dumps(data), c.evidence_needs)
        accepted = c.validate_review(result)
        actual = next(a for a in accepted.need_assessments if a.need_id == 'N2')
        self.assertEqual(actual.verdict, 'uncertain')
        self.assertEqual(actual.reason, '失败机制待核实')
        self.assertEqual(actual.blocker.cause_level, 'mechanism')
        self.assertEqual(actual.blocker.missing_observation, '失败请求记录')
        self.assertFalse(nonblocking_trigger_request(c.draft, accepted, c.evidence_needs))
        data['need_assessments'][1]['evidence_ids'] = ['EV_MISSING']
        with self.assertRaises(ProtocolError):
            c.validate_review(parse_review_selection(json.dumps(data), c.evidence_needs))

    def test_explicit_legacy_scope_mismatch_still_rejected(self):
        c = self.prepared()
        data = c.review.model_dump(mode='json')
        data['need_assessments'][1].update(verdict='uncertain', blocker={
            'cause_level': 'trigger', 'affected_targets': ['H1'],
            'missing_observation': '触发记录', 'decision_impact': '改变机制', 'resolution': 'not_planned'})
        next(a for a in data['assessments'] if a['target_id'] == 'H1')['verdict'] = 'uncertain'
        with self.assertRaises(ProtocolError) as caught:
            c.validate_review(parse_review_selection(json.dumps(data), c.evidence_needs))
        self.assertEqual(caught.exception.code, 'REVIEW_BLOCKER_SCOPE')

    def test_blocking_question_cannot_use_only_trigger_and_already_supported_next_step(self):
        c = self.prepared()
        c.draft = c.draft.model_copy(update={'hypotheses': [
            h.model_copy(update={'level': 'trigger'}) for h in c.draft.hypotheses]})
        data = c.review.model_dump(mode='json')
        next(a for a in data['assessments'] if a['target_id'] == 'H1')['verdict'] = 'uncertain'
        data['need_assessments'][2].update(verdict='uncertain', blocking=True, blocker={
            'affected_targets': ['H1', 'A1'], 'missing_observation': '触发记录',
            'decision_impact': '需要完成已支持的人工核实待办', 'resolution': 'external_source'})
        before = c.need_input(), dict(c.context.counts)
        with self.assertRaises(ProtocolError) as caught:
            c.validate_review(parse_review_selection(json.dumps(data), c.evidence_needs))
        self.assertEqual(caught.exception.code, 'REVIEW_BLOCKER_SCOPE')
        self.assertEqual((c.need_input(), c.context.counts), before)
        # An uncertain action is a material distinction and is never erased.
        next(a for a in data['assessments'] if a['target_id'] == 'A1')['verdict'] = 'uncertain'
        accepted = c.validate_review(parse_review_selection(json.dumps(data), c.evidence_needs))
        self.assertTrue(accepted.need_assessments[2].blocking)
        self.assertEqual(next(a for a in accepted.assessments if a.target_id == 'A1').verdict, 'uncertain')
