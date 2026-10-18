"""Missing evidence, unfinished investigation and contradictory review stay distinct."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.contracts import Assessment, EvidenceNeed, NeedAssessment, ReviewBlocker, ReviewDecision
from evidence.diagnosis import ProtocolError
from providers.fixtures import ProviderError
from tests.tools.test_incident_tools import executor, window
from workflow.diagnostic_audit import diagnostic_audit, validate_blockers, blocker_follow_up


class DiagnosticAuditTests(unittest.TestCase):
    def setUp(self):
        self.ex = executor()
        self.executors = {'investigation': self.ex, 'knowledge': executor(role='knowledge')}
        self.check = {'tool': 'get_recent_changes', 'args': window(self.ex)}
        self.need = EvidenceNeed(need_id='N2', purpose='mechanism', question='近端机制是什么？',
                                 reason='机制尚未确认。')
        self.needs = {'N2': self.need}

    def review(self, *, blocker=True, target_verdict='uncertain', **changes):
        value = {'cause_level': 'mechanism', 'affected_targets': ['H1'],
            'missing_observation': '需要窗口内的配置变更记录。',
            'decision_impact': '若存在编码器变更则支持兼容性机制，否则需其他解释。',
            'resolution': 'scoped_check', 'checks': [self.check]}
        value.update(changes)
        return ReviewDecision(assessments=[Assessment(target_id='H1', verdict=target_verdict, reason='待验证。')],
            need_assessments=[NeedAssessment(need_id='N2', verdict='uncertain', reason='需要补查。',
                blocker=ReviewBlocker.model_validate(value) if blocker else None)])

    def audit(self, review, tasks=()):
        return diagnostic_audit(self.needs, review, None, self.executors, tasks,
                                stop='BUDGET_EXCEEDED', events=[])

    def test_live_uncertainty_requires_specific_blocker_without_auto_support(self):
        review = self.review(blocker=False)
        with self.assertRaisesRegex(ProtocolError, 'REVIEW_BLOCKER_REQUIRED'):
            validate_blockers(self.needs, review, self.executors, required=True)
        self.assertEqual(review.need_assessments[0].verdict, 'uncertain')

    def test_trigger_cannot_replace_mechanism_scope(self):
        with self.assertRaisesRegex(ProtocolError, 'REVIEW_BLOCKER_SCOPE'):
            validate_blockers(self.needs, self.review(cause_level='trigger'), self.executors)

    def test_trigger_cannot_be_relabelled_as_an_alternative_mechanism(self):
        needs = {'N2': self.need.model_copy(update={'purpose': 'alternative'})}
        with self.assertRaisesRegex(ProtocolError, 'REVIEW_BLOCKER_SCOPE'):
            validate_blockers(needs, self.review(cause_level='trigger'), self.executors)

    def test_blocker_cannot_simultaneously_approve_all_affected_targets(self):
        with self.assertRaisesRegex(ProtocolError, 'REVIEW_BLOCKER_CONFLICT'):
            validate_blockers(self.needs, self.review(target_verdict='supported'), self.executors)

    def test_unknown_target_and_role_mismatch_fail_before_any_execution(self):
        for changes, error in (({'affected_targets': ['H2']}, 'REVIEW_BLOCKER_TARGET'),
                               ({'target_role': 'knowledge'}, 'INVALID_BLOCKER_CHECK')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ProtocolError, error):
                validate_blockers(self.needs, self.review(**changes), self.executors)
        self.assertEqual(self.ex.context.counts['tool_calls'], 0)

    def test_external_claim_cannot_hide_an_executable_check(self):
        with self.assertRaisesRegex(ProtocolError, 'REVIEW_BLOCKER_CHANNEL_CONFLICT'):
            validate_blockers(self.needs, self.review(resolution='external_source'), self.executors)

    def test_budget_stop_does_not_make_unexecuted_check_unavailable(self):
        review = self.review()
        validate_blockers(self.needs, review, self.executors, required=True)
        audit = self.audit(review, [{'role': 'investigation', 'goal': '检查配置变更',
            'need_ids': ['N2'], 'required_checks': [self.check], 'expected_value': '区分机制'}])
        self.assertEqual(audit['gaps'][0]['category'], 'investigation_incomplete')
        self.assertEqual(audit['investigation_status'], 'incomplete')
        self.assertEqual(audit['termination_reason'], 'BUDGET_EXCEEDED')

    def test_actual_empty_query_is_scoped_insufficiency_not_channel_absence(self):
        with patch.object(self.ex.provider, 'query', return_value=([], False)):
            self.ex.execute(self.check['tool'], self.check['args'])
        gap = self.audit(self.review())['gaps'][0]
        self.assertEqual(gap['category'], 'selected_scope_insufficient')
        self.assertTrue(gap['query_checks'][0]['query_complete'])
        self.assertFalse(gap['semantic_necessity_verified_by_program'])

    def test_actual_query_failure_has_its_own_category(self):
        with patch.object(self.ex.provider, 'query', side_effect=ProviderError('UPSTREAM_TIMEOUT')):
            self.ex.execute(self.check['tool'], self.check['args'])
        self.assertEqual(self.audit(self.review())['gaps'][0]['category'], 'retrieval_failed')

    def test_useful_blocker_is_handed_to_supervisor_without_executing(self):
        review = self.review()
        validate_blockers(self.needs, review, self.executors, required=True)
        request = blocker_follow_up(self.needs, review, self.executors)
        self.assertEqual((request.need_id, request.target_id), ('N2', 'H1'))
        self.assertEqual(request.checks[0].tool, 'get_recent_changes')
        self.assertEqual(self.ex.context.counts['tool_calls'], 0)

    def test_completed_empty_query_is_not_handed_back_as_a_new_check(self):
        with patch.object(self.ex.provider, 'query', return_value=([], False)):
            self.ex.execute(self.check['tool'], self.check['args'])
        self.assertIsNone(blocker_follow_up(self.needs, self.review(), self.executors))

    def test_successful_query_does_not_prove_causal_support(self):
        self.ex.execute(self.check['tool'], self.check['args'])
        audit = self.audit(self.review())
        self.assertEqual(audit['mechanism_status'], 'unconfirmed')
        self.assertEqual(audit['gaps'][0]['category'], 'unresolved_review')

    def test_external_input_is_identified_as_a_review_claim(self):
        gap = self.audit(self.review(resolution='external_source', checks=[]))['gaps'][0]
        self.assertEqual(gap['category'], 'external_input_required')
        self.assertFalse(gap['semantic_necessity_verified_by_program'])

    def test_suspected_overblocking_never_changes_the_verdict(self):
        review = self.review(blocker=False, target_verdict='supported')
        review = review.model_copy(update={'assessments': [*review.assessments,
            Assessment(target_id='A1', verdict='supported', reason='人工只读核查有效。')]})
        draft = SimpleNamespace(hypotheses=[SimpleNamespace(level='mechanism', hypothesis_id='H1')],
                                recommended_actions=[object()])
        audit = diagnostic_audit(self.needs, review, draft, self.executors, [], stop='REVIEW_INCOMPLETE', events=[])
        self.assertEqual(audit['overblocking_suspected_need_ids'], ['N2'])
        self.assertEqual(review.need_assessments[0].verdict, 'uncertain')
        self.assertEqual(audit['termination_reason'], 'REVIEW_INCOMPLETE')

    def test_supervisor_support_is_not_reported_as_successful_review(self):
        needs = {'N2': self.need.model_copy(update={'status': 'supported'})}
        audit = diagnostic_audit(needs, None, None, self.executors, [], stop='MODEL_OUTPUT_INVALID', events=[])
        self.assertEqual(audit['gaps'][0]['category'], 'review_not_completed')
        self.assertEqual(audit['partial_origin'], 'protocol_or_execution_failure')

    def test_model_service_failure_is_not_an_evidence_limitation(self):
        audit = diagnostic_audit(self.needs, None, None, self.executors, [], stop='MODEL_ERROR', events=[])
        self.assertEqual(audit['partial_origin'], 'protocol_or_execution_failure')
        self.assertEqual(audit['termination_reason'], 'MODEL_ERROR')

    def test_end_to_end_handoff_preserves_planning_budget_and_action_gate(self):
        from tests.workflow.test_review_readiness import ReviewReadinessTests
        calls = 0
        def review_change(data):
            nonlocal calls
            calls += 1
            if calls != 1:
                return
            next(a for a in data['assessments'] if a['target_id'] == 'H1').update(
                verdict='uncertain', reason='还需检查配置变更以区分机制。')
            next(a for a in data['need_assessments'] if a['need_id'] == 'N2').update(
                verdict='uncertain', reason='机制需要补查。',
                blocker=self.review().need_assessments[0].blocker.model_dump(mode='json'))
        report, _ = ReviewReadinessTests().run_report(review_change=review_change)
        # The legacy scripted diagnosis repeats the now-completed query as its
        # only advice. Handoff must not bypass the existing action gate.
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['run_summary']['termination_reason'], 'DIAGNOSIS_INCOMPLETE')
        self.assertEqual(report['rework_rounds'], 1)
        self.assertLessEqual(report['run_summary']['model_calls'], 16)
        events = report['events']
        handoff = next(i for i, e in enumerate(events) if e['type'] == 'review_blocker_handoff')
        query = next(i for i, e in enumerate(events) if e['type'] == 'tool_result'
                     and e['name'] == 'get_recent_changes')
        self.assertLess(handoff, query)
        self.assertTrue(any(e['type'] == 'phase' and e.get('node') == 'supervisor'
                            for e in events[handoff:query]))


if __name__ == '__main__':
    unittest.main()
