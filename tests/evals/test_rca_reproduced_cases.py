from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from evals.rca_cases import validate_reachability
from evals.rca_contracts import DEFAULT_MANIFEST, load_dataset, read_json, sha_file
from evals.rca_reproduced_cases import (CASE_IDS, REPRODUCED_MANIFEST,
    build_reproduced_cases, write_dataset)


class RCAReproducedObservationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(REPRODUCED_MANIFEST)
        cls.folder = REPRODUCED_MANIFEST.parent
        cls.machine_manifest = cls.folder / 'machine/manifest.json'
        cls.executions = {case_id: read_json(cls.folder / 'machine' /
            ('experiment_' + case_id.rsplit('_', 1)[-1] + '.json')) for case_id in CASE_IDS}

    def test_machine_evidence_is_source_bound_and_never_human_gold(self):
        self.assertEqual(self.dataset['validation']['machine_experiment_cases'], 3)
        self.assertEqual(self.dataset['validation']['human_gold_cases'], 0)
        self.assertEqual(set(self.dataset['bound_artifacts']), {
            'machine/manifest.json', 'machine/experiment_001.json',
            'machine/experiment_019.json', 'machine/experiment_025.json',
            'machine/source/rca_experiments.py', 'machine/source/reproduce_rca.py'})
        for case in self.dataset['cases']:
            truth = case['truth']
            self.assertEqual(truth['origin'], 'executed_synthetic_experiment')
            self.assertEqual(truth['truth_status'], 'pending_human_review')
            self.assertFalse(truth['provenance']['human_verified'])
            self.assertFalse(truth['provenance']['is_gold'])
            self.assertEqual(truth['provenance']['reviewers'], [])
            self.assertEqual(truth['provenance']['reproduction_evidence'], [])
            machine = truth['machine_evidence']
            self.assertEqual(machine['status'], 'executed_not_human_adjudicated')
            self.assertFalse(machine['original_authored_world_confirmed'])
            for artifact in [machine['experiment_manifest'], *machine['artifacts']]:
                self.assertEqual(sha_file(self.folder / artifact['path']), artifact['sha256'])

    def test_pool_public_statistics_are_exact_projections_of_measured_trials(self):
        case = self.dataset['cases'][0]
        rows = {row['id']: json.loads(row['message']) for row in case['public']['world']['logs']}
        execution = self.executions[case['case_id']]
        self.assertEqual(rows['row_01']['parameters'], execution['parameters'])
        for row_id, phases in (('row_02', {'before', 'capacity_restored'}),
                               ('row_03', {'capacity_reduced'}),
                               ('row_04', {'smooth_negative_control'})):
            trials = [trial for trial in execution['trials'] if trial['phase'] in phases]
            self.assertEqual(len(rows[row_id]['trials']), len(trials))
            for projected, actual in zip(rows[row_id]['trials'], trials, strict=True):
                self.assertEqual(projected, {key: actual[key] for key in projected})
        self.assertTrue(all(t['distribution'] == 'burst' for t in rows['row_03']['trials']))
        self.assertTrue(all(t['distribution'] == 'smooth' for t in rows['row_04']['trials']))
        self.assertIn('no SQL', rows['row_01']['program'])

    def test_mutex_public_observations_match_actual_projection_without_holder_paths(self):
        case = self.dataset['cases'][1]
        actual = self.executions[case['case_id']]['trials'][0]['public_observation']
        for row in case['public']['world']['logs']:
            projected = json.loads(row['message'])
            self.assertEqual(projected, {key: actual[key] for key in projected})
        public_text = json.dumps(case['public'], ensure_ascii=False)
        for hidden in ('private_execution_trace', 'holder_kind', 'awaiting_io_signal',
                       'local_maintenance_work', 'local_work_iterations', 'released_at'):
            self.assertNotIn(hidden, public_text)

    def test_gateway_observation_keeps_distinct_private_failures_indistinguishable(self):
        case = self.dataset['cases'][2]
        trials = self.executions[case['case_id']]['trials']
        actual = trials[0]['public_observation']
        self.assertTrue(all(trial['public_observation'] == actual for trial in trials))
        self.assertEqual({trial['private_execution_trace']['observed_exception_type'] for trial in trials},
                         {'RemoteDisconnected', 'BadStatusLine'})
        for row in case['public']['world']['logs']:
            projected = json.loads(row['message'])
            self.assertEqual(projected, {key: actual[key] for key in projected})
        public_text = json.dumps(case['public'], ensure_ascii=False)
        for hidden in ('RemoteDisconnected', 'BadStatusLine', 'failure_kind',
                       'upstream_malformed_status_line', 'private_execution_trace'):
            self.assertNotIn(hidden, public_text)

    def test_snapshots_use_capture_time_and_disclose_native_scope_label(self):
        manifest = read_json(self.machine_manifest)
        for case in self.dataset['cases']:
            world = case['public']['world']
            self.assertIn('not a staging deployment', world['observation_origin'])
            self.assertEqual(world['ticket']['scope']['start'], manifest['started_at'])
            for row in world['logs']:
                self.assertEqual(row['timestamp'], manifest['completed_at'])
                self.assertEqual(row['timestamp_basis'],
                    'experiment_manifest_capture_completion_not_request_event_time')

    def test_observations_change_version_without_backdating_old_case_confirmation(self):
        old = load_dataset(DEFAULT_MANIFEST)
        old_by_id = {case['case_id']: case for case in old['cases']}
        boundary = self.dataset['manifest']['version_boundary']
        self.assertFalse(boundary['original_cases_modified'])
        self.assertFalse(boundary['original_worlds_confirmed'])
        self.assertIn('not be pooled', boundary['comparison_limit'])
        for case in self.dataset['cases']:
            self.assertNotEqual(case['public_sha256'], old_by_id[case['case_id']]['public_sha256'])

    def test_real_tool_queries_acquire_all_declared_fields_without_truncation(self):
        result = validate_reachability(self.dataset)
        self.assertEqual(len(result['cases']), 3)
        self.assertEqual(result['model_calls'], 0)
        self.assertFalse(result['causal_truth_validated'])
        self.assertTrue(all(not case['content_truncated_sources'] for case in result['cases']))
        self.assertEqual(result['cases'][2]['expected_source_errors'], ['SOURCE_NOT_CONFIGURED'])

    def test_copied_execution_is_self_contained_and_deterministically_materialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / 'version'
            result = write_dataset(self.machine_manifest, copied)
            self.assertEqual(result['fingerprint'], self.dataset['fingerprint'])
            self.assertEqual(result['bound_artifacts'], self.dataset['bound_artifacts'])
            with self.assertRaises(FileExistsError):
                write_dataset(self.machine_manifest, copied)

    def test_machine_attachment_hash_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / 'version'
            shutil.copytree(self.folder, copied)
            raw = copied / 'machine/experiment_019.json'
            raw.write_bytes(raw.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'Machine artifact hash mismatch'):
                load_dataset(copied / 'manifest.json')
            with self.assertRaisesRegex(ValueError, 'Experiment evidence hash mismatch'):
                build_reproduced_cases(copied / 'machine/manifest.json')

    def test_worker_allowlist_excludes_all_private_machine_evidence(self):
        from scripts.evaluate_rca import dataset_freeze, worker_case
        freeze = dataset_freeze(self.dataset)
        self.assertTrue(set(self.dataset['bound_artifacts']) <= set(freeze['files_sha256']))
        for case in self.dataset['cases']:
            task = worker_case(case, 'sufficient_evidence')
            self.assertEqual(set(task), {'world', 'capability_metadata', 'initial_checks'})
            text = json.dumps(task, ensure_ascii=False)
            for hidden in ('machine_evidence', 'causal_conditions', 'expected_relation',
                           'private_execution_trace', 'RemoteDisconnected', 'holder_kind'):
                self.assertNotIn(hidden, text)


if __name__ == '__main__':
    unittest.main()
