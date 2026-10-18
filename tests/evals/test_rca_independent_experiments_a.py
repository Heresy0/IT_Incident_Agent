import json
import unittest

from evals.rca_independent_experiments_a import (CASE_IDS, run_experiments,
    reproduce_sqlite_transaction_lock, reproduce_sqlite_missing_index,
    reproduce_hmac_key_rotation, reproduce_gzip_encoding_mismatch)


class RCAIndependentExperimentsATests(unittest.TestCase):
    def test_sqlite_held_transaction_times_out_then_same_write_recovers(self):
        result = reproduce_sqlite_transaction_lock(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        trial = result['trials'][0]
        self.assertEqual(trial['baseline']['value_after'], 1)
        self.assertEqual(trial['fault']['error_code'], 'SQLITE_BUSY')
        self.assertEqual(trial['fault']['error_message'], 'database is locked')
        self.assertTrue(trial['fault']['holder_transaction_open'])
        self.assertGreaterEqual(trial['fault']['elapsed_ms'], 10)
        self.assertEqual(trial['restored']['value_after'], 2)
        self.assertTrue(trial['holder_transaction_closed'])
        self.assertTrue(trial['writer_transaction_closed'])
        rows = {row['observation_id']: row for row in result['public_rows']}
        self.assertEqual(rows['row_02']['message']['failed_write_results'][0], {'repeat': 1, **trial['fault']})
        self.assertEqual(rows['row_03']['message']['phase_results'][0]['committed_values'], [1, None, 2])

    def test_missing_index_uses_actual_query_plan_and_vm_budget(self):
        result = reproduce_sqlite_missing_index(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        trial = result['trials'][0]
        self.assertEqual(trial['baseline']['returned_payloads'], ['payload_3999'])
        self.assertEqual(trial['fault']['error_code'], 'SQLITE_INTERRUPT')
        self.assertEqual(trial['fault']['interrupt_reason'], 'vm_instruction_budget')
        self.assertEqual(trial['fault']['observed_vm_progress_instructions'], 1000)
        self.assertTrue(any('SCAN records' in item for item in trial['fault']['query_plan']))
        self.assertTrue(any('SEARCH' in item for item in trial['restored']['query_plan']))
        self.assertEqual(trial['restored']['returned_payloads'], trial['baseline']['returned_payloads'])
        rows = {row['observation_id']: row for row in result['public_rows']}
        self.assertEqual(rows['row_02']['message']['failed_lookup_results'][0], {'repeat': 1, **trial['fault']})
        self.assertEqual(rows['row_03']['message']['phase_results'][0]['query_plans'],
            [trial[phase]['query_plan'] for phase in ('baseline', 'fault', 'restored')])

    def test_hmac_reuses_fault_signature_after_only_verifier_key_change(self):
        result = reproduce_hmac_key_rotation(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        trial = result['trials'][0]
        self.assertTrue(trial['baseline']['verified'])
        self.assertFalse(trial['fault']['verified'])
        self.assertTrue(trial['restored']['verified'])
        self.assertEqual(trial['fault']['signature_sha256'], trial['restored']['signature_sha256'])
        self.assertNotEqual(trial['fault']['verifier_key_id'], trial['restored']['verifier_key_id'])
        self.assertEqual(trial['fault']['payload_sha256'], trial['restored']['payload_sha256'])
        rows = {row['observation_id']: row for row in result['public_rows']}
        self.assertEqual(rows['row_02']['message']['verification_results'][0], {'repeat': 1, **trial['fault']})
        self.assertEqual(rows['row_03']['message']['phase_results'][0]['verified'], [True, False, True])
        self.assertNotIn('key_material', str(rows))

    def test_gzip_real_bytes_fail_wrong_decoder_and_recover_identically(self):
        result = reproduce_gzip_encoding_mismatch(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        trial = result['trials'][0]
        self.assertEqual(trial['baseline']['decoded_document'], {'request': 1, 'value': 'local-observation'})
        self.assertEqual(trial['fault']['outcome'], 'failed')
        self.assertEqual(trial['fault']['content_encoding'], 'deflate')
        self.assertTrue(trial['fault']['body_prefix_hex'].startswith('1f8b'))
        self.assertEqual(trial['fault']['body_sha256'], trial['restored']['body_sha256'])
        self.assertEqual(trial['baseline']['decoded_document'], trial['restored']['decoded_document'])
        rows = {row['observation_id']: row for row in result['public_rows']}
        self.assertEqual(rows['row_02']['message']['decode_failures'][0], {'repeat': 1, **trial['fault']})
        self.assertEqual(rows['row_03']['message']['phase_results'][0]['outcomes'], ['decoded', 'failed', 'decoded'])

    def test_all_four_families_execute_three_phases_in_three_repeats(self):
        results = run_experiments(repeats=3)
        self.assertEqual(tuple(result['case_id'] for result in results), CASE_IDS)
        self.assertEqual(len({result['family'] for result in results}), 4)
        for result in results:
            self.assertEqual(result['status'], 'passed', result['checks'])
            self.assertEqual(result['origin'], 'executed_synthetic_experiment')
            self.assertFalse(result['human_verified'])
            self.assertFalse(result['is_gold'])
            self.assertFalse(result['causal_truth_validated'])
            self.assertFalse(result['original_incident_reproduced'])
            self.assertFalse(result['business_application_imported'])
            self.assertEqual(result['model_calls'], 0)
            self.assertEqual([trial['repeat'] for trial in result['trials']], [1, 2, 3])
            for trial in result['trials']:
                self.assertTrue(all(phase in trial for phase in ('baseline', 'fault', 'restored')))
            self.assertEqual(result['reference']['key_observation_ids'], ['row_01', 'row_02', 'row_03'])
            for row in result['public_rows']:
                self.assertNotIn('reference', row)
                self.assertNotIn('causal_condition', row['message'])
                self.assertNotIn('reference_explanation', row['message'])
                self.assertLessEqual(len(json.dumps(row['message'], ensure_ascii=False,
                    separators=(',', ':'))), 1100)

    def test_repeat_bounds_reject_before_execution(self):
        for value in (0, 6, True, 1.5, '3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_experiments(value)


if __name__ == '__main__':
    unittest.main()
