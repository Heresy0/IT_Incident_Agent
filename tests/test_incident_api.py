"""Free M4 API/control acceptance. No model provider is contacted."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.auth import Principal
from backend.router.incident_router import router
from backend.router.research_router import router as runs
from backend.service import get_workflow_service
from backend.service.incident_provider import catalog, BoundIncidentProvider
from backend.service.incident_support import IncidentError
from backend.service.run_store import MemoryRunStore
from backend.service.workflow_service import WorkflowService, CapacityExceeded
from mult_agents.incident.contracts import IncidentsArgs
from tests.support import TestConfig, REQUEST


class IncidentAPITests(unittest.TestCase):
    def setUp(self):
        self.owner = Principal('team_a', 'alice')
        self.operator = Principal('team_a', 'alice', 'operator')
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / 'auth.json'
        identities = {'a': self.owner, 'op': self.operator, 'b': Principal('team_a', 'bob'), 'c': Principal('team_b', 'alice')}
        path.write_text(json.dumps({'principals': [{'tenant_id': p.tenant_id, 'user_id': p.user_id, 'role': p.role,
                         'token_sha256': hashlib.sha256(t.encode()).hexdigest()} for t, p in identities.items()]}))
        self.env = patch.dict(os.environ, {'RESEARCH_AUTH_FILE': str(path)})
        self.env.start()
        class Graph:
            def stream(self, *args, **kwargs):
                yield {'direct_answer': {'status': 'completed', 'final': 'research retained', 'intent': 'direct'}}
        self.store = MemoryRunStore()
        self.service = WorkflowService('unused', store=self.store, config=TestConfig(), workflow=Graph(), max_concurrency=1)
        app = FastAPI()
        app.include_router(router); app.include_router(runs)
        app.dependency_overrides[get_workflow_service] = lambda: self.service
        self.client = TestClient(app)
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.client.close(); self.service.close(); self.env.stop(); self.tmp.cleanup()

    def req(self, method, path, token='a', **kwargs):
        return self.client.request(method, '/api/v1/' + path, headers={'Authorization': 'Bearer ' + token}, **kwargs)

    def create(self, **changes):
        fields = {**catalog(self.owner)[0]['fields'], **changes}
        response = self.req('POST', 'incidents', json=fields)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def start(self, incident, key='start_a', **changes):
        response = self.req('POST', f'incidents/{incident["id"]}/diagnoses', json={'request_key': key, **changes})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['run_id']

    def result(self, run_id):
        return asyncio.run(self.service.wait_result(run_id, self.owner))

    def block(self):
        entered, release = Event(), Event()
        self.releases.append(release)
        adapter = self.service._adapters['incident']
        original = adapter.execute
        def execute(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test timed out')
            return original(*args)
        adapter.execute = execute
        return entered, release

    def confirmation(self, run_id, revision=1):
        return {'request_key': 'confirm_a', 'revision': revision, 'run_id': run_id,
                'confirmed_cause': 'dependency timeout manually checked', 'resolution': '人工核对下游恢复后完成验证',
                'tags': ['manual'], 'error_codes': ['UPSTREAM_TIMEOUT']}

    def test_catalog_scope_and_input_validation(self):
        response = self.req('GET', 'incident-demo-cases')
        self.assertEqual(len(response.json()['items']), 3)
        self.assertNotIn('gold', response.text)
        fields = catalog(self.owner)[0]['fields']
        for changes in ({'tenant_id': 'team_b'}, {'service': 'other'}, {'start': '2020-01-01'}, {'occurred_at': '2099-01-01T00:00:00Z'}):
            self.assertEqual(self.req('POST', 'incidents', json={**fields, **changes}).status_code, 422)
        self.assertEqual(self.client.get('/api/v1/incidents').status_code, 401)
        self.assertFalse(self.store.incidents)

    def test_owner_isolation_for_incidents_runs_and_events(self):
        incident = self.create(); run_id = self.start(incident); self.result(run_id)
        for token in ('b', 'c'):
            self.assertEqual(self.req('GET', 'incidents', token).json()['items'], [])
            for path in (f'incidents/{incident["id"]}', f'research/runs/{run_id}', f'research/runs/{run_id}/events'):
                self.assertEqual(self.req('GET', path, token).status_code, 404)
            self.assertEqual(self.req('POST', f'incidents/{incident["id"]}/diagnoses', token, json={'request_key': 'foreign'}).status_code, 404)

    def test_durable_id_counts_replay_and_terminal_idempotency(self):
        incident = self.create(); run_id = self.start(incident); result = self.result(run_id)
        self.assertEqual(result['run_id'], run_id)
        self.assertEqual(result['incident_id'], incident['id'])
        self.assertFalse(result['memory_enabled'])
        history = self.store.events(run_id)
        self.assertEqual([e['seq'] for e in history], list(range(1, len(history)+1)))
        self.assertEqual(sum(e['type'] == 'final' for e in history), 1)
        self.assertEqual(sum(e['type'] == 'call_start' and e.get('kind') == 'model' for e in history), result['run_summary']['model_calls'])
        self.assertEqual(self.start(incident), run_id)
        self.assertEqual(len(self.store.runs), 1)
        replay = self.req('GET', f'research/runs/{run_id}/events?after_seq={history[-2]["seq"]}')
        self.assertEqual(replay.text.count('"type": "final"'), 1)
        self.assertEqual(len(self.store.runs), 1)
        row = self.req('GET', f'incidents/{incident["id"]}').json()
        self.assertEqual(row['business_status'], 'awaiting_confirmation')

    def test_shared_capacity_same_key_retries_and_active_lock(self):
        entered, release = self.block(); incident = self.create(); run_id = self.start(incident)
        self.assertTrue(entered.wait(3))
        with ThreadPoolExecutor(4) as pool:
            ids = list(pool.map(lambda _: self.service.start_diagnosis(incident['id'],
                {'request_key': 'start_a', 'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}, self.owner)[0], range(8)))
        self.assertEqual(set(ids), {run_id})
        with self.assertRaises(CapacityExceeded):
            self.service.start_run(REQUEST)
        # New keys on an active incident report the business conflict before capacity.
        self.assertEqual(self.req('POST', f'incidents/{incident["id"]}/diagnoses', json={'request_key': 'another'}).status_code, 409)
        self.assertEqual(self.req('PATCH', f'incidents/{incident["id"]}', json={'revision': 1, 'changes': catalog(self.owner)[0]['fields']}).status_code, 409)
        self.assertEqual(self.req('POST', f'incidents/{incident["id"]}/diagnoses', json={'request_key': 'start_a', 'execution_mode': 'live', 'model_budget': 6, 'tool_budget': 4}).status_code, 409)
        release.set(); self.result(run_id)
        research = self.service.start_run(REQUEST)
        self.assertEqual(asyncio.run(self.service.wait_result(research))['final'], 'research retained')

    def test_revision_snapshot_and_stale_confirmation(self):
        incident = self.create(); run_id = self.start(incident); self.result(run_id)
        fields = {**catalog(self.owner)[0]['fields'], 'title': '修改标题'}
        path = f'incidents/{incident["id"]}'
        changed = self.req('PATCH', path, json={'revision': 1, 'changes': fields})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['revision'], 2)
        self.assertEqual(self.store.get(run_id)['config']['incident_snapshot']['title'], incident['title'])
        self.assertEqual(self.req('PATCH', path, json={'revision': 1, 'changes': fields}).status_code, 409)
        self.assertEqual(self.start(incident), run_id)
        self.assertEqual(self.req('POST', path+'/resolution', 'op', json=self.confirmation(run_id, 2)).status_code, 409)

    def test_manual_confirmation_and_owner_private_history(self):
        incident = self.create(); run_id = self.start(incident); self.result(run_id)
        self.assertFalse(self.store.confirmed_cases(self.owner))
        path = f'incidents/{incident["id"]}/resolution'; payload = self.confirmation(run_id)
        self.assertEqual(self.req('POST', path, json=payload).status_code, 403)
        self.assertEqual(self.req('POST', path, 'op', json=payload).status_code, 200)
        self.assertTrue(self.req('POST', path, 'op', json=payload).json()['replayed'])
        self.assertEqual(len(self.store.cases), 1)
        self.assertEqual(self.req('POST', path, 'op', json={**payload, 'resolution': 'different'}).status_code, 409)
        self.assertEqual(self.req('GET', f'incidents/{incident["id"]}').json()['business_status'], 'resolved')
        provider = BoundIncidentProvider(incident, self.owner, self.store)
        records, _ = provider.query('search_incidents', IncidentsArgs(symptoms='dependency timeout'), provider.ticket().scope)
        self.assertTrue(any(r['data_version'] == 'manual-1' for r in records))
        foreign = Principal('team_a', 'bob')
        provider = BoundIncidentProvider({**incident, 'user_id': 'bob'}, foreign, self.store)
        records, _ = provider.query('search_incidents', IncidentsArgs(symptoms='dependency timeout'), provider.ticket().scope)
        self.assertFalse(any(r['data_version'] == 'manual-1' for r in records))

    def test_free_ticket_reports_no_provider_without_model_calls(self):
        incident = self.create(demo_case_id=None, service='custom_service')
        result = self.result(self.start(incident))
        self.assertEqual(result['business_result'], 'needs_information')
        self.assertEqual(result['run_summary']['model_calls'], 0)
        self.assertFalse(result['evidence'])

    def test_live_requires_explicit_budgets_and_missing_key_is_safe(self):
        incident = self.create(); path = f'incidents/{incident["id"]}/diagnoses'
        with patch('mult_agents.incident.agents.build_roles', side_effect=AssertionError('paid call forbidden')):
            self.assertEqual(self.req('POST', path, json={'request_key': 'live', 'execution_mode': 'live'}).status_code, 422)
            run_id = self.start(incident, 'live', execution_mode='live', model_budget=6, tool_budget=4)
            self.assertEqual(self.result(run_id)['run_summary']['termination_reason'], 'AUTH_ERROR')
        self.assertEqual(self.req('GET', f'incidents/{incident["id"]}').json()['business_status'], 'open')

    def test_submission_failure_releases_capacity_and_terminalizes(self):
        incident = self.create()
        with patch.object(self.service._executor, 'submit', side_effect=RuntimeError('submit failed')):
            self.assertEqual(self.req('POST', f'incidents/{incident["id"]}/diagnoses', json={'request_key': 'fail'}).status_code, 503)
        failed = next(iter(self.store.runs.values()))
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(sum(e['type'] == 'final' for e in self.store.events(failed['run_id'])), 1)
        self.assertEqual(self.service.get_incident(incident['id'], self.owner)['business_status'], 'open')
        self.result(self.start(incident, 'retry'))

    def test_live_adapter_injected_free_model_uses_requested_global_budget(self):
        from mult_agents.incident.collaboration_fake import scripted_models
        self.service._base_config = TestConfig(api_key='injected-free')
        calls = []
        def factory(*args):
            calls.append(True)
            return scripted_models()
        self.service._adapters['incident'].model_factory = factory
        incident = self.create()
        run_id = self.start(incident, 'live_injected', execution_mode='live', model_budget=6, tool_budget=4)
        result = self.result(run_id)
        self.assertLessEqual(result['run_summary']['model_calls'], 6)
        self.assertLessEqual(result['run_summary']['tool_calls'], 4)
        self.assertEqual(result['execution_mode'], 'live')
        self.assertEqual(self.store.get(run_id)['config']['limits']['model_calls'], 6)
        self.assertEqual(self.start(incident, 'live_injected', execution_mode='live', model_budget=6, tool_budget=4), run_id)
        self.assertEqual(calls, [True])

    def test_storage_admission_failure_and_interrupted_incident_recovery(self):
        incident = self.create(); self.service.start()
        with patch.object(self.store, 'begin_diagnosis', side_effect=RuntimeError('db failed')):
            with self.assertRaises(RuntimeError):
                self.service.start_diagnosis(incident['id'], {'request_key': 'broken', 'execution_mode': 'scripted_control_only',
                    'model_budget': None, 'tool_budget': None}, self.owner)
        execution = {'request_key': 'interrupted', 'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}
        run_id, _, _ = self.store.begin_diagnosis(incident['id'], '00000000-0000-0000-0000-000000000001', execution, self.owner, {})
        self.assertEqual(self.store.fail_interrupted(), 1)
        result = self.store.get(run_id)['result']
        self.assertEqual(result['task_type'], 'incident')
        self.assertEqual(result['incident_id'], incident['id'])
        self.assertEqual(result['run_summary']['termination_reason'], 'PROCESS_INTERRUPTED')
        self.assertEqual(self.store.fail_interrupted(), 0)
        self.result(self.start(incident, 'retry'))
