from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from evals.rca_cases import build_candidate_cases, build_calibration_cases, validate_reachability, write_dataset
from evals.rca_contracts import load_dataset


class RCACasesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()

    def test_candidate_composition_is_provisional_and_groups_are_not_independent_cases(self):
        candidates = [case for case in self.dataset['cases'] if case['split'] == 'candidate_acceptance']
        self.assertEqual(len(candidates), 30)
        self.assertEqual(Counter(case['truth']['identifiability'] for case in candidates),
            {'root_identifiable': 18, 'mechanism_only': 6, 'not_identifiable': 6})
        self.assertEqual(len({case['independence_group'] for case in candidates}), 25)
        self.assertEqual(self.dataset['validation']['human_gold_cases'], 0)
        self.assertFalse(self.dataset['manifest']['holdout_sealed'])
        for case in candidates:
            self.assertEqual(case['truth']['truth_status'], 'pending_human_review')
            self.assertEqual(case['truth']['provenance']['reproduction_evidence'], [])

    def test_calibration_reuses_only_explicitly_known_source_controls_and_covers_three_layers(self):
        from evals.closed_loop_cases import build_cases
        known = build_cases()
        cases = [case for case in self.dataset['cases'] if case['split'] == 'calibration']
        self.assertEqual(len(cases), 8)
        self.assertEqual({case['truth']['identifiability'] for case in cases},
                         {'root_identifiable', 'mechanism_only', 'not_identifiable'})
        for case in cases:
            provenance = case['truth']['provenance']
            self.assertEqual(case['public']['world'], known[provenance['source_case_id']].observations)
            self.assertIn('calibration only', provenance['notes'][0])

    def test_same_symptom_has_different_causes_and_multifactor_cases(self):
        candidates = build_candidate_cases()
        same = [case for case in candidates if case['scenario_family'] == 'connection_wait']
        self.assertEqual(len(same), 3)
        self.assertEqual(len({case['public']['world']['ticket']['symptoms'] for case in same}), 1)
        explanations = {case['truth']['reference_explanation'] for case in same}
        self.assertEqual(len(explanations), 3)
        multi = [case for case in candidates if len(case['truth']['causal_conditions']) >= 2]
        self.assertGreaterEqual(len(multi), 3)

    def test_candidate_ids_are_neutral_and_no_filename_answer_is_in_runtime_ticket(self):
        for case in build_candidate_cases():
            world = case['public']['world']
            self.assertRegex(world['ticket']['incident_id'], '^eval_observation_[0-9]{3}$')
            self.assertRegex(world['ticket']['scope']['service'], '^component_[0-9]{3}$')
            self.assertTrue(all(row['id'].startswith('row_') for row in world['logs']))

    def test_actual_executor_reachability_captures_unavailable_and_truncated_boundaries(self):
        result = validate_reachability(self.dataset)
        self.assertEqual(len(result['cases']), 38)
        self.assertEqual(result['model_calls'], 0)
        self.assertFalse(result['causal_truth_validated'])
        self.assertTrue(all(case['tool_calls'] <= 8 for case in result['cases']))
        unavailable = next(case for case in result['cases'] if case['case_id'] == 'rca_candidate_025')
        self.assertEqual(unavailable['expected_source_errors'], ['SOURCE_NOT_CONFIGURED'])
        truncated = next(case for case in result['cases'] if case['case_id'] == 'rca_candidate_029')
        self.assertEqual(truncated['content_truncated_sources'], ['get_service_logs/row_01'])

    def test_witness_mismatch_fails_against_real_executor_instead_of_trusting_id_existence(self):
        changed = deepcopy(self.dataset)
        case = changed['cases'][8]
        case['public_diagnostics']['source_queries'][0]['args']['level'] = 'ERROR'
        case['truth']['oracle_queries'][0]['args']['level'] = 'ERROR'
        # IDs still exist in the world, but exact filters no longer retrieve them.
        with self.assertRaisesRegex(ValueError, 'Real tool witness mismatch'):
            validate_reachability(changed)

    def test_public_fixed_controls_fit_current_business_draft_contract(self):
        from workflow.v2.contracts import ClaimInput
        for case in self.dataset['cases']:
            for control in case['public_diagnostics']['fixed_controls']:
                ClaimInput(statement=control['statement'], level=control['level'])

    def test_materialization_is_deterministic_and_prevents_accidental_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            created = write_dataset(path)
            self.assertEqual(created['fingerprint'], self.dataset['fingerprint'])
            with self.assertRaises(FileExistsError):
                write_dataset(path)


if __name__ == '__main__':
    unittest.main()
