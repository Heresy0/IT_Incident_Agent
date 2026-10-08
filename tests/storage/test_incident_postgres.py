"""M4 persistence acceptance; only an explicitly isolated *_test database."""
import asyncio
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from api.auth import Principal
from providers.bound import catalog, BoundIncidentProvider
from incidents.support import IncidentError
from storage.runs import PostgresRunStore
from workflow.service import WorkflowService
from evidence.contracts import IncidentsArgs
from tests.support import TestConfig


@unittest.skipUnless(os.getenv('INCIDENT_TEST_POSTGRES_DSN', os.getenv('RESEARCH_TEST_POSTGRES_DSN')), 'requires isolated *_test database')
class IncidentPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.getenv('INCIDENT_TEST_POSTGRES_DSN', os.getenv('RESEARCH_TEST_POSTGRES_DSN'))
        if not conninfo_to_dict(cls.dsn).get('dbname', '').endswith('_test'):
            raise RuntimeError('M4 tests require isolated *_test database')

    def setUp(self):
        self.owner = Principal('m4test_' + uuid4().hex, 'alice', 'operator')
        self.store = PostgresRunStore(self.dsn)
        self.execution = {'request_key': 'start_a', 'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}
        self.incident = self.store.create_incident(catalog(self.owner)[0]['fields'], self.owner)
        self.workflow_to_close = None

    def tearDown(self):
        # Never clear tables or another test/project's rows.
        with self.store.pool.connection() as conn:
            for table in ('confirmed_incident_cases', 'incidents', 'research_runs'):
                conn.execute(f'DELETE FROM {table} WHERE tenant_id=%s', (self.owner.tenant_id,))
        if self.workflow_to_close:
            self.workflow_to_close.close()
        else:
            self.store.close()

    def begin(self, execution=None, store=None):
        return (store or self.store).begin_diagnosis(self.incident['id'], str(uuid4()), execution or self.execution, self.owner, {})

    def test_repair_full_lifecycle_and_checkpoints_survive_new_connection(self):
        import time
        from providers.registry import ServiceBinding, ServiceRegistry
        from tests.workflow.test_remediation import OfflineExecutor
        fields = catalog(self.owner)[0]['fields']
        executor = OfflineExecutor()
        binding = ServiceBinding(self.owner.tenant_id, (self.owner.user_id,), fields['service'], fields['environment'],
            fields['service_version'], repairs={'api': {'executor': 'offline', 'actions': ['restart_service']}})
        service = WorkflowService('unused', store=self.store, config=TestConfig(),
            service_registry=ServiceRegistry([binding]), repair_executors={'offline': executor})
        self.workflow_to_close = service
        second = PostgresRunStore(self.dsn)
        try:
            run_id, _ = service.start_diagnosis(self.incident['id'], self.execution, self.owner)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                run = service.get_run(run_id, self.owner)
                if run['status'] != 'running':
                    break
                time.sleep(.03)
            self.assertEqual(run['result']['review_status'], 'passed')
            plan = service.repairs.propose(self.incident['id'], {'request_key': 'repair', 'revision': 1, 'run_id': run_id,
                'action': 'restart_service', 'target': 'api', 'parameters': {}, 'reason': 'Database control acceptance',
                'evidence_ids': [run['result']['evidence'][0]['evidence_id']]}, self.owner)
            service.repairs.approve(self.incident['id'], plan['id'], {'digest': plan['digest'], 'decision': 'approve'}, self.owner)
            result = service.repairs.execute(self.incident['id'], plan['id'], {'request_key': 'execute'}, self.owner)
            persisted = second.get_repairs(self.incident['id'], self.owner)[0]
            self.assertEqual(result, persisted)
            self.assertEqual(result['status'], 'verified')
            self.assertIn('action_started', [event['type'] for event in persisted['events']])
            self.assertEqual([event['seq'] for event in persisted['events']], list(range(1, len(persisted['events']) + 1)))
            service.repairs.execute(self.incident['id'], plan['id'], {'request_key': 'execute'}, self.owner)
            self.assertEqual(executor.writes, 1)
            # A process loss after the persisted action boundary retains the checkpoints.
            def interrupted(incident, plans):
                plans[0]['status'] = 'executing'
                return plans[0]
            self.store.repair_transaction(self.incident['id'], self.owner, interrupted)
            second.recover_repairs()
            recovered = second.get_repairs(self.incident['id'], self.owner)[0]
            self.assertEqual(recovered['status'], 'manual_required')
            self.assertEqual(recovered['events'][-1]['type'], 'interrupted')
            service.repairs.execute(self.incident['id'], plan['id'], {'request_key': 'execute'}, self.owner)
            self.assertEqual(executor.writes, 1)
        finally:
            second.close()

    def test_repair_storage_serializes_claims_and_blocks_business_edits(self):
        second = PostgresRunStore(self.dsn)
        iid = self.incident['id']
        try:
            def propose(incident, plans):
                if not plans:
                    plans.append({'id': 'plan', 'status': 'approved', 'events': []})
                return plans[0]
            self.store.repair_transaction(iid, self.owner, propose)
            def claim(store):
                def mutation(incident, plans):
                    if plans[0]['status'] == 'approved':
                        plans[0]['status'] = 'executing'
                        return True
                    return False
                return store.repair_transaction(iid, self.owner, mutation)
            with ThreadPoolExecutor(2) as pool:
                claims = list(pool.map(claim, (self.store, second)))
            self.assertEqual(sum(claims), 1)
            with self.assertRaisesRegex(IncidentError, 'REPAIR_IN_PROGRESS'):
                second.patch_incident(iid, catalog(self.owner)[0]['fields'], 1, self.owner)
            with self.assertRaises(IncidentError):
                second.repair_transaction(iid, Principal(self.owner.tenant_id, 'other'), propose)
            self.store.recover_repairs()
            plans = second.repair_transaction(iid, self.owner, lambda incident, plans: plans)
            self.assertEqual(plans[0]['status'], 'manual_required')
        finally:
            second.close()

    def test_two_stores_concurrent_idempotency_and_database_constraints(self):
        second = PostgresRunStore(self.dsn)
        try:
            with ThreadPoolExecutor(6) as pool:
                results = list(pool.map(lambda n: self.begin(store=self.store if n % 2 else second), range(12)))
            self.assertEqual(len({r[0] for r in results}), 1)
            self.assertEqual(sum(r[1] for r in results), 1)
            run_id, _, config = results[0]
            with self.assertRaises(IncidentError):
                self.begin({**self.execution, 'request_key': 'other'})
            # Check DB protection even if a caller bypasses the service/row-lock rules.
            for cfg in (config, {**config, 'request_key': 'other'}):
                with self.assertRaises(psycopg.errors.UniqueViolation):
                    with self.store.pool.connection() as conn:
                        conn.execute("INSERT INTO research_runs(run_id,checkpoint_thread_id,query,user_id,thread_id,tenant_id,status,config) VALUES (%s,%s,'test',%s,%s,%s,'running',%s)",
                            (str(uuid4()), str(uuid4()), self.owner.user_id, self.incident['id'], self.owner.tenant_id, Jsonb(cfg)))
            self.store.finish(run_id, {'status': 'partial', 'final': 'limited', 'run_id': run_id})
            self.assertEqual(self.begin()[0], run_id)
            self.assertFalse(self.begin()[1])
            self.assertTrue(self.begin({**self.execution, 'request_key': 'new'})[1])
        finally:
            second.close()

    def test_atomic_terminal_state_restart_replay_and_revision_snapshot(self):
        run_id, _, config = self.begin()
        self.assertEqual(self.store.get_incident(self.incident['id'], self.owner)['business_status'], 'investigating')
        self.assertTrue(self.store.finish(run_id, {'run_id': run_id, 'status': 'partial', 'final': 'limited'}))
        self.assertFalse(self.store.finish(run_id, {'run_id': run_id, 'status': 'completed'}))
        reopened = PostgresRunStore(self.dsn)
        try:
            row = reopened.get_incident(self.incident['id'], self.owner)
            self.assertEqual(row['business_status'], 'awaiting_confirmation')
            self.assertEqual([e['type'] for e in reopened.events(run_id)], ['status', 'final'])
            document = {**catalog(self.owner)[0]['fields'], 'title': '修订后'}
            self.store.patch_incident(self.incident['id'], document, 1, self.owner)
            self.assertEqual(reopened.get(run_id)['config']['incident_snapshot'], config['incident_snapshot'])
            self.assertEqual(reopened.get_incident(self.incident['id'], self.owner)['revision'], 2)
        finally:
            reopened.close()

    def test_owner_isolation_and_atomic_manual_case_retrieval(self):
        run_id, _, _ = self.begin(); self.store.finish(run_id, {'run_id': run_id, 'status': 'partial', 'final': 'limited'})
        for principal in (Principal(self.owner.tenant_id, 'bob', 'operator'), Principal('other', 'alice', 'operator')):
            self.assertIsNone(self.store.get_incident(self.incident['id'], principal))
            self.assertIsNone(self.store.get(run_id, principal))
            self.assertEqual(self.store.events(run_id, principal=principal), [])
            self.assertEqual(self.store.confirmed_cases(principal), [])
        payload = {'request_key': 'confirmed', 'run_id': run_id, 'revision': 1, 'confirmed_cause': 'dependency timeout manually verified',
            'resolution': '人工验证处理结果', 'tags': ['manual'], 'error_codes': []}
        with ThreadPoolExecutor(4) as pool:
            confirmations = list(pool.map(lambda _: self.store.confirm_incident(self.incident['id'], payload, self.owner), range(4)))
        self.assertEqual(sum(not c['replayed'] for c in confirmations), 1)
        self.assertEqual(len({c['case_id'] for c in confirmations}), 1)
        self.assertEqual(self.store.get_incident(self.incident['id'], self.owner)['business_status'], 'resolved')
        self.assertEqual(len(self.store.confirmed_cases(self.owner)), 1)
        provider = BoundIncidentProvider(self.incident, self.owner, self.store)
        rows, _ = provider.query('search_incidents', IncidentsArgs(symptoms='dependency timeout'), provider.ticket().scope)
        self.assertTrue(any(r['data_version'] == 'manual-1' for r in rows))

    def test_interrupted_incident_keeps_metadata_and_unlocks_business_state(self):
        run_id, _, config = self.begin()
        self.assertGreaterEqual(self.store.fail_interrupted(), 1)
        run = self.store.get(run_id)
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['result']['incident_snapshot'], config['incident_snapshot'])
        self.assertEqual(run['result']['run_summary']['termination_reason'], 'PROCESS_INTERRUPTED')
        self.assertEqual(self.store.get_incident(self.incident['id'], self.owner)['business_status'], 'open')
        self.assertEqual(sum(e['type'] == 'final' for e in self.store.events(run_id)), 1)

    def test_shared_service_persists_free_collaboration_and_keeps_incident_runs(self):
        service = WorkflowService('unused', store=self.store, config=TestConfig(postgres_dsn=self.dsn))
        try:
            run_id, replayed = service.start_diagnosis(self.incident['id'], self.execution, self.owner)
            self.assertFalse(replayed)
            result = asyncio.run(service.wait_result(run_id, self.owner))
            self.assertEqual(result['run_id'], run_id)
            self.assertEqual(result['task_type'], 'incident')
            self.assertGreater(result['run_summary']['tool_calls'], 0)
            self.assertEqual(service.start_diagnosis(self.incident['id'], self.execution, self.owner), (run_id, True))
            with self.store.pool.connection() as conn:
                conn.execute("UPDATE research_runs SET created_at=NOW()-INTERVAL '30 days' WHERE run_id=%s", (run_id,))
            self.store.cleanup()
            self.assertIsNotNone(self.store.get(run_id))
        finally:
            # The fixture owns the pool until teardown; service closes only its worker here.
            service._executor.shutdown(wait=True)
