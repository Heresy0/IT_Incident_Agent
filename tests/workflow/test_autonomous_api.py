"""Free tests of the real admission, worker, HTTP and repair-source boundaries."""
import asyncio
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import Principal, get_current_principal
from api.incidents import router as incidents_router
from api.repairs import router as repairs_router
from api.runs import router as runs_router
from config.incident import IncidentConfig
from incidents.support import IncidentError
from providers.bound import catalog
from providers.fixtures import FixtureProvider
from providers.registry import ServiceBinding, ServiceRegistry
from storage.runs import MemoryRunStore
from workflow.dependencies import get_workflow_service
from workflow.service import WorkflowService
from workflow.v2.scripted import scripted_model


class AutonomousAPITests(unittest.TestCase):
    def setUp(self):
        self.owner = Principal('api_v2', 'alice')
        self.principal = self.owner
        self.store = MemoryRunStore()
        self.config = IncidentConfig(api_key='private-model-key-for-control', model='control-model',
            base_url='https://private-model-endpoint.invalid/v1',
            extra_body={'test_setting': 'frozen', 'private_header': 'never-persist-this'})
        self.factory_calls = []

        def factory(key, name):
            self.factory_calls.append((key, name))
            return scripted_model(FixtureProvider('case_001', self.owner))

        self.service = WorkflowService('unused', config=self.config, store=self.store,
            incident_engine='autonomous_v2', incident_model_factory=factory, max_concurrency=1)
        app = FastAPI()
        for router in (incidents_router, runs_router, repairs_router):
            app.include_router(router)
        app.dependency_overrides[get_workflow_service] = lambda: self.service
        app.dependency_overrides[get_current_principal] = lambda: self.principal
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.service.close()

    def create(self):
        fields = next(row['fields'] for row in catalog(self.owner) if row['case_id'] == 'case_001')
        response = self.client.post('/api/v1/incidents', json=fields)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def start(self, incident, key='start-v2', live=True):
        payload = {'request_key': key}
        if live:
            payload.update(execution_mode='live', model_budget=8, tool_budget=4)
        response = self.client.post(f'/api/v1/incidents/{incident["id"]}/diagnoses', json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def result(self, run_id):
        return asyncio.run(self.service.wait_result(run_id, self.owner))

    def test_actual_live_api_dispatches_v2_and_replays_same_key_with_one_final_event(self):
        incident = self.create()
        start = self.start(incident)
        result = self.result(start['run_id'])
        self.assertEqual(result['engine_id'], 'autonomous_v2')
        self.assertEqual(result['execution_mode'], 'live')
        self.assertTrue(result['evidence'])
        self.assertFalse(result['repair_eligible'])
        self.assertEqual(self.start(incident), {**start, 'replayed': True})
        self.assertEqual(len(self.factory_calls), 1)
        self.assertEqual(len(self.store.runs), 1)
        events = self.store.events(start['run_id'])
        self.assertEqual([event['seq'] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(sum(event['type'] == 'final' for event in events), 1)
        replay = self.client.get(start['event_url'], headers={'Last-Event-ID': str(events[-2]['seq'])})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.text.count('"type": "final"'), 1)
        changed = self.client.post(f'/api/v1/incidents/{incident["id"]}/diagnoses', json={
            'request_key': 'start-v2', 'execution_mode': 'live', 'model_budget': 9, 'tool_budget': 4})
        self.assertEqual(changed.status_code, 409)

    def test_public_run_and_sse_never_persist_model_credentials_or_private_config(self):
        incident = self.create()
        start = self.start(incident)
        self.result(start['run_id'])
        run = self.client.get(start['result_url'])
        events = self.client.get(start['event_url'])
        self.assertEqual(run.status_code, 200)
        self.assertEqual(events.status_code, 200)
        joined = run.text + events.text + json.dumps(self.store.runs, default=str)
        for secret in ('private-model-key-for-control', 'private-model-endpoint.invalid', 'never-persist-this',
                       '_model_config', '_provider_registry'):
            self.assertNotIn(secret, joined)
        self.assertIn('model_settings_sha256', run.text)

    def test_owner_boundaries_cover_v2_run_and_event_replay(self):
        incident = self.create()
        start = self.start(incident)
        self.result(start['run_id'])
        for principal in (Principal('api_v2', 'bob'), Principal('another', 'alice')):
            self.principal = principal
            for path in (start['result_url'], start['event_url'], f'/api/v1/incidents/{incident["id"]}'):
                self.assertEqual(self.client.get(path).status_code, 404)
            denied = self.client.post(f'/api/v1/incidents/{incident["id"]}/diagnoses',
                                      json={'request_key': 'foreign'})
            self.assertEqual(denied.status_code, 404)

    def test_admitted_worker_keeps_engine_limits_and_deep_model_settings(self):
        incident = self.create()
        submitted = []

        def defer(fn, *args):
            submitted.append((fn, args))

        with patch.object(self.service._executor, 'submit', side_effect=defer):
            start = self.start(incident)
        fn, args = submitted[0]
        request = args[1]
        frozen = deepcopy(request['run_spec'])
        self.config.extra_body['test_setting'] = 'changed-after-admission'
        self.service._base_config = IncidentConfig(api_key='replacement-key', model='replacement-model')
        with patch.dict(os.environ, {'INCIDENT_MAX_SECONDS': '25', 'INCIDENT_MAX_MODEL_CALLS': '6'}):
            fn(*args)
        result = self.result(start['run_id'])
        self.assertEqual(result['engine_id'], 'autonomous_v2')
        self.assertEqual(result['run_spec'], frozen)
        self.assertEqual(result['run_summary']['limits'], frozen['limits'])
        self.assertEqual(self.factory_calls, [('private-model-key-for-control', 'control-model')])
        self.assertEqual(request['_model_config'].extra_body['test_setting'], 'frozen')

    def test_environment_limits_are_respected_and_frozen_at_admission(self):
        incident = self.create()
        with patch.dict(os.environ, {'INCIDENT_MAX_SECONDS': '30', 'INCIDENT_MAX_INPUT_CHARS': '4000',
                                     'INCIDENT_MAX_MODEL_CALLS': '6', 'INCIDENT_MAX_TOOL_CALLS': '3'}):
            start = self.start(incident)
        result = self.result(start['run_id'])
        limits = self.store.get(start['run_id'])['config']['run_spec']['limits']
        self.assertEqual(limits['seconds'], 30)
        self.assertEqual(limits['input_chars'], 4000)
        self.assertEqual(limits['model_calls'], 6)
        self.assertEqual(limits['tool_calls'], 3)
        self.assertEqual(result['run_summary']['limits'], limits)

    def test_scripted_api_uses_same_v2_engine_without_model_factory(self):
        incident = self.create()
        result = self.result(self.start(incident, live=False)['run_id'])
        self.assertEqual(result['engine_id'], 'autonomous_v2')
        self.assertEqual(result['execution_mode'], 'scripted_control_only')
        self.assertFalse(self.factory_calls)
        self.assertTrue(result['evidence'])

    def test_direct_repair_proposals_cannot_bypass_v2_source_gate(self):
        # Isolate the source gate before target resolution or any write-capable executor.
        run = {'run_id': 'source-v2', 'status': 'completed',
               'config': {'incident_id': 'iid', 'run_spec': {'engine_id': 'autonomous_v2'}},
               'result': {'engine_id': 'autonomous_v2', 'review_status': 'passed', 'repair_eligible': False,
                          'evidence': [{'evidence_id': 'E1', 'kind': 'observation'}]}}
        payload = {'request_key': 'repair', 'revision': 1, 'run_id': 'source-v2',
                   'action': 'restart_service', 'target': 'api', 'parameters': {},
                   'reason': 'Manual proposal must still respect the source eligibility.', 'evidence_ids': ['E1']}
        with patch.object(self.service, 'get_run', return_value=run), \
             patch.object(self.service.repairs, '_transaction', side_effect=AssertionError('source gate bypassed')):
            with self.assertRaises(IncidentError) as denied:
                self.service.repairs.propose('iid', payload, self.owner)
        self.assertEqual(denied.exception.code, 'REPAIR_ENGINE_NOT_ELIGIBLE')

    def test_recommendation_source_gate_uses_config_when_result_engine_is_missing(self):
        run = {'config': {'incident_id': 'iid', 'run_spec': {'engine_id': 'autonomous_v2'}},
               'result': {'review_status': 'passed'}}
        with patch.object(self.service, 'get_run', return_value=run):
            with self.assertRaises(IncidentError) as denied:
                self.service.repairs.from_recommendation('iid', {'run_id': 'run', 'action_index': 0}, self.owner)
        self.assertEqual(denied.exception.code, 'REPAIR_ENGINE_NOT_ELIGIBLE')

    def test_existing_repair_plan_cannot_be_approved_from_v2_source(self):
        run = {'config': {'run_spec': {'engine_id': 'autonomous_v2'}}, 'result': {}}
        incident = {'revision': 1, 'latest_run_id': 'run-v2', 'business_status': 'awaiting_confirmation'}
        plan = {'proposal': {'revision': 1, 'run_id': 'run-v2'},
                'expires_at': '2099-01-01T00:00:00+00:00'}
        with patch.object(self.service, 'get_run', return_value=run), \
             patch.object(self.service.repairs, '_target', side_effect=AssertionError('source gate bypassed')):
            with self.assertRaises(IncidentError) as denied:
                self.service.repairs._valid(incident, plan, self.owner)
        self.assertEqual(denied.exception.code, 'REPAIR_ENGINE_NOT_ELIGIBLE')

    def test_frozen_binding_drift_fails_before_provider_or_model_invocation(self):
        fields = next(row['fields'] for row in catalog(self.owner) if row['case_id'] == 'case_001')
        binding = ServiceBinding(self.owner.tenant_id, (self.owner.user_id,), fields['service'],
            fields['environment'], fields['service_version'],
            observations={'prometheus': {'base_url': 'https://private-metrics-endpoint.invalid'},
                          'metrics': {'latency': {'unit': 'ms', 'query': 'private_query'}}})
        self.service.services = ServiceRegistry([binding])
        response = self.client.post('/api/v1/incidents', json={**fields, 'demo_case_id': None})
        self.assertEqual(response.status_code, 201)
        incident = response.json()
        submitted = []
        with patch.object(self.service._executor, 'submit', side_effect=lambda fn, *a: submitted.append((fn, a))):
            start = self.start(incident)
        fn, args = submitted[0]
        request = args[1]
        stored = self.store.get(start['run_id'])['config']
        self.assertEqual(stored['run_spec']['provider_binding_sha256'], binding.fingerprint)
        self.assertEqual(stored['workflow_engine'], 'autonomous_loop')
        request['_provider_registry']._bindings[0].observations['metrics']['latency']['unit'] = 'seconds'
        with patch('providers.registry.ServiceRegistry.provider', side_effect=AssertionError('provider called')):
            fn(*args)
        result = self.result(start['run_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['run_summary']['termination_reason'], 'PROVIDER_BINDING_CHANGED')
        self.assertEqual(result['run_summary']['model_calls'], 0)
        self.assertFalse(self.factory_calls)
        raw = json.dumps(self.store.get(start['run_id']), default=str)
        self.assertNotIn('private-metrics-endpoint', raw)
        self.assertNotIn('private_query', raw)


if __name__ == '__main__':
    unittest.main()
