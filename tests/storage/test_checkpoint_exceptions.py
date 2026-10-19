"""Real PostgreSQL disconnections and typed-failure replay, with matched controls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests.storage import test_checkpoint_recovery as controls

ROOT = Path(__file__).resolve().parents[2]


def canonical(value, identities):
    if isinstance(value, dict):
        return {key: canonical(item, identities) for key, item in value.items() if key not in {
            'span_id', 'parent_span_id', 'retrieved_at', 'duration_ms', 'source_version',
            'source_version_full', 'recoveries', 'created_at', 'updated_at'}}
    if isinstance(value, list):
        return [canonical(item, identities) for item in value]
    if isinstance(value, str):
        for identity in identities:
            value = value.replace(identity, 'paired-identity')
    return value


@unittest.skipUnless(controls.DSN, 'requires isolated *_test PostgreSQL')
class CheckpointExceptionTests(unittest.TestCase):
    setUp = controls.CheckpointRecoveryTests.setUp
    tearDown = controls.CheckpointRecoveryTests.tearDown
    begin = controls.CheckpointRecoveryTests.begin
    attempts = controls.CheckpointRecoveryTests.attempts
    assert_delivery = controls.CheckpointRecoveryTests.assert_delivery

    def worker(self, run_id, point='none', stage='*', ordinal=0, *, scenario=''):
        env = {**os.environ, 'INCIDENT_TEST_POSTGRES_DSN': controls.DSN,
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/checkpoint_worker.py'),
            run_id, str(self.audit), point, stage, str(ordinal), 'fixed-clock', 'scenario=' + scenario],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.worker_index += 1
        if os.getenv('CHECKPOINT_ACCEPTANCE_DIR'):
            (Path(self.temp.name) / f'{run_id}-worker-{self.worker_index}.json').write_text(
                json.dumps({'run_id': run_id, 'fault': point, 'stage': stage, 'ordinal': ordinal,
                    'scenario': scenario, 'returncode': result.returncode,
                    'stdout': result.stdout, 'stderr': result.stderr}, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertIn(result.returncode, (0, 73, 74), result.stderr)
        return result

    def normalized(self, run):
        events = copy.deepcopy(run['result'])
        return canonical({'result': events, 'requests': self.attempts()},
                         [run['run_id'], run['config']['incident_id']])

    def control(self, scenario=''):
        rid = self.begin()
        self.worker(rid, scenario=scenario)
        run, execution, operations = self.assert_delivery(rid)
        self.assertEqual(len(self.attempts()), len(operations))
        return self.normalized(run)

    def assert_pair(self, expected, run_id):
        run, execution, operations = self.assert_delivery(run_id)
        self.assertEqual(self.normalized(run), expected)
        self.assertEqual(len(self.attempts()), len(operations))
        self.assertNotIn('RECOVERY_STATE_CONFLICT', run['result']['run_summary']['termination_reason'])
        self.assertEqual(sum(row['status'] == 'unknown' for row in operations), 0)
        return run, execution, operations

    def test_model_response_survives_transient_postgres_receipt_read_failure(self):
        expected = self.control()
        for stage, ordinal in (('collect', 1), ('review', 1), ('rework', 4), ('rework', 5)):
            with self.subTest(stage=stage, ordinal=ordinal):
                rid = self.begin()
                self.assertEqual(self.worker(rid, 'pg_read_disconnect', stage, ordinal).returncode, 74)
                self.assertEqual(self.store.get(rid)['status'], 'running')
                self.worker(rid)
                self.assert_pair(expected, rid)

    def test_tool_response_survives_transient_postgres_receipt_read_failure(self):
        expected = self.control()
        for stage, ordinal in (('collect', 2), ('rework', 2)):
            with self.subTest(stage=stage, ordinal=ordinal):
                rid = self.begin()
                self.assertEqual(self.worker(rid, 'pg_read_disconnect', stage, ordinal).returncode, 74)
                self.worker(rid)
                self.assert_pair(expected, rid)

    def test_post_commit_usage_and_end_events_resume_without_cursor_conflict(self):
        expected = self.control()
        for point in ('pg_usage_disconnect', 'pg_end_disconnect'):
            for stage, ordinal in (('collect', 1), ('rework', 5)):
                with self.subTest(point=point, stage=stage):
                    rid = self.begin()
                    self.assertEqual(self.worker(rid, point, stage, ordinal).returncode, 74)
                    self.worker(rid)
                    self.assert_pair(expected, rid)

    def test_failed_receipts_preserve_uninterrupted_error_and_retry_semantics(self):
        for scenario, stage, ordinal in (('raw_provider', 'collect', 2),
                ('execution_timeout', 'collect', 2), ('provider_timeout', 'collect', 2),
                ('raw_model', 'review', 1), ('model_auth', 'review', 1)):
            with self.subTest(scenario=scenario):
                expected = self.control(scenario)
                rid = self.begin()
                self.assertEqual(self.worker(rid, 'after_failure_saved', stage, ordinal, scenario=scenario).returncode, 73)
                self.worker(rid, scenario=scenario)
                self.assert_pair(expected, rid)
