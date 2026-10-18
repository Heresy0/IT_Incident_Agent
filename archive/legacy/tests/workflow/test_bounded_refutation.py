"""Exercise actual review gates without model or tool API calls."""
import json
import unittest
from datetime import timedelta

from agents.contracts import ReviewDecision
from agents.protocol import reviewer_schema
from evidence.diagnosis import ProtocolError
from workflow.review_protection import BOUNDED_POLICY


def prepared():
    from tests.workflow.test_review_protection import ReviewProtectionTests
    c, data = ReviewProtectionTests().prepared()
    c.review_protection = BOUNDED_POLICY
    eid = next(e.evidence_id for e in c.evidence.values() if e.kind == 'observation')
    assessment = next(a for a in data['assessments'] if a['target_id'] == 'H2')
    assessment.update(verdict='not_supported', evidence_ids=[eid], reason='对照观测与该具体命题冲突')
    return c, data, assessment, eid


def accept(c, data):
    return c.validate_review(ReviewDecision.model_validate_json(json.dumps(data)))


class BoundedRefutationTests(unittest.TestCase):
    def test_default_entry_points_use_latest_contract_and_prompt(self):
        from tests.workflow.test_planned_default import planned_models
        from tests.tools.test_incident_tools import P
        from providers.fixtures import FixtureProvider
        from workflow.coordinator import collaborate
        from scripts.compare_incident import capture
        from runtime.context import Limits
        models = planned_models()
        report = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertEqual(report['review_protection'], BOUNDED_POLICY)
        self.assertEqual(report['review_contract_version'], BOUNDED_POLICY)
        messages = models['reviewer'].histories[0]
        self.assertIn('本轮反证规则', messages[0].content)
        self.assertIn('refutation_basis', json.loads(messages[1].content)['output_schema']['$defs']['Assessment']['properties'])
        captured = capture('case_001', 'multi', lambda _: planned_models(), P, Limits())
        self.assertEqual(captured['review_protection'], BOUNDED_POLICY)
        self.assertEqual(captured['status'], 'completed')

    def test_supported_workflow_does_not_add_model_or_tool_calls(self):
        from tests.workflow.test_planned_default import planned_models
        from tests.tools.test_incident_tools import P
        from providers.fixtures import FixtureProvider
        from workflow.coordinator import Collaboration
        reports = [Collaboration(planned_models(), FixtureProvider('case_001', P), P,
                    review_protection=mode).run() for mode in ('conservative_v1', BOUNDED_POLICY)]
        self.assertEqual(reports[0]['status'], reports[1]['status'])
        for key in ('model_calls', 'tool_calls'):
            self.assertEqual(reports[0]['run_summary'][key], reports[1]['run_summary'][key])
        self.assertEqual(reports[1]['refutation_audit'], [])

    def test_weakening_and_absence_do_not_become_refuted(self):
        for kind in ('weakens_explanation', 'absence_of_support'):
            with self.subTest(kind=kind):
                c, data, a, _ = prepared()
                a['refutation_basis'] = {'kind': kind, 'claim': '尚未确认的触发来源'}
                before = dict(c.context.counts)
                review = accept(c, data)
                self.assertEqual(next(a for a in review.assessments if a.target_id == 'H2').verdict, 'uncertain')
                event = next(e for e in c.events if e['type'] == 'review_protection_applied')
                self.assertEqual(event['original']['refutation_basis']['kind'], kind)
                self.assertEqual(event['policy'], BOUNDED_POLICY)
                self.assertEqual(c.context.counts, before)

    def test_direct_contradiction_is_retained_but_not_certified(self):
        c, data, a, _ = prepared()
        a['refutation_basis'] = {'kind': 'direct_contradiction', 'claim': '待审阅的具体命题'}
        review = accept(c, data)
        self.assertEqual(next(a for a in review.assessments if a.target_id == 'H2').verdict, 'not_supported')
        audit = next(e for e in c.events if e['type'] == 'refutation_basis_retained')
        self.assertFalse(audit['semantic_proof_verified'])

    def test_missing_basis_degrades_without_protocol_retry(self):
        c, data, _, _ = prepared()
        counts = dict(c.context.counts)
        self.assertEqual(next(a for a in accept(c, data).assessments if a.target_id == 'H2').verdict, 'uncertain')
        self.assertEqual(c.context.counts, counts)
        self.assertFalse(any(e['type'] == 'validation_failure' for e in c.events))

    def test_scope_mismatch_cannot_refute(self):
        for field, value in (('service', 'other-service'), ('environment', 'production'),
                             ('observed_from', 'before'), ('observed_to', 'after')):
            with self.subTest(field=field):
                c, data, a, eid = prepared()
                if value == 'before':
                    value = c.ticket.scope.start - timedelta(minutes=1)
                elif value == 'after':
                    value = c.ticket.scope.end + timedelta(minutes=1)
                c.evidence[eid] = c.evidence[eid].model_copy(update={field: value})
                a['refutation_basis'] = {'kind': 'direct_contradiction', 'claim': '某个具体命题'}
                self.assertEqual(next(a for a in accept(c, data).assessments if a.target_id == 'H2').verdict, 'uncertain')
                self.assertTrue(any(e.get('code') == 'COUNTEREVIDENCE_SCOPE_MISMATCH' for e in c.events))

    def test_unknown_and_duplicate_ids_still_fail_before_downgrade(self):
        for duplicate in (False, True):
            c, data, a, eid = prepared()
            a['evidence_ids'] = [eid, eid] if duplicate else ['EV_FAKE']
            with self.assertRaises(ProtocolError):
                accept(c, data)
            self.assertFalse(any(e['type'] == 'review_protection_applied' for e in c.events))

    def test_non_hypothesis_negative_is_not_relaxed(self):
        c, data, a, _ = prepared()
        a['verdict'] = 'uncertain'
        action = next(a for a in data['assessments'] if a['target_id'] == 'A1')
        action.update(verdict='not_supported', evidence_ids=[], refutation_basis=None)
        self.assertEqual(next(a for a in accept(c, data).assessments if a.target_id == 'A1').verdict, 'not_supported')

    def test_existing_mode_keeps_cited_negative_without_basis(self):
        c, data, _, _ = prepared()
        c.review_protection = 'conservative_v1'
        self.assertEqual(next(a for a in accept(c, data).assessments if a.target_id == 'H2').verdict, 'not_supported')

    def test_old_model_schema_hides_new_contract_and_new_schema_is_conditional(self):
        c, _, _, _ = prepared()
        old = reviewer_schema(ReviewDecision.model_json_schema(), c.evidence_needs, c.targets(), conservative=True)
        new = reviewer_schema(ReviewDecision.model_json_schema(), c.evidence_needs, c.targets(),
                              conservative=True, bounded_refutation=True)
        self.assertNotIn('refutation_basis', old['$defs']['Assessment']['properties'])
        self.assertNotIn('RefutationBasis', old['$defs'])
        rule = next(rule for rule in new['$defs']['Assessment']['allOf']
                    if rule['then'].get('required') == ['refutation_basis'])
        self.assertEqual(rule['if']['properties']['verdict']['const'], 'not_supported')
        self.assertEqual(rule['then']['required'], ['refutation_basis'])


if __name__ == '__main__':
    unittest.main()
