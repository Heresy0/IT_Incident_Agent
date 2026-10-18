"""Synthetic unit fixtures are not human review records or case gold."""
import unittest

from evals.rca_scoring import (EVIDENCE_FIELDS, ROOT_FIELDS, ReviewError,
    aggregate_case_metrics, resource_metrics, score_run, mechanical_references_valid)


def judgments():
    evidence = {name: True for name in EVIDENCE_FIELDS}
    root = {name: True for name in ROOT_FIELDS}
    root.update(wrong_definitive_cause=False, unsupported_definitive_cause=False, abstained_from_root=False)
    return evidence, root


class RcaScoringTests(unittest.TestCase):
    def test_reference_value_path_and_identity_cannot_be_overridden_by_human_booleans(self):
        from copy import deepcopy
        base = {'normalized': {'observations': [{'evidence_id': 'E1', 'payload': {'nested': {'value': 7}},
            'source_fields': {'allowed_field_paths': ['nested.value']}}], 'audit_items': [{
            'refs': [{'evidence_id': 'E1', 'field_path': 'nested.value', 'value': 7}]}]}}
        self.assertTrue(mechanical_references_valid(base))
        for field, value in (('evidence_id', 'invented'), ('field_path', 'nested..value'), ('value', 8)):
            changed = deepcopy(base)
            changed['normalized']['audit_items'][0]['refs'][0][field] = value
            self.assertFalse(mechanical_references_valid(changed))
        e, r = judgments()
        self.assertFalse(score_run(e, r, 'root_identifiable', has_output=True,
            mechanical_references_valid=False)['strict_root_success'])

    def test_budget_missing_is_unassessed_and_actual_overrun_is_retained(self):
        out = resource_metrics({})
        self.assertEqual(out['hard_budget']['status'], 'unassessed')
        report = {'run_summary': {'model_calls': 17, 'tool_calls': 2, 'web_calls': 0,
            'duration_ms': 1000, 'limits': {'model_calls': 16, 'tool_calls': 8, 'web_calls': 0, 'seconds': 180}},
            'worker_wall_seconds': 2.5}
        out = resource_metrics(report)
        self.assertEqual(out['hard_budget']['status'], 'violation')
        self.assertIn('model_calls', out['hard_budget']['violations'])
        self.assertEqual(out['tool_calls'], 2)
        self.assertEqual(out['worker_wall_seconds'], 2.5)

    def test_strict_requires_every_causal_link_and_boundary(self):
        e, r = judgments()
        self.assertTrue(score_run(e, r, 'root_identifiable', has_output=True)['strict_root_success'])
        for name in ('causal_condition_correct', 'causal_chain_correct', 'mechanism_correct', 'root_claim_made'):
            changed = dict(r, **{name: False})
            self.assertFalse(score_run(e, changed, 'root_identifiable', has_output=True)['strict_root_success'], name)
        for name in ('scope_correct', 'evidence_chain_sufficient', 'unknown_boundary_preserved'):
            changed = dict(r, **{name: False})
            self.assertFalse(score_run(e, changed, 'root_identifiable', has_output=True)['strict_root_success'], name)

    def test_empty_timeout_or_invalid_reference_never_strict_success(self):
        e, r = judgments()
        for options in ({'has_output': False}, {'has_output': True, 'delivery_failed': True},
                        {'has_output': True, 'mechanical_references_valid': False}):
            self.assertFalse(score_run(e, r, 'root_identifiable', **options)['strict_root_success'])

    def test_all_unknown_does_not_succeed_on_answerable_case(self):
        e, r = judgments()
        r.update(root_claim_made=False, abstained_from_root=True)
        out = score_run(e, r, 'root_identifiable', has_output=True)
        self.assertFalse(out['strict_root_success'])
        self.assertTrue(out['answerable_abstention'])

    def test_unknown_requires_missing_information_and_discriminating_step(self):
        e, r = judgments()
        r.update(root_claim_made=False, abstained_from_root=True)
        self.assertTrue(score_run(e, r, 'not_identifiable', has_output=True)['unknown_boundary_success'])
        for name in ('scope_correct', 'unknown_boundary_preserved',
                'missing_discriminating_information_identified', 'discriminating_next_step_proposed'):
            self.assertFalse(score_run(e, dict(r, **{name: False}), 'not_identifiable', has_output=True)['unknown_boundary_success'])

    def test_mechanism_case_rejects_unobservable_hidden_root(self):
        e, r = judgments()
        self.assertFalse(score_run(e, r, 'mechanism_only', has_output=True)['mechanism_success'])
        r.update(root_claim_made=False, abstained_from_root=True)
        self.assertTrue(score_run(e, r, 'mechanism_only', has_output=True)['mechanism_success'])

    def test_wrong_and_unsupported_are_separate(self):
        e, r = judgments()
        r['unsupported_definitive_cause'] = True
        out = score_run(e, r, 'root_identifiable', has_output=True)
        self.assertFalse(out['wrong_confirmation'])
        self.assertTrue(out['unsupported_confirmation'])
        self.assertTrue(out['wrong_or_unsupported_confirmation'])
        self.assertFalse(out['strict_root_success'])

    def test_contradictory_or_incomplete_human_fields_rejected(self):
        e, r = judgments()
        with self.assertRaisesRegex(ReviewError, 'CONTRADICTORY'):
            score_run(e, dict(r, abstained_from_root=True), 'root_identifiable', has_output=True)
        with self.assertRaisesRegex(ReviewError, 'COMPLETE_BINARY'):
            score_run(e, dict(r, scope_correct=None), 'root_identifiable', has_output=True)

    def test_case_majority_not_repetition_as_independent_case(self):
        e, r = judgments()
        rows = []
        for case, successes in (('a', 2), ('b', 0)):
            for repeat in range(1, 4):
                m = score_run(e, r, 'root_identifiable', has_output=repeat <= successes)
                rows.append({'condition': 'base', 'mode': 'e2e', 'case_id': case,
                    'repeat': repeat, 'identifiability': 'root_identifiable', 'metrics': m})
        out = aggregate_case_metrics(rows, expected_repeats=3)['base:e2e']
        self.assertEqual(out['strict_root_location_run_rate']['denominator'], 6)
        self.assertEqual(out['strict_root_location_case_majority'], {'numerator': 1, 'denominator': 2, 'rate': .5})
        self.assertEqual(out['root_case_successes_out_of_repeats'], {'0': 1, '1': 0, '2': 1, '3': 0})
        with self.assertRaisesRegex(ReviewError, 'INCOMPLETE_CASE_REPEAT'):
            aggregate_case_metrics(rows[:-1], expected_repeats=3)

    def test_raw_quota_code_corrects_legacy_other_category(self):
        report = {'normalized': {'failures': [{'category': 'other', 'code': 'QUOTA_EXCEEDED', 'recovered': False}]},
            'model_call_log': [{'charged_request': True, 'model': 'test', 'usage': None, 'seconds': .1,
                'error': {'code': 'QUOTA_EXCEEDED'}}]}
        out = resource_metrics(report)
        self.assertEqual(out['provider_failures'], 1)
        self.assertEqual(out['provider_failure_codes'], {'QUOTA_EXCEEDED': 1})
        self.assertIsNone(out['cost']['estimated_total'])
        self.assertIsNone(out['input_tokens_total'])
        self.assertEqual(out['unknown_usage_calls'], 1)

    def test_actual_provider_aliases_classified(self):
        for code in ('AUTH_ERROR', 'RATE_LIMIT', 'MODEL_ERROR', 'PROVIDER_AUTH_MISSING'):
            out = resource_metrics({'model_call_log': [{'charged_request': True, 'model': 'test',
                'usage': None, 'error': {'code': code}}]})
            self.assertEqual(out['provider_failures'], 1, code)

    def test_repetition_includes_finish_denominator(self):
        out = resource_metrics({'native_report': {'events': [{'type': 'query_reused', 'role': 'investigation'}]},
            'decision_trace': [{'stage': 'investigate'}, {'stage': 'investigate', 'decision': 'finish'}, {'stage': 'revise'}]})
        self.assertEqual(out['collection_decisions'], 2)
        self.assertEqual(out['repeated_fraction'], .5)

    def test_unknown_usage_remains_unknown_with_valid_rate_card(self):
        rate = {'currency': 'CNY', 'pricing_scope': 'test', 'effective_date': '2026-10-18',
            'source_url': 'https://example.com/test', 'models': {'test': {'max_input_tokens': 1000,
                'input_per_million': 1, 'output_per_million': 2}}}
        calls = [{'charged_request': True, 'model': 'test', 'usage': {'input_tokens': 100, 'output_tokens': 10}},
                 {'charged_request': True, 'model': 'test', 'usage': None}]
        out = resource_metrics({'model_call_log': calls}, rate)
        self.assertIsNone(out['cost']['estimated_total'])
        self.assertAlmostEqual(out['cost']['known_usage_estimated_subtotal'], .00012)
        self.assertEqual(out['cost']['unpriced_calls'], 1)


if __name__ == '__main__':
    unittest.main()
