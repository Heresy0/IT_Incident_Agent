"""Real coordinator guards; no model API or claims of semantic accuracy."""
import json
import unittest

from agents.contracts import ReviewDecision, Hypothesis
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider
from tests.tools.test_incident_tools import P
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration
from workflow.evidence_needs import reviewed_needs
from workflow.review_outcome import nonblocking_trigger_request


class ReviewProtectionTests(unittest.TestCase):
    def prepared(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P, review_protection='conservative_v1')
        c.run()
        c.stop = ''
        c.draft = c.draft.model_copy(update={'hypotheses': [
            c.draft.hypotheses[0].model_copy(update={'level': 'mechanism'}),
            Hypothesis(hypothesis_id='H2', cause='为何此时开始发生仍未知', level='trigger',
                       support_refs=[], counter_refs=[], pending_checks=['触发记录'])],
            'recommended_actions': [c.draft.recommended_actions[0].model_copy(update={
                'kind': 'manual_check', 'requires_approval': False})]})
        data = c.review.model_dump(mode='json')
        data['assessments'].append({'target_id': 'H2', 'verdict': 'uncertain', 'reason': '触发信息缺失', 'evidence_ids': []})
        data['request_evidence'] = None
        return c, data

    def blocker(self, data, targets, need='N3', **updates):
        value = {'cause_level': 'alternative', 'affected_targets': targets,
                 'missing_observation': '需要进一步取得记录', 'decision_impact': '该记录能解释未知来源',
                 'resolution': 'external_source'}
        value.update(updates)
        next(row for row in data['need_assessments'] if row['need_id'] == need).update(
            verdict='uncertain', blocking=True, blocker=value)

    def accept(self, c, data):
        c.review = c.validate_review(ReviewDecision.model_validate_json(json.dumps(data)))
        c.evidence_needs = reviewed_needs(c.evidence_needs, c.review.need_assessments)
        c.finish_review()
        return c.review

    def test_missing_current_counterevidence_is_unknown_not_refuted(self):
        c, data = self.prepared()
        next(a for a in data['assessments'] if a['target_id'] == 'H2').update(
            verdict='not_supported', reason='没有证据支持远端异常', evidence_ids=[])
        counts = dict(c.context.counts)
        review = self.accept(c, data)
        self.assertEqual(next(a for a in review.assessments if a.target_id == 'H2').verdict, 'uncertain')
        self.assertEqual(c.result()['output']['hypotheses'][1]['status'], 'unresolved')
        event = next(e for e in c.events if e['type'] == 'review_protection_applied')
        self.assertEqual(event['original']['verdict'], 'not_supported')
        self.assertEqual(event['original']['reason'], '没有证据支持远端异常')
        self.assertEqual(c.context.counts, counts)

    def test_missing_counterevidence_on_mechanism_cannot_finish(self):
        c, data = self.prepared()
        next(a for a in data['assessments'] if a['target_id'] == 'H1').update(verdict='not_supported', evidence_ids=[])
        self.accept(c, data)
        self.assertEqual(c.status(), 'partial')
        self.assertTrue(c.reviewed_gaps())

    def test_current_citation_retains_independent_negative_verdict_without_certifying_truth(self):
        c, data = self.prepared()
        eid = next(e.evidence_id for e in c.evidence.values() if e.kind == 'observation')
        next(a for a in data['assessments'] if a['target_id'] == 'H2').update(
            verdict='not_supported', evidence_ids=[eid], reason='此观测构成反证；含义仍需审阅')
        accepted = self.accept(c, data)
        self.assertEqual(next(a for a in accepted.assessments if a.target_id == 'H2').verdict, 'not_supported')
        self.assertFalse(any(e['type'] == 'review_protection_applied' for e in c.events))

    def test_historical_knowledge_alone_is_not_current_refutation(self):
        c, data = self.prepared()
        ex = c.executors['knowledge']
        ex.execute('search_runbooks', {'query': 'dependency'})
        c.evidence.update(ex.evidence)
        ids = [e.evidence_id for e in ex.evidence.values() if e.kind != 'observation']
        self.assertTrue(ids)
        next(a for a in data['assessments'] if a['target_id'] == 'H2').update(verdict='not_supported', evidence_ids=ids[:1])
        review = self.accept(c, data)
        self.assertEqual(next(a for a in review.assessments if a.target_id == 'H2').verdict, 'uncertain')

    def test_unknown_evidence_is_rejected_before_a_downgrade_can_mask_it(self):
        c, data = self.prepared()
        next(a for a in data['assessments'] if a['target_id'] == 'H2').update(verdict='not_supported', evidence_ids=['EV_FAKE'])
        with self.assertRaises(ProtocolError):
            self.accept(c, data)
        self.assertFalse(any(e['type'] == 'review_protection_applied' for e in c.events))

    def test_trigger_only_alternative_gap_becomes_visible_nonblocking_limitation(self):
        c, data = self.prepared()
        self.blocker(data, ['H2', 'A1'])
        original = json.loads(json.dumps(data))
        counts = dict(c.context.counts)
        accepted = self.accept(c, data)
        n3 = next(a for a in accepted.need_assessments if a.need_id == 'N3')
        self.assertEqual((n3.verdict, n3.blocking), ('uncertain', False))
        self.assertEqual(n3.blocker.affected_targets, ['H2', 'A1'])
        self.assertEqual(c.status(), 'completed')
        self.assertTrue(any('需要进一步取得记录' in gap for gap in c.extra_gaps))
        self.assertEqual(data, original)
        self.assertEqual(c.context.counts, counts)

    def test_core_uncertainty_is_retained_without_rejecting_whole_review(self):
        c, data = self.prepared()
        self.blocker(data, ['H2'], need='N2', cause_level='trigger')
        accepted = self.accept(c, data)
        n2 = next(a for a in accepted.need_assessments if a.need_id == 'N2')
        self.assertEqual(n2.verdict, 'uncertain')
        self.assertIsNone(n2.blocking)
        self.assertEqual(c.status(), 'partial')
        self.assertTrue(c.reviewed_gaps())

    def test_material_target_keeps_blocking_when_metadata_calls_it_trigger(self):
        c, data = self.prepared()
        next(a for a in data['assessments'] if a['target_id'] == 'H1')['verdict'] = 'uncertain'
        self.blocker(data, ['H1'], cause_level='trigger')
        accepted = self.accept(c, data)
        n3 = next(a for a in accepted.need_assessments if a.need_id == 'N3')
        self.assertTrue(n3.blocking)
        self.assertEqual(n3.blocker.cause_level, 'alternative')
        self.assertEqual(c.status(), 'partial')

    def test_supported_change_is_not_treated_as_a_supported_information_task(self):
        c, data = self.prepared()
        c.draft = c.draft.model_copy(update={'recommended_actions': [c.draft.recommended_actions[0].model_copy(
            update={'kind': 'manual_change', 'requires_approval': True})]})
        self.blocker(data, ['A1'])
        with self.assertRaisesRegex(ProtocolError, 'REVIEW_BLOCKER_CONFLICT'):
            self.accept(c, data)

    def test_unsupported_action_condition_remains_blocking(self):
        c, data = self.prepared()
        next(a for a in data['assessments'] if a['target_id'] == 'A1')['verdict'] = 'uncertain'
        self.blocker(data, ['A1'])
        accepted = self.accept(c, data)
        self.assertTrue(next(a for a in accepted.need_assessments if a.need_id == 'N3').blocking)
        self.assertEqual(c.status(), 'partial')

    def test_unsafe_query_inside_a_scope_limitation_is_rejected_without_execution(self):
        c, data = self.prepared()
        self.blocker(data, ['H2'], resolution='scoped_check', checks=[{'tool': 'get_recent_changes',
            'args': {'start': '2020-01-01T00:00:00Z', 'end': '2020-01-02T00:00:00Z'}}])
        counts = dict(c.context.counts)
        with self.assertRaisesRegex(ProtocolError, 'INVALID_BLOCKER_CHECK'):
            self.accept(c, data)
        self.assertEqual(c.context.counts, counts)

    def test_supported_manual_task_result_request_is_deferred_and_preserved(self):
        c, data = self.prepared()
        self.blocker(data, ['A1'])
        data['request_evidence'] = {'need_id': 'N3', 'target_id': 'A1',
            'missing_observation': '人工核查的结果', 'expected_value': '解释触发历史'}
        accepted = self.accept(c, data)
        self.assertEqual(accepted.request_evidence.target_id, 'A1')
        self.assertTrue(nonblocking_trigger_request(c.draft, accepted, c.evidence_needs, conservative=True))
        self.assertEqual(c.status(), 'completed')

    def test_end_to_end_scope_limitation_uses_no_extra_model_or_tool_call(self):
        from tests.workflow.test_review_handoff import ReviewHandoffTests
        def change(data):
            self.blocker(data, ['H2', 'A1'])
        report, _ = ReviewHandoffTests().run_report(change)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['run_summary']['model_calls'], 4)
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(report['output']['hypotheses'][1]['status'], 'unresolved')
        self.assertTrue(report['deferred_review_requests'])
        self.assertEqual(next(n for n in report['evidence_needs'] if n['need_id'] == 'N3')['blocking'], False)

    def test_legacy_mode_keeps_strict_scope_and_old_negative_projection(self):
        c, data = self.prepared()
        c.review_protection = 'strict_v1'
        next(a for a in data['assessments'] if a['target_id'] == 'H2').update(verdict='not_supported', evidence_ids=[])
        review = self.accept(c, data)
        self.assertEqual(next(a for a in review.assessments if a.target_id == 'H2').verdict, 'not_supported')
        self.assertEqual(c.result()['output']['hypotheses'][1]['status'], 'refuted')


if __name__ == '__main__':
    unittest.main()
