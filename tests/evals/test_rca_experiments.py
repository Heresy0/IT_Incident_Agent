import json
from pathlib import Path
import tempfile
import unittest

from evals.rca_experiments import (canonical_sha, reproduce_gateway,
    reproduce_mutex, reproduce_pool, run_experiments, sha_file)


class RCAExperimentsTests(unittest.TestCase):
    def test_pool_actual_capacity_intervention_and_smooth_control(self):
        result = reproduce_pool(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        smooth, before, reduced, restored = result['trials']
        self.assertEqual([item['capacity'] for item in result['trials']], [4, 40, 4, 40])
        self.assertEqual(smooth['acquisition_timeouts'], 0)
        self.assertEqual(before['acquisition_timeouts'], 0)
        self.assertGreater(reduced['acquisition_timeouts'], 0)
        self.assertEqual(restored['acquisition_timeouts'], 0)
        self.assertEqual(len(reduced['requests']), 30)
        self.assertTrue(any(not item['acquired'] and item['wait_ms'] >= 80
            for item in reduced['requests']))
        self.assertTrue(all(not item['human_verified'] and not item['is_gold']
            for item in [result]))
        self.assertFalse(result['causal_truth_validated'])

    def test_mutex_real_timeout_does_not_reveal_the_holder_path(self):
        result = reproduce_mutex(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        first, second = result['trials']
        self.assertEqual(first['public_observation'], second['public_observation'])
        self.assertNotEqual(first['holder_kind'], second['holder_kind'])
        self.assertTrue(all(item['private_execution_trace']['wait_ms'] >= 60
            for item in result['trials']))
        self.assertGreater(second['private_execution_trace']['owner']['local_work_iterations'], 0)
        self.assertFalse(result['human_verified'])

    def test_gateway_distinct_actual_failures_have_equal_visible502(self):
        result = reproduce_gateway(repeats=1)
        self.assertEqual(result['status'], 'passed', result['checks'])
        first, second = result['trials']
        self.assertEqual(first['public_observation'], second['public_observation'])
        self.assertEqual(first['public_observation']['http_status'], 502)
        self.assertEqual(first['private_execution_trace']['observed_exception_type'], 'RemoteDisconnected')
        self.assertEqual(second['private_execution_trace']['observed_exception_type'], 'BadStatusLine')
        self.assertEqual(first['gateway_response_bytes_hex'], second['gateway_response_bytes_hex'])
        self.assertNotIn('failure_kind', first['public_observation'])

    def test_artifacts_are_bound_and_no_human_gold_is_generated(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'run'
            manifest = run_experiments(folder, repeats=1, cases=('019', '025'))
            self.assertEqual(manifest['status'], 'passed')
            self.assertEqual(manifest['model_calls'], 0)
            self.assertFalse(manifest['original_incident_reproduced'])
            self.assertFalse(manifest['human_verified'])
            self.assertFalse(manifest['is_gold'])
            self.assertTrue(manifest['source_unchanged_during_run'])
            for item in manifest['cases']:
                path = folder / item['path']
                self.assertEqual(item['sha256'], sha_file(path))
                result = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(result['human_review_status'], 'pending')
                self.assertEqual(result['source_sha256'], manifest['source_sha256'])
            self.assertEqual(json.loads((folder / 'manifest.json').read_text(encoding='utf-8')), manifest)
            with self.assertRaises(FileExistsError):
                run_experiments(folder, repeats=1, cases=('025',))

    def test_bounded_arguments_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            for repeats, cases in ((0, ('001',)), (6, ('001',)), (1, ()),
                    (1, ('001', '001')), (1, ('999',))):
                folder = Path(temporary) / 'invalid'
                with self.assertRaises(ValueError):
                    run_experiments(folder, repeats=repeats, cases=cases)
                self.assertFalse(folder.exists())

    def test_canonical_hash_is_order_insensitive(self):
        self.assertEqual(canonical_sha({'x': 1, 'y': 2}), canonical_sha({'y': 2, 'x': 1}))


if __name__ == '__main__':
    unittest.main()
