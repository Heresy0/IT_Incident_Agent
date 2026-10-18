import json
import unittest

from evals.rca_independent_experiments_b import (CASE_IDS, run_experiments,
    reproduce_http_content_length, reproduce_character_decoding,
    reproduce_stale_local_cache, reproduce_url_prefix_routing)


class RCAIndependentExperimentsBTests(unittest.TestCase):
    def test_actual_http_body_reader_captures_incomplete_read_and_restoration(self):
        result = reproduce_http_content_length(repeats=2)
        self.assertEqual(result['status'], 'passed', result['checks'])
        self.assertEqual(result['family'], 'http_response_framing')
        for trial in result['trials']:
            baseline, fault, restored = (trial[name] for name in ('baseline', 'fault', 'restored'))
            self.assertTrue(baseline['success'])
            self.assertFalse(fault['success'])
            self.assertEqual(fault['exception_type'], 'IncompleteRead')
            self.assertEqual(fault['expected_additional_bytes'], 11)
            self.assertEqual(fault['received_bytes'], fault['wire_body_bytes'])
            self.assertEqual(fault['parsed_content_length'], fault['wire_body_bytes'] + 11)
            self.assertTrue(restored['success'])
            self.assertEqual(baseline['wire_body_sha256'], fault['wire_body_sha256'])
            self.assertEqual(fault['wire_body_sha256'], restored['wire_body_sha256'])
            self.assertEqual(baseline['received_sha256'], restored['received_sha256'])
        table = result['public_rows'][1]['message']
        for measured, trial in zip(table['measurements'], result['trials']):
            projected = dict(zip(table['columns'], measured))
            self.assertEqual(projected['repeat'], trial['repeat'])
            for key in set(projected) - {'repeat'}:
                self.assertEqual(projected[key], trial['fault'][key])

    def test_actual_strict_decode_error_has_unchanged_input_and_restored_text(self):
        result = reproduce_character_decoding(repeats=2)
        self.assertEqual(result['status'], 'passed', result['checks'])
        for trial in result['trials']:
            baseline, fault, restored = (trial[name] for name in ('baseline', 'fault', 'restored'))
            self.assertEqual(baseline['decoded_text'], '订单已支付')
            self.assertEqual(restored['decoded_text'], baseline['decoded_text'])
            self.assertEqual(fault['exception_type'], 'UnicodeDecodeError')
            self.assertEqual(fault['error_start'], 0)
            self.assertGreater(fault['error_end'], fault['error_start'])
            self.assertEqual({baseline['input_sha256'], fault['input_sha256'],
                restored['input_sha256']}, {baseline['input_sha256']})
        self.assertEqual(result['public_rows'][0]['message']['measurements'][0]['changed_encoding'],
            result['trials'][0]['fault']['configured_encoding'])

    def test_actual_stale_cache_retains_old_entry_then_fetches_after_eviction(self):
        result = reproduce_stale_local_cache(repeats=2)
        self.assertEqual(result['status'], 'passed', result['checks'])
        for trial in result['trials']:
            baseline, fault, restored = (trial[name] for name in ('baseline', 'fault', 'restored'))
            self.assertEqual(baseline['read_result']['value'], 12)
            self.assertEqual(fault['source_snapshot']['value'], 9)
            self.assertEqual(fault['read_result']['value'], 12)
            self.assertEqual(fault['read_result']['version'], 1)
            self.assertEqual(fault['source_snapshot']['version'], 2)
            self.assertTrue(fault['read_result']['cache_hit'])
            self.assertEqual(fault['read_result']['source_reads_for_lookup'], 0)
            self.assertEqual(trial['source_update']['invalidations_performed'], 0)
            self.assertTrue(trial['invalidation']['removed'])
            self.assertEqual(restored['read_result']['value'], 9)
            self.assertEqual(restored['read_result']['version'], 2)
            self.assertFalse(restored['read_result']['cache_hit'])
            self.assertEqual(restored['read_result']['source_reads_for_lookup'], 1)
            self.assertEqual(fault['source_snapshot'], restored['source_snapshot'])
            self.assertEqual(fault['source_snapshot'], trial['direct_source_read_control'])
        self.assertTrue(any('future updates' in item for item in result['limitations']))

    def test_actual_url_parser_and_lookup_keep_query_and_table_fixed(self):
        result = reproduce_url_prefix_routing(repeats=2)
        self.assertEqual(result['status'], 'passed', result['checks'])
        for trial in result['trials']:
            baseline, fault, restored = (trial[name] for name in ('baseline', 'fault', 'restored'))
            self.assertEqual([phase['response_status'] for phase in (baseline, fault, restored)],
                [200, 404, 200])
            self.assertEqual(fault['parsed_path'], '/api/v2/orders/42')
            self.assertFalse(fault['route_matched'])
            self.assertTrue(trial['direct_route_control']['route_matched'])
            self.assertEqual({phase['parsed_query'] for phase in (baseline, fault, restored)},
                {'include=items'})
            self.assertEqual(len({phase['route_table_sha256'] for phase in (baseline, fault, restored)}), 1)

    def test_all_independent_cases_publish_only_facts_and_never_human_gold(self):
        results = run_experiments(repeats=3)
        self.assertEqual(tuple(result['case_id'] for result in results), CASE_IDS)
        self.assertEqual(len({result['family'] for result in results}), 4)
        self.assertEqual(sum(len(result['checks']) for result in results), 48)
        for result in results:
            self.assertEqual(result['origin'], 'executed_synthetic_experiment')
            self.assertFalse(result['human_verified'])
            self.assertFalse(result['is_gold'])
            self.assertFalse(result['causal_truth_validated'])
            self.assertFalse(result['original_incident_reproduced'])
            self.assertEqual(result['model_calls'], 0)
            self.assertEqual(result['human_review_status'], 'pending')
            self.assertEqual(result['status'], 'passed', result['checks'])
            self.assertEqual(len(result['trials']), 3)
            self.assertGreaterEqual(result['wall_seconds'], 0)
            rows = result['public_rows']
            self.assertEqual([row['observation_id'] for row in rows[:3]],
                ['row_01', 'row_02', 'row_03'])
            self.assertEqual(result['reference']['key_observation_ids'],
                ['row_01', 'row_02', 'row_03'])
            self.assertEqual(set(row['category'] for row in rows),
                set(result['reference']['required_categories']))
            for row in rows:
                self.assertEqual(set(row), {'observation_id', 'category', 'message'})
                message = row['message']
                repeats = ([entry[0] for entry in message['measurements']]
                    if 'columns' in message else [entry['repeat'] for entry in message['measurements']])
                self.assertEqual(repeats, [1, 2, 3])
                self.assertLessEqual(len(json.dumps(message, ensure_ascii=False)), 1100)
            serialized_public = json.dumps(rows, ensure_ascii=False)
            for private_field in ('causal_condition', 'reference_explanation', 'acceptable_aliases',
                    'forbidden_assertions', 'causal_truth_validated', 'private_execution_trace'):
                self.assertNotIn(private_field, serialized_public)

    def test_invalid_repetition_arguments_do_not_execute(self):
        for function in (run_experiments, reproduce_http_content_length,
                reproduce_character_decoding, reproduce_stale_local_cache, reproduce_url_prefix_routing):
            for repeats in (0, 6, -1, 1.5, True, '3'):
                with self.subTest(function=function.__name__, repeats=repeats):
                    with self.assertRaises(ValueError):
                        function(repeats=repeats)


if __name__ == '__main__':
    unittest.main()
