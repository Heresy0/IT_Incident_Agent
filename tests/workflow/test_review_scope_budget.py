"""Gap ownership and a bounded path from review to current evidence."""
import json
import unittest

from agents.contracts import EvidenceNeed, EvidenceRequest
from agents.contracts import ReviewDecision
from agents.protocol import reviewer_schema
from agents.scripted import scripted_models
from langchain_core.messages import ToolMessage
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_information_gain import FocusedLeaf, action_models
from workflow.coordinator import Collaboration


class ReviewScopeBudgetTests(unittest.TestCase):
    def reviewed(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P, investigation_mode='agent')
        c.run()
        c.draft = c.draft.model_copy(update={'hypotheses': [
            h.model_copy(update={'level': 'trigger'}) for h in c.draft.hypotheses]})
        assessments = [a.model_copy(update={'verdict': 'uncertain'}) if a.target_id == 'H1'
                       else a for a in c.review.assessments]
        review = c.review.model_copy(update={'assessments': assessments,
            'request_evidence': EvidenceRequest(need_id='N2', target_id='H1',
                missing_observation='变更发起记录', expected_value='区分变更发起原因')})
        return c, review

    def test_trigger_request_cannot_reopen_symptom_mechanism_or_alternative(self):
        c, review = self.reviewed()
        before = c.need_input(), dict(c.context.counts)
        for need in ('N1', 'N2', 'N3'):
            with self.subTest(need=need):
                result = c.validate_review(review.model_copy(update={'request_evidence':
                    review.request_evidence.model_copy(update={'need_id': need})}))
                self.assertIsNone(result.request_evidence.need_id)
                self.assertEqual(result.request_evidence.target_id, 'H1')
                self.assertEqual(result.request_evidence.missing_observation, '变更发起记录')
                self.assertEqual(result.assessments, review.assessments)
                self.assertEqual(result.need_assessments, review.need_assessments)
        self.assertEqual((c.need_input(), c.context.counts), before)
        self.assertFalse(any(e['type'] == 'review_need_reopened' for e in c.events))
        self.assertEqual(sum(e['type'] == 'review_request_scope_detached' for e in c.events), 3)

    def test_target_only_gap_keeps_independent_mechanism_verdict(self):
        c, review = self.reviewed()
        validated = c.validate_review(review.model_copy(update={'request_evidence':
            review.request_evidence.model_copy(update={'need_id': None})}))
        self.assertEqual(next(a for a in validated.need_assessments if a.need_id == 'N2').verdict, 'supported')
        self.assertEqual(next(a for a in validated.assessments if a.target_id == 'H1').verdict, 'uncertain')
        self.assertIsNotNone(validated.request_evidence)
        schema = reviewer_schema(ReviewDecision.model_json_schema(), c.evidence_needs, c.targets())
        rule = schema['$defs']['EvidenceRequest']['allOf'][0]
        self.assertEqual(rule['if']['properties']['target_id']['enum'], ['H1'])
        self.assertNotIn('N2', rule['then']['properties']['need_id']['enum'])
        self.assertNotIn('N1', rule['then']['properties']['need_id']['enum'])
        self.assertIn(None, rule['then']['properties']['need_id']['enum'])

    def test_detachment_does_not_promote_an_uncertain_need(self):
        c, review = self.reviewed()
        needs = [a.model_copy(update={'verdict': 'uncertain', 'reason': '机制仍缺证据'})
                 if a.need_id == 'N2' else a for a in review.need_assessments]
        result = c.validate_review(review.model_copy(update={'need_assessments': needs}))
        self.assertEqual(result.need_assessments, needs)
        self.assertEqual(next(a for a in result.assessments if a.target_id == 'H1').verdict, 'uncertain')
        self.assertIsNone(result.request_evidence.need_id)

    def test_existing_trigger_need_is_valid_but_cannot_attach_mechanism(self):
        c, review = self.reviewed()
        c.evidence_needs['N4'] = EvidenceNeed(need_id='N4', purpose='trigger',
            question='为何发生变更', reason='尚未核实')
        review = review.model_copy(update={'request_evidence':
            review.request_evidence.model_copy(update={'need_id': 'N4'})})
        self.assertEqual(c.validate_review(review).request_evidence.need_id, 'N4')
        c.draft = c.draft.model_copy(update={'hypotheses': [
            h.model_copy(update={'level': 'mechanism'}) for h in c.draft.hypotheses]})
        result = c.validate_review(review)
        self.assertIsNone(result.request_evidence.need_id)
        self.assertEqual(next(a for a in result.assessments if a.target_id == 'H1').verdict, 'uncertain')

    def test_optional_collection_leaves_review_rework_and_final_review(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider, invalid='A1')
        class HistoryControl(FocusedLeaf):
            def invoke(self, messages):
                if any(isinstance(m, ToolMessage) for m in messages):
                    return json_response({'findings': [], 'missing_information': ['历史检索不能证明当前机制']})
                return super().invoke(messages)
        models['knowledge'] = HistoryControl()
        def keep_collecting(response, messages, calls):
            view = json.loads(messages[1].content)['input']
            if view['phase'] == 'rework':
                return response
            value = json.loads(response.content)
            if not view['tasks']:
                value['tasks'] = [t for t in value['tasks'] if t['role'] == 'investigation']
                return json_response(value)
            value.update(action='dispatch', reason='控制：继续请求历史检索', tasks=[{
                'role': 'knowledge', 'goal': '历史线索' + str(calls), 'need_ids': ['N3'],
                'checks': [{'tool': 'search_incidents', 'args': {'symptoms': 'control-' + str(calls)}}],
                'expected_value': '检索历史候选，不代替当前机制证据'}])
            return json_response(value)
        models['supervisor'] = AlterOutput(models['supervisor'], keep_collecting)
        c = Collaboration(models, provider, P,
            limits=Limits(model_calls=16, tool_calls=8, reserve_model_calls=3),
            parallel_collection=False, investigation_mode='agent')
        report = c.run()
        self.assertTrue(any(e['type'] == 'collection_reserve_reached' for e in report['events']))
        self.assertEqual(report['rework_rounds'], 1)
        self.assertEqual(len(report['reviews']), 2)
        self.assertTrue(any(e.get('execution_source') == 'approved_rework' for e in report['events']))
        self.assertEqual(report['status'], 'completed')
        self.assertLessEqual(report['run_summary']['model_calls'], 16)
        self.assertLessEqual(report['run_summary']['tool_calls'], 8)

    def test_blind_review_path_and_supervisor_slot_are_reserved(self):
        c, _ = self.reviewed()
        c.blind_review = True
        c.supervisor_calls = 3
        c.context.counts.update(model_calls=8, tool_calls=2)
        c.stop = ''
        c.next_node = 'supervisor'
        before = c.need_input()
        c.supervise()
        self.assertEqual(c.next_node, 'diagnosis')
        self.assertEqual(c.context.counts['model_calls'], 8)
        self.assertEqual(c.need_input(), before)
        c.context.counts['model_calls'] += 3  # diagnosis, blind review, decision
        self.assertTrue(c.rework_budget()['can_collect'])


if __name__ == '__main__':
    unittest.main()
