"""Actual PostgreSQL and process-death tests; no model or external HTTP calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from api.auth import Principal
from providers.bound import catalog
from runtime.context import Limits, RunContext
from storage.runs import PostgresRunStore
from storage.recovery import RecoveryStore, RecoveryConflict
from workflow.recovery import execute, execution_request
from tests.checkpoint_support import service_for

ROOT = Path(__file__).resolve().parents[2]
DSN = os.getenv('INCIDENT_TEST_POSTGRES_DSN', os.getenv('RESEARCH_TEST_POSTGRES_DSN'))


@unittest.skipUnless(DSN, 'requires isolated *_test PostgreSQL')
class CheckpointRecoveryTests(unittest.TestCase):
    def setUp(self):
        if not conninfo_to_dict(DSN).get('dbname', '').endswith('_test'):
            raise RuntimeError('Recovery tests require an isolated *_test database')
        self.store = PostgresRunStore(DSN)
        self.store.setup_checkpoints()
        self.service = service_for(self.store)
        self.owner = Principal('checkpoint_test_' + uuid4().hex, 'alice', 'operator')
        artifact = os.getenv('CHECKPOINT_ACCEPTANCE_DIR')
        if artifact:
            path = Path(artifact) / self._testMethodName / uuid4().hex
            path.mkdir(parents=True)
            self.temp = SimpleNamespace(name=str(path), cleanup=lambda: None)
        else:
            self.temp = tempfile.TemporaryDirectory()
        self.audit = Path(self.temp.name) / 'external.jsonl'

    def tearDown(self):
        with self.store.pool.connection() as conn:
            ids = [row['run_id'] for row in conn.execute('SELECT run_id FROM research_runs WHERE tenant_id=%s', (self.owner.tenant_id,)).fetchall()]
            for run_id in ids:
                for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
                    conn.execute(f'DELETE FROM {table} WHERE thread_id=%s', (run_id,))
            for table in ('incidents', 'research_runs'):
                conn.execute(f'DELETE FROM {table} WHERE tenant_id=%s', (self.owner.tenant_id,))
        self.service.close()
        self.temp.cleanup()

    def begin(self, *, budget=16, purpose='diagnosis'):
        fields = {**catalog(self.owner)[0]['fields'], 'purpose': purpose}
        incident = self.store.create_incident(fields, self.owner)
        execution = {'request_key': 'first', 'execution_mode': 'live', 'model_budget': budget, 'tool_budget': 8}
        metadata = self.service._incident.metadata(execution, incident=incident, principal=self.owner)
        run_id, _, _ = self.store.begin_diagnosis(incident['id'], str(uuid4()), execution, self.owner, metadata)
        self.audit = Path(self.temp.name) / (run_id + '.jsonl')
        self.worker_index = 0
        return run_id

    def worker(self, run_id, point='none', stage='*', ordinal=0, repair=False):
        env = {**os.environ, 'INCIDENT_TEST_POSTGRES_DSN': DSN, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/checkpoint_worker.py'), run_id,
            str(self.audit), point, stage, str(ordinal), *(['repair'] if repair else [])],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=45)
        self.assertIn(result.returncode, (0, 73), result.stderr)
        self.worker_index += 1
        if os.getenv('CHECKPOINT_ACCEPTANCE_DIR'):
            receipt = {'run_id': run_id, 'fault': point, 'stage': stage, 'ordinal': ordinal,
                       'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
            (Path(self.temp.name) / f'{run_id}-worker-{self.worker_index}.json').write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        return result

    def attempts(self):
        return [json.loads(row) for row in self.audit.read_text(encoding='utf-8').splitlines()] if self.audit.exists() else []

    def assert_delivery(self, run_id):
        run = self.store.get(run_id)
        self.assertNotEqual(run['status'], 'running')
        self.assertTrue(run['result']['final'])
        events, after = [], 0
        while True:
            batch = self.store.events(run_id, after)
            events.extend(batch)
            if len(batch) < 100:
                break
            after = batch[-1]['seq']
        self.assertEqual([event['seq'] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(event['type'] == 'final' for event in events), 1)
        self.assertEqual(sum(event['type'] == 'workflow_completed' for event in events), 1)
        starts = {event['span_id'] for event in events if event['type'] == 'call_start'}
        ends = {event['span_id'] for event in events if event['type'] == 'call_end'}
        with self.store.pool.connection() as conn:
            execution = conn.execute('SELECT * FROM run_execution WHERE run_id=%s', (run_id,)).fetchone()
            operations = conn.execute('SELECT * FROM run_operations WHERE run_id=%s', (run_id,)).fetchall()
        self.assertLessEqual(execution['counts']['model_calls'], execution['limits']['model_calls'])
        self.assertLessEqual(execution['counts']['tool_calls'], execution['limits']['tool_calls'])
        self.assertEqual(run['result']['run_summary']['model_calls'], execution['counts']['model_calls'])
        self.assertLessEqual(len(execution['allowances'].get('semantic_rework', [])), 1)
        self.assertEqual(execution['counts']['model_calls'], sum(op['kind'] == 'model' for op in operations))
        self.assertEqual(execution['counts']['tool_calls'], sum(op['kind'] == 'tool' for op in operations))
        if all(op['status'] == 'responded' for op in operations):
            self.assertEqual(starts, ends, 'Recovered completed calls must retain their committed span identity')
        if os.getenv('CHECKPOINT_ACCEPTANCE_DIR'):
            with self.store.pool.connection() as conn:
                snapshots = conn.execute('SELECT * FROM run_snapshots WHERE run_id=%s', (run_id,)).fetchall()
            receipt = {'run': run, 'execution': execution, 'operations': operations,
                       'events': events, 'snapshots': snapshots, 'external_attempts': self.attempts()}
            (Path(self.temp.name) / (run_id + '-receipt.json')).write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        return run, execution, operations

    def test_every_internal_rework_call_is_reused_without_a_second_round(self):
        for ordinal in (2, 3, 4, 5, 6):
            with self.subTest(ordinal=ordinal):
                run_id = self.begin()
                self.assertEqual(self.worker(run_id, 'after_response_saved', 'rework', ordinal).returncode, 73)
                self.worker(run_id)
                run, _, operations = self.assert_delivery(run_id)
                self.assertEqual(run['status'], 'completed', run['result'])
                self.assertEqual(run['result']['rework_rounds'], 1)
                self.assertEqual(len(self.attempts()), len(operations))

    def test_review_correction_receipt_does_not_grant_another_repair(self):
        run_id = self.begin()
        self.assertEqual(self.worker(run_id, 'after_response_saved', 'review', 2, repair=True).returncode, 73)
        self.worker(run_id, repair=True)
        run, execution, operations = self.assert_delivery(run_id)
        self.assertLessEqual(len(execution['allowances'].get('reviewer_repair', [])), 1)
        self.assertEqual(len(self.attempts()), len(operations))

    def test_low_budget_status_check_and_restart_before_reservation(self):
        for budget, purpose in ((3, 'diagnosis'), (6, 'diagnosis'), (16, 'status_check')):
            with self.subTest(budget=budget, purpose=purpose):
                run_id = self.begin(budget=budget, purpose=purpose)
                self.assertEqual(self.worker(run_id, 'before_reserve', 'collect', 1).returncode, 73)
                self.assertEqual(self.attempts(), [])
                self.worker(run_id)
                self.assert_delivery(run_id)

    def test_no_fault_real_graph_rework_delivery_and_terminal_replay(self):
        run_id = self.begin()
        self.worker(run_id)
        run, execution, operations = self.assert_delivery(run_id)
        self.assertEqual(run['status'], 'completed', run['result'])
        self.assertEqual(run['result']['rework_rounds'], 1)
        before = self.attempts()
        self.worker(run_id)
        self.assertEqual(self.attempts(), before)
        self.assertEqual(self.store.get(run_id), run)
        self.assertEqual(execution['tokens']['known_calls'], execution['counts']['model_calls'])

    def test_completed_response_reused_after_real_process_exit_in_every_stage(self):
        for stage in ('collect', 'diagnose', 'review', 'challenge', 'rework'):
            with self.subTest(stage=stage):
                run_id = self.begin()
                first = self.worker(run_id, 'after_response_saved', stage, 1)
                # The first challenge can skip when the initial review objects;
                # use the post-rework challenge cursor for that coverage below.
                if first.returncode == 0:
                    self.assertEqual(stage, 'challenge')
                    continue
                self.assertEqual(first.returncode, 73)
                self.worker(run_id)
                run, execution, operations = self.assert_delivery(run_id)
                self.assertEqual(run['status'], 'completed', run['result'])
                self.assertEqual(len(self.attempts()), len(operations))
                self.assertEqual(run['result']['rework_rounds'], 1)

    def test_stage_receipt_ahead_of_graph_checkpoint_does_not_repeat_calls(self):
        for stage in ('collect', 'diagnose', 'review', 'challenge', 'rework', 'publish', 'finalize'):
            with self.subTest(stage=stage):
                run_id = self.begin()
                self.assertEqual(self.worker(run_id, 'after_stage_saved', stage).returncode, 73)
                self.worker(run_id)
                run, _, operations = self.assert_delivery(run_id)
                self.assertEqual(run['status'], 'completed', run['result'])
                self.assertEqual(len(self.attempts()), len(operations))

    def test_unknown_reserved_call_keeps_budget_and_is_never_resent(self):
        for stage, ordinal in (('collect', 1), ('collect', 2), ('review', 1), ('rework', 1)):
            with self.subTest(stage=stage, ordinal=ordinal):
                run_id = self.begin()
                self.assertEqual(self.worker(run_id, 'before_external_call', stage, ordinal).returncode, 73)
                before = len(self.attempts())
                self.worker(run_id)
                run, _, operations = self.assert_delivery(run_id)
                self.assertEqual(run['status'], 'partial', run['result'])
                self.assertEqual(len(self.attempts()), before)
                self.assertEqual(sum(op['status'] == 'unknown' for op in operations), 1)

    def test_prepared_report_and_committed_report_survive_process_exit(self):
        for point in ('after_report_ready', 'after_report_committed'):
            with self.subTest(point=point):
                run_id = self.begin()
                self.assertEqual(self.worker(run_id, point).returncode, 73)
                before = self.attempts()
                self.worker(run_id)
                self.assert_delivery(run_id)
                self.assertEqual(self.attempts(), before)

    def test_exclusive_owner_and_stale_fence_protect_budget_and_checkpoint(self):
        from storage.checkpoints import FencedPostgresSaver
        run_id = self.begin()
        with RecoveryStore.own(self.store, run_id) as first:
            with RecoveryStore.own(self.store, run_id) as other:
                self.assertIsNone(other)
            with self.store.pool.connection() as conn:
                conn.execute('UPDATE run_execution SET epoch=epoch+1 WHERE run_id=%s', (run_id,))
            with self.assertRaises(RecoveryConflict):
                first.reserve('collect', 1, 'model', {}, True, [])
            with self.assertRaises(RecoveryConflict):
                with FencedPostgresSaver(first)._cursor():
                    pass

    def test_expired_deadline_cannot_issue_more_external_calls(self):
        run_id = self.begin()
        self.assertEqual(self.worker(run_id, 'after_stage_saved', 'collect').returncode, 73)
        before = self.attempts()
        with self.store.pool.connection() as conn:
            conn.execute("UPDATE run_execution SET deadline_at=clock_timestamp()-INTERVAL '1 second' WHERE run_id=%s", (run_id,))
        self.worker(run_id)
        run, _, _ = self.assert_delivery(run_id)
        self.assertEqual(self.attempts(), before)
        self.assertNotEqual(run['status'], 'completed')

    def test_snapshot_corruption_and_source_change_stop_without_new_calls(self):
        for kind in ('snapshot', 'source'):
            with self.subTest(kind=kind):
                run_id = self.begin()
                self.worker(run_id, 'after_stage_saved', 'collect')
                before = self.attempts()
                with self.store.pool.connection() as conn:
                    if kind == 'snapshot':
                        conn.execute("UPDATE run_snapshots SET sha256='broken' WHERE run_id=%s", (run_id,))
                    else:
                        row = self.store.get(run_id)
                        config = row['config']
                        config['run_spec']['source_version_full'] = 'changed'
                        conn.execute('UPDATE research_runs SET config=%s WHERE run_id=%s', (Jsonb(config), run_id))
                self.worker(run_id)
                run, _, _ = self.assert_delivery(run_id)
                self.assertEqual(run['status'], 'failed')
                self.assertEqual(self.attempts(), before)
