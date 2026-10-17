"""Outcome projection must not turn more reports into better diagnoses."""
from copy import deepcopy
import unittest

from tests.tools.test_incident_tools import executor, window
from workflow.v2.contracts import parse_draft
from workflow.v2.outcome import classify_outcome
from workflow.v2.review import ReviewBatch, evidence_pack_version, parse_review


class InvestigationOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.ex = executor(role='single')
        self.ex.execute('get_service_metrics', {**window(self.ex), 'metrics': ['db_cpu']})
        self.ticket = self.ex.provider.ticket()

    def draft(self, *, claims=True, level='mechanism', action=False, gaps=None):
        value = {'claims': [{'statement': '模型判断的有限机制', 'level': level, 'refs': ['R1']}] if claims else [],
                 'next_steps': [{'action': '取得授权后只读核对本地记录', 'refs': ['R1']}] if action else [],
                 'gaps': gaps or []}
        draft, errors = parse_draft(value, self.ex)
        self.assertEqual(errors, [])
        return draft

    def reviewed(self, draft, relation='supported', decision='appropriate'):
        pack = evidence_pack_version(self.ex)
        values = [{'target_id': target.id, 'relation': relation, 'refs': ['R1'], 'issue': '固定语义审阅结果'}
                  for target in draft.claims]
        values.extend({'target_id': target.id, 'decision': decision, 'refs': ['R1'], 'issue': '固定建议适当性结果'}
                      for target in draft.next_steps)
        items, errors = parse_review({'items': values}, draft, self.ex, evidence_pack_version=pack)
        self.assertEqual(errors, [])
        return ReviewBatch(items=list(items.values()), evidence_pack_version=pack, attempts=1)

    def outcome(self, draft=None, reviews=None, failures=None, stop='COLLECTION_BUDGET_EXHAUSTED'):
        draft = self.draft() if draft is None else draft
        reviews = self.reviewed(draft) if reviews is None else reviews
        return classify_outcome(draft, reviews, self.ex, failures or [], stop, self.ticket)

    def test_supported_mechanism_can_finish_without_artificial_advice(self):
        result = self.outcome()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['review_status'], 'passed')
        self.assertEqual(result['reason'], 'FINISHED')
        self.assertEqual(result['business_result'], 'diagnosis_available')
        self.assertEqual(result['run_outcome']['completion_basis'], 'model_reviewed_bounded_mechanism')
        self.assertFalse(result['run_outcome']['causality_verified_by_program'])

    def test_known_unknown_does_not_erase_supported_bounded_result(self):
        result = self.outcome(self.draft(gaps=['为何开始仍未知']))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['run_outcome']['investigation']['status'], 'limited')
        self.assertEqual(result['run_outcome']['investigation']['known_unknowns'], ['为何开始仍未知'])
        self.assertEqual(result['diagnosis_readiness']['report_status'], 'reviewed_with_limitations')

    def test_facts_and_appropriate_advice_do_not_substitute_for_mechanism(self):
        draft = self.draft(claims=False, action=True)
        result = self.outcome(draft)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'NO_SUPPORTED_MECHANISM')
        self.assertEqual(result['review_status'], 'passed')

    def test_supported_trigger_and_all_refutations_are_not_diagnoses(self):
        for level, relation in [('trigger', 'supported'), ('alternative', 'supported'), ('mechanism', 'contradicted')]:
            with self.subTest(level=level, relation=relation):
                draft = self.draft(level=level)
                result = self.outcome(draft, self.reviewed(draft, relation=relation))
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['reason'], 'NO_SUPPORTED_MECHANISM')

    def test_unreviewed_and_partially_supported_claims_are_not_completed(self):
        draft = self.draft()
        for reviews, reason in [(ReviewBatch(), 'REVIEW_NOT_PERFORMED'),
                                (self.reviewed(draft, relation='partial'), 'REVIEW_NEEDS_INFORMATION')]:
            with self.subTest(reason=reason):
                result = self.outcome(draft, reviews)
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['reason'], reason)

    def test_rejected_advice_prevents_claiming_all_targets_passed(self):
        draft = self.draft(action=True)
        result = self.outcome(draft, self.reviewed(draft, decision='out_of_scope'))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['review_status'], 'needs_information')
        self.assertEqual(result['run_outcome']['review']['unresolved_target_ids'], ['A1'])

    def test_same_collection_boundary_reports_actual_remaining_blocker(self):
        success = self.outcome()
        draft = self.draft()
        unreviewed = self.outcome(draft, ReviewBatch())
        failed = self.outcome(failures=[{'stage': 'draft', 'category': 'shape', 'code': 'INVALID_JSON'}])
        self.assertEqual(success['reason'], 'FINISHED')
        self.assertEqual(unreviewed['reason'], 'REVIEW_NOT_PERFORMED')
        self.assertEqual(failed['reason'], 'INVALID_JSON')
        for result in [success, unreviewed, failed]:
            self.assertEqual(result['run_outcome']['execution']['collection_stop'], 'COLLECTION_BUDGET_EXHAUSTED')

    def test_unrecovered_fault_blocks_but_recovered_protocol_fault_does_not(self):
        for code in ['STEP_INVALID', 'TOOL_DENIED', 'BUDGET_EXCEEDED', 'TIME_BUDGET_EXCEEDED']:
            with self.subTest(code=code):
                failure = {'stage': 'investigate', 'code': code}
                result = self.outcome(failures=[failure])
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['reason'], code)
                self.assertEqual(result['review_status'], 'passed')
                self.assertEqual(result['run_outcome']['investigation']['status'], 'handoff')
                self.assertEqual(self.outcome(failures=[{**failure, 'recovered': True}])['status'], 'completed')

    def test_actual_hard_stop_is_not_a_normal_reservation_boundary(self):
        result = self.outcome(stop='BUDGET_EXCEEDED')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'BUDGET_EXCEEDED')

    def test_source_error_and_truncation_remain_explicit_handoff(self):
        key = next(iter(self.ex.check_results))
        original = deepcopy(self.ex.check_results[key])
        for status, truncated, error, reason in [('error', False, {'code': 'SOURCE_NOT_CONFIGURED'}, 'SOURCE_UNAVAILABLE'),
                                                 ('ok', True, None, 'SOURCE_TRUNCATED')]:
            with self.subTest(reason=reason):
                self.ex.check_results[key] = {**original, 'status': status, 'truncated': truncated, 'error': error}
                draft = self.draft()
                result = self.outcome(draft, self.reviewed(draft))
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['review_status'], 'passed')
                self.assertEqual(result['run_outcome']['investigation']['status'], 'handoff')
                self.assertTrue(result['run_outcome']['investigation']['source_limitations'])

    def test_stale_pack_or_target_does_not_carry_approval(self):
        draft = self.draft()
        reviews = self.reviewed(draft)
        self.ex.execute('get_service_logs', {**window(self.ex), 'category': 'dependency'})
        result = self.outcome(draft, reviews)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['review_status'], 'not_performed')

    def test_no_observation_cannot_be_completed(self):
        draft = self.draft().model_copy(update={'facts': []})
        result = self.outcome(draft)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'NO_CURRENT_OBSERVATIONS')

    def test_status_check_needs_observations_but_no_causal_claim(self):
        ticket = self.ticket.model_copy(update={'purpose': 'status_check'})
        draft = self.draft(claims=False)
        result = classify_outcome(draft, ReviewBatch(), self.ex, [], 'FINISHED', ticket)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['business_result'], 'status_checked')
        self.assertEqual(result['review_status'], 'not_performed')
        self.assertEqual(result['reason'], 'STATUS_CHECK_COMPLETED')

    def test_projection_has_no_side_effects(self):
        draft = self.draft(gaps=['有限未知'])
        reviews = self.reviewed(draft)
        failures = [{'stage': 'draft', 'code': 'INVALID_JSON', 'recovered': True}]
        before = (draft.model_dump(), reviews.model_dump(), deepcopy(failures), deepcopy(self.ex.check_results))
        self.outcome(draft, reviews, failures)
        after = (draft.model_dump(), reviews.model_dump(), failures, self.ex.check_results)
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
