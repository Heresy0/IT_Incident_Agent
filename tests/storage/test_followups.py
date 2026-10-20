"""Real PostgreSQL transactions and interrupted followup-stage recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4

from incidents.support import IncidentError
from providers.bound import catalog
from storage.runs import PostgresRunStore
from storage.recovery import RecoveryInterrupted
from tests.storage import test_v2_postgres as controls
from tests.support import TestConfig
from workflow.service import WorkflowService


@unittest.skipUnless(os.getenv('INCIDENT_TEST_POSTGRES_DSN'), 'requires isolated *_test PostgreSQL')
class FollowupPostgresTests(unittest.TestCase):
    setUpClass = controls.V2PostgresTests.__dict__['setUpClass']
    setUp = controls.V2PostgresTests.setUp
    tearDown = controls.V2PostgresTests.tearDown
    begin = controls.V2PostgresTests.begin

    def parent(self):
        rid, _, _ = self.begin()
        self.store.finish(rid, {'run_id': rid, 'status': 'partial', 'final': 'original immutable report',
            'run_summary': {'model_calls': 2, 'tool_calls': 1, 'token_usage': {'status': 'partial_or_unknown'}}, 'drafts': []})
        return rid

    def followup_execution(self, parent, key='followup'):
        return {**self.execution, 'request_key': key,
            'followup': {'parent_run_id': parent, 'revision': 1, 'note': 'operator followup', 'observations': []}}

    def test_concurrent_followup_start_is_one_new_run_and_one_revision(self):
        parent = self.parent(); execution = self.followup_execution(parent)
        second = PostgresRunStore(self.dsn)
        try:
            def begin(index):
                return (self.store if index % 2 else second).begin_diagnosis(
                    self.incident['id'], str(uuid4()), execution, self.owner, {})
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(begin, range(12)))
            self.assertEqual(len({r[0] for r in results}), 1)
            self.assertEqual(sum(r[1] for r in results), 1)
            self.assertEqual(second.get_incident(self.incident['id'], self.owner)['revision'], 2)
            self.assertEqual(second.get(parent)['result']['final'], 'original immutable report')
        finally:
            second.close()

    def test_failed_insert_rolls_back_revision_and_followup_snapshot(self):
        parent = self.parent(); before = self.store.get_incident(self.incident['id'], self.owner)
        import psycopg
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.store.begin_diagnosis(self.incident['id'], parent, self.followup_execution(parent), self.owner, {})
        self.assertEqual(self.store.get_incident(self.incident['id'], self.owner), before)

    def test_history_counts_all_delivered_rounds_and_rejects_stale_parent(self):
        parent = self.parent(); execution = self.followup_execution(parent)
        rid, _, _ = self.store.begin_diagnosis(self.incident['id'], str(uuid4()), execution, self.owner, {})
        self.store.finish(rid, {'run_id': rid, 'status': 'partial', 'final': 'new report', 'run_summary': {
            'model_calls': 3, 'tool_calls': 2, 'token_usage': {'status': 'known', 'input_tokens': 10, 'output_tokens': 4}}})
        history = self.store.list_diagnoses(self.incident['id'], self.owner, 0, 1)
        self.assertEqual(len(history['items']), 1)
        self.assertEqual(history['totals']['runs'], 2)
        self.assertEqual(history['totals']['model_calls'], 5)
        self.assertEqual(history['totals']['unmeasured_runs'], 1)
        self.assertEqual(history['items'][0]['parent_run_id'], parent)
        with self.assertRaises(IncidentError):
            self.store.begin_diagnosis(self.incident['id'], str(uuid4()), {**execution, 'request_key': 'stale'}, self.owner, {})

    def test_recovery_reuses_frozen_user_sources_without_granting_another_round(self):
        parent = self.parent()
        payload = self.followup_execution(parent)
        payload['followup']['observations'] = [{'source_name': 'manual test log', 'service': self.incident['service'],
            'observed_at': self.incident['end'], 'content': 'operator observation; unverified'}]
        service = WorkflowService('unused', store=self.store, config=TestConfig(), max_concurrency=1,
            checkpoint_recovery=True)
        self.workflow_to_close = service
        injected = []
        def cut(point, journal):
            if point == 'after_response_saved' and journal.stage_name == 'collect' and not injected:
                injected.append(True)
                raise RecoveryInterrupted('synthetic transient after committed response')
        self.store._recovery_fault = cut
        rid, _ = service.start_followup(self.incident['id'], payload, self.owner)
        result = asyncio.run(asyncio.wait_for(service.wait_result(rid), timeout=25))
        self.assertTrue(injected)
        self.assertEqual(result['followup']['parent_run_id'], parent)
        self.assertEqual(len(result['followup']['submissions']), 1)
        manual = [row for row in result['evidence'] if row['payload'].get('source_type') == 'user_submission']
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]['payload']['message'], 'operator observation; unverified')
        self.assertLessEqual(result['rework_rounds'], 1)
        self.assertEqual(result['recovery']['recoveries'], 1)
        self.assertTrue(result['final'])
        with self.store.pool.connection() as conn:
            row = conn.execute('SELECT counts FROM run_execution WHERE run_id=%s', (rid,)).fetchone()
            count = conn.execute('SELECT COUNT(*) AS total FROM run_operations WHERE run_id=%s', (rid,)).fetchone()['total']
        self.assertEqual(count, row['counts']['model_calls'] + row['counts']['tool_calls'])
        self.assertEqual(self.store.get_incident(self.incident['id'], self.owner)['revision'], 2)
