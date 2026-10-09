"""Offline execution lifecycle and isolation; never contacts a daemon or model API."""
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
import unittest
from unittest.mock import patch
import httpx
from api.auth import Principal
from providers.bound import catalog
from providers.registry import ServiceBinding, ServiceRegistry
from providers.fixtures import ProviderError
from repairs.contracts import RepairProposal
from repairs.executors import DockerExecutor, DockerCLIExecutor
from repairs.verification import SymptomVerifier
from storage.runs import MemoryRunStore
from workflow.service import WorkflowService
from incidents.support import IncidentError
from tests.support import TestConfig


class OfflineExecutor:
    name = 'offline'
    live = False
    actions = {'restart_service', 'scale_service', 'rollback_release'}
    verification = 'Offline deterministic health check.'

    def __init__(self):
        self.writes = self.checks = 0
        self.healthy = True
        self.timeout = False
        self.entered, self.release = Event(), Event()
        self.release.set()

    def preflight(self, *args):
        return {'running': False}

    def execute(self, *args):
        self.writes += 1
        self.entered.set()
        self.release.wait(5)
        if self.timeout:
            raise ProviderError('TIMEOUT', True)

    def verify(self, *args):
        self.checks += 1
        return {'passed': self.healthy}


class RemediationTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.operator = Principal('a', 'alice'), Principal('a', 'alice', 'operator')
        self.executor = OfflineExecutor()
        fields = catalog(self.owner)[0]['fields']
        self.binding = ServiceBinding('a', ('alice',), fields['service'], fields['environment'], fields['service_version'],
            repairs={'api': {'executor': 'offline', 'actions': list(self.executor.actions),
                             'max_replicas': 3, 'versions': ['v1']}})
        self.store = MemoryRunStore()
        self.service = WorkflowService('unused', store=self.store, config=TestConfig(),
            service_registry=ServiceRegistry([self.binding]), repair_executors={'offline': self.executor})
        self.incident = self.service.create_incident(fields, self.owner)
        run_id, _ = self.service.start_diagnosis(self.incident['id'], {'request_key': 'diagnose',
            'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}, self.owner)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = self.service.get_run(run_id, self.owner)
            if run['status'] != 'running':
                break
            time.sleep(.02)
        self.assertNotEqual(run['status'], 'running')
        result = run['result']
        self.assertEqual(result['review_status'], 'passed')
        self.payload = {'request_key': 'plan', 'revision': 1, 'run_id': run_id,
            'action': 'restart_service', 'target': 'api', 'parameters': {},
            'reason': 'Operator reviewed scoped evidence.', 'evidence_ids': [result['evidence'][0]['evidence_id']]}

    def tearDown(self):
        self.executor.release.set()
        self.service.close()

    def plan(self):
        return self.service.repairs.propose(self.incident['id'], self.payload, self.owner)

    def test_status_check_result_cannot_become_a_repair_source(self):
        self.store.runs[self.payload['run_id']]['result']['purpose'] = 'status_check'
        with self.assertRaises(IncidentError) as denied:
            self.plan()
        self.assertEqual(denied.exception.code, 'REPAIR_REQUIRES_INCIDENT_DIAGNOSIS')
        self.assertEqual(self.executor.writes, 0)

    def approve(self, plan):
        return self.service.repairs.approve(self.incident['id'], plan['id'],
            {'digest': plan['digest'], 'decision': 'approve'}, self.operator)

    def execute(self, plan, principal=None, key='write'):
        return self.service.repairs.execute(self.incident['id'], plan['id'], {'request_key': key}, principal or self.operator)

    def test_full_lifecycle_is_persisted_idempotent_and_does_not_close_incident(self):
        plan = self.plan()
        self.assertEqual(self.plan(), plan)
        self.assertEqual(self.executor.writes, 0)
        self.approve(plan)
        result = self.execute(plan)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.execute(plan), result)
        self.assertEqual((self.executor.writes, self.executor.checks), (1, 1))
        self.assertNotEqual(self.service.get_incident(self.incident['id'], self.owner)['business_status'], 'resolved')
        returned = self.service.repairs.list(self.incident['id'], self.owner)[0]
        self.assertEqual(returned.pop('effective_status'), 'verified')
        self.assertEqual(returned, result)
        with self.assertRaises(IncidentError):
            self.execute(plan, key='different')

    def test_operator_digest_and_owner_boundaries(self):
        plan = self.plan()
        for fn in (lambda: self.execute(plan), lambda: self.approve({**plan, 'digest': '0' * 64}),
                   lambda: self.execute(plan, self.owner),
                   lambda: self.service.repairs.list(self.incident['id'], Principal('a', 'bob')),
                   lambda: self.service.repairs.list(self.incident['id'], Principal('b', 'alice'))):
            with self.assertRaises(IncidentError):
                fn()
        self.assertEqual(self.executor.writes, 0)

    def test_unregistered_target_unknown_evidence_and_key_reuse(self):
        for changes in ({'target': 'other'}, {'evidence_ids': ['invented']}, {'revision': 2}):
            with self.assertRaises(IncidentError):
                self.service.repairs.propose(self.incident['id'], {**self.payload, **changes}, self.owner)
        self.plan()
        with self.assertRaises(IncidentError):
            self.service.repairs.propose(self.incident['id'], {**self.payload, 'reason': 'changed'}, self.owner)

    def test_expiry_revision_and_binding_changes_invalidate_approval(self):
        plan = self.plan()
        self.approve(plan)
        self.store.repair_plans[self.incident['id']][0]['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(IncidentError, 'REPAIR_EXPIRED'):
            self.execute(plan)
        self.store.repair_plans[self.incident['id']][0]['expires_at'] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        self.service.services = ServiceRegistry([ServiceBinding(**{**self.binding.__dict__, 'dependencies': ('new',)})])
        with self.assertRaisesRegex(IncidentError, 'CONFIGURATION_CHANGED'):
            self.execute(plan)
        self.service.services = ServiceRegistry([self.binding])
        self.store.incidents[self.incident['id']]['revision'] = 2
        with self.assertRaisesRegex(IncidentError, 'SOURCE_CHANGED'):
            self.execute(plan)
        self.assertEqual(self.executor.writes, 0)

    def test_rejected_plan_cannot_execute(self):
        plan = self.plan()
        self.service.repairs.approve(self.incident['id'], plan['id'], {'digest': plan['digest'], 'decision': 'reject'}, self.operator)
        with self.assertRaises(IncidentError):
            self.execute(plan)

    def test_timeout_is_uncertain_and_never_retried(self):
        self.executor.timeout = True
        plan = self.plan(); self.approve(plan)
        result = self.execute(plan)
        self.assertEqual(result['status'], 'manual_required')
        self.execute(plan)
        self.assertEqual(self.executor.writes, 1)
        self.assertEqual(self.executor.checks, 0)

    def test_failed_health_is_bounded_and_requires_manual_work(self):
        self.executor.healthy = False
        plan = self.plan(); self.approve(plan)
        with patch('repairs.service.time.sleep'):
            self.assertEqual(self.execute(plan)['status'], 'manual_required')
        self.assertEqual((self.executor.writes, self.executor.checks), (1, 3))

    def test_concurrency_and_incident_edit_guard(self):
        plan = self.plan(); self.approve(plan)
        self.executor.release.clear()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.execute, plan)
            self.assertTrue(self.executor.entered.wait(3))
            self.assertEqual(self.execute(plan)['status'], 'executing')
            with self.assertRaisesRegex(IncidentError, 'REPAIR_IN_PROGRESS'):
                self.service.patch_incident(self.incident['id'], catalog(self.owner)[0]['fields'], 1, self.owner)
            with self.assertRaisesRegex(IncidentError, 'REPAIR_IN_PROGRESS'):
                self.service.start_diagnosis(self.incident['id'], {'request_key': 'second',
                    'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}, self.owner)
            self.executor.release.set()
            self.assertEqual(first.result()['status'], 'verified')
        self.assertEqual(self.executor.writes, 1)

    def test_interrupted_write_is_not_resumed(self):
        plan = self.plan(); self.approve(plan)
        self.store.repair_plans[self.incident['id']][0].update(status='executing', execution_key='write')
        self.store.recover_repairs()
        self.assertEqual(self.execute(plan)['status'], 'manual_required')
        self.assertEqual(self.executor.writes, 0)

    def test_live_executor_cannot_use_synthetic_diagnosis(self):
        self.executor.live = True
        with self.assertRaisesRegex(IncidentError, 'SYNTHETIC_DIAGNOSIS'):
            self.plan()

    def test_parameter_limits_and_unsupported_actions(self):
        with self.assertRaises(ValueError):
            RepairProposal.model_validate({**self.payload, 'parameters': {'command': 'echo x'}})
        for changes in ({'action': 'scale_service', 'parameters': {'replicas': 4}},
                        {'action': 'rollback_release', 'parameters': {'version': 'unknown'}}):
            with self.assertRaises(IncidentError):
                self.service.repairs.propose(self.incident['id'], {**self.payload, **changes}, self.owner)
        self.executor.actions = {'restart_service'}
        with self.assertRaisesRegex(IncidentError, 'ACTION_UNSUPPORTED'):
            self.service.repairs.propose(self.incident['id'], {**self.payload, 'action': 'scale_service',
                'parameters': {'replicas': 2}}, self.owner)

    def test_preflight_failure_does_not_write_and_capacity_is_bounded(self):
        plan = self.plan(); self.approve(plan)
        with patch.object(self.executor, 'preflight', side_effect=ProviderError('TARGET_BUSY')):
            self.assertEqual(self.execute(plan)['status'], 'blocked')
        self.assertEqual(self.executor.writes, 0)
        self.service.repairs.capacity.acquire()
        self.service.repairs.capacity.acquire()
        try:
            with self.assertRaisesRegex(IncidentError, 'CAPACITY'):
                self.execute(plan)
        finally:
            self.service.repairs.capacity.release()
            self.service.repairs.capacity.release()

    def configure_symptoms(self, values):
        values = iter(values)
        latest = [10]
        target = {**self.binding.repairs['api'], 'symptom_checks': [
            {'metric': 'queue_depth', 'operator': 'lte', 'threshold': 0, 'max_age_seconds': 60}]}
        binding = ServiceBinding(**{**self.binding.__dict__, 'repairs': {'api': target}, 'observations': {
            'prometheus': {'url': 'http://127.0.0.1:9090'},
            'metrics': {'queue_depth': {'query': 'sum(queue_depth{service="test"})',
                                       'freshness_query': 'max(time()-timestamp(queue_depth{service="test"}))', 'unit': 'jobs'}}}})
        self.service.services = ServiceRegistry([binding])
        def reply(request):
            if 'timestamp' in request.url.params['query']:
                return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector',
                    'result': [{'value': [datetime.now(timezone.utc).timestamp(), '1']}]}})
            latest[0] = next(values, latest[0])
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector',
                'result': [{'value': [datetime.now(timezone.utc).timestamp(), str(latest[0])]}]}})
        self.service.repairs.symptom_verifier = SymptomVerifier(transport=httpx.MockTransport(reply))

    def test_healthy_container_is_insufficient_when_original_symptom_remains(self):
        self.configure_symptoms([10])
        plan = self.plan(); self.approve(plan)
        with patch('repairs.service.time.sleep'):
            result = self.execute(plan)
        self.assertEqual(result['status'], 'manual_required')
        observations = [event for event in result['events'] if event['type'] == 'verification']
        self.assertEqual(len(observations), 3)
        self.assertTrue(observations[0]['observation']['passed'])
        self.assertFalse(observations[0]['symptoms']['passed'])
        self.assertEqual(self.executor.writes, 1)

    def test_original_symptom_recovery_is_recorded_and_required(self):
        self.configure_symptoms([10, 0])
        plan = self.plan(); self.approve(plan)
        self.assertEqual(plan['symptom_checks'][0]['unit'], 'jobs')
        result = self.execute(plan)
        self.assertEqual(result['status'], 'verified')
        observation = next(event for event in result['events'] if event['type'] == 'verification')
        self.assertTrue(observation['symptoms']['passed'])
        self.assertEqual(observation['symptoms']['checks'][0]['value'], 0)

    def test_already_recovered_symptom_prevents_unnecessary_write(self):
        self.configure_symptoms([0])
        plan = self.plan(); self.approve(plan)
        result = self.execute(plan)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['events'][-2]['code'], 'SYMPTOM_ALREADY_HEALTHY')
        self.assertEqual(self.executor.writes, 0)

    def test_crash_after_action_boundary_preserves_audit_and_never_replays(self):
        plan = self.plan(); self.approve(plan)
        with patch.object(self.executor, 'execute', side_effect=SystemExit('simulated process loss')):
            with self.assertRaises(SystemExit):
                self.execute(plan)
        stored = self.store.get_repairs(self.incident['id'], self.owner)[0]
        self.assertEqual(stored['events'][-1]['type'], 'action_started')
        self.assertEqual(stored['status'], 'executing')
        self.store.recover_repairs()
        self.assertEqual(self.execute(plan)['status'], 'manual_required')

    def test_action_is_not_sent_if_audit_boundary_cannot_commit(self):
        plan = self.plan(); self.approve(plan)
        original = self.store.repair_transaction
        def transaction(iid, principal, mutation):
            def check(incident, plans):
                result = mutation(incident, plans)
                if plans[-1]['events'][-1]['type'] == 'action_started':
                    raise RuntimeError('synthetic storage failure')
                return result
            return original(iid, principal, check)
        with patch.object(self.store, 'repair_transaction', side_effect=transaction):
            result = self.execute(plan)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.executor.writes, 0)
        self.assertNotIn('action_started', [event['type'] for event in result['events']])

    def test_read_projection_shows_expiry_without_mutating_approval_or_audit(self):
        plan = self.plan(); self.approve(plan)
        stored = self.store.repair_plans[self.incident['id']][0]
        stored['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        before = deepcopy(stored)
        read = self.service.repairs.get(self.incident['id'], plan['id'], self.owner)
        self.assertEqual(read['effective_status'], 'expired')
        self.assertEqual(read['status'], 'approved')
        self.assertEqual(self.store.repair_plans[self.incident['id']][0], before)


class DockerAdapterTests(unittest.TestCase):
    def test_cli_uses_fixed_arguments_and_does_not_expose_inspect_environment(self):
        from subprocess import CompletedProcess
        response = {'Id': 'a' * 64, 'Config': {'Healthcheck': {'Test': ['CMD', 'true']}, 'Env': ['KEY=hidden']},
                    'State': {'Running': True, 'StartedAt': 'old'}}
        import json
        with patch('repairs.executors.shutil.which', return_value='docker.exe'), patch(
                'repairs.executors.subprocess.run', return_value=CompletedProcess([], 0, json.dumps([response]), '')) as run:
            observation = DockerCLIExecutor().preflight({'container_id': 'a' * 64, 'context': 'desktop-linux'}, 'restart_service', {})
            self.assertNotIn('hidden', str(observation))
            self.assertEqual(run.call_args.args[0], ['docker.exe', '--context', 'desktop-linux', 'inspect', '--type', 'container', 'a' * 64])
            self.assertFalse(run.call_args.kwargs['shell'])
        with self.assertRaises(ProviderError):
            DockerCLIExecutor().execute({'container_id': '../other'}, 'restart_service', {})

    def test_agent_candidate_requires_registered_capability_and_current_evidence(self):
        from types import SimpleNamespace
        from agents.contracts import DiagnosisSelection
        from evidence.diagnosis import expand_diagnosis, ProtocolError
        selected = DiagnosisSelection.model_validate({'recommended_actions': [{
            'action': 'Restart registered service', 'condition': 'Approved', 'expected_result': 'Healthy',
            'risk': 'Temporary interruption', 'requires_approval': True,
            'repair': {'action': 'restart_service', 'target': 'api', 'parameters': {}, 'evidence_ids': ['EV_current']}}]})
        registry = SimpleNamespace(provider=SimpleNamespace(repair_capabilities=[{'action': 'restart_service', 'target': 'api'}]),
                                   evidence={'EV_current': SimpleNamespace(kind='observation', locator='get_service_metrics/row')}, references={})
        self.assertEqual(expand_diagnosis(selected, registry).recommended_actions[0].repair.target, 'api')
        registry.evidence['EV_current'].kind = 'past_incident'
        with self.assertRaises(ProtocolError):
            expand_diagnosis(selected, registry)
        registry.evidence['EV_current'].kind = 'observation'
        registry.provider.repair_capabilities = []
        with self.assertRaises(ProtocolError):
            expand_diagnosis(selected, registry)
    def test_exact_container_and_restart_then_health_verification(self):
        calls = []
        def reply(request):
            calls.append((request.method, request.url.path))
            if request.method == 'POST':
                return httpx.Response(204)
            return httpx.Response(200, json={'Id': 'a' * 64, 'Config': {'Healthcheck': {'Test': ['CMD', 'true']}},
                'State': {'Running': True, 'StartedAt': 'old' if len(calls) == 1 else 'new', 'Health': {'Status': 'healthy'}}})
        executor = DockerExecutor(transport=httpx.MockTransport(reply))
        target = {'url': 'http://127.0.0.1:2375', 'container_id': 'a' * 64}
        before = executor.preflight(target, 'restart_service', {})
        executor.execute(target, 'restart_service', {})
        self.assertTrue(executor.verify(target, 'restart_service', {}, before)['passed'])
        self.assertEqual([call[0] for call in calls], ['GET', 'POST', 'GET'])

    def test_running_without_healthcheck_or_changed_start_is_insufficient(self):
        def reply(request):
            return httpx.Response(200, json={'Id': 'a' * 64, 'Config': {}, 'State': {'Running': True, 'StartedAt': 'same'}})
        executor = DockerExecutor(transport=httpx.MockTransport(reply))
        target = {'url': 'http://127.0.0.1:2375', 'container_id': 'a' * 64}
        with self.assertRaises(ProviderError):
            executor.preflight(target, 'restart_service', {})
        self.assertFalse(executor.verify(target, 'restart_service', {}, {'started_at': 'same'})['passed'])


class RepairAPITests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.auth import get_current_principal
        from api.repairs import router
        from workflow.dependencies import get_workflow_service
        self.fixture = RemediationTests()
        self.fixture.setUp()
        self.identity = self.fixture.owner
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_principal] = lambda: self.identity
        app.dependency_overrides[get_workflow_service] = lambda: self.fixture.service
        self.client = TestClient(app)
        self.base = '/api/v1/incidents/' + self.fixture.incident['id'] + '/repairs'

    def tearDown(self):
        self.client.close()
        self.fixture.tearDown()

    def test_api_plan_approval_execution_and_owner_replay(self):
        response = self.client.post(self.base, json=self.fixture.payload)
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        route = self.base + '/' + plan['id']
        self.assertEqual(self.client.post(route + '/approval', json={'digest': plan['digest'], 'decision': 'approve'}).status_code, 403)
        self.identity = self.fixture.operator
        self.assertEqual(self.client.post(route + '/approval', json={'digest': plan['digest'], 'decision': 'approve'}).status_code, 200)
        response = self.client.post(route + '/execute', json={'request_key': 'execute'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'verified')
        self.client.post(route + '/execute', json={'request_key': 'execute'})
        self.assertEqual(self.fixture.executor.writes, 1)
        self.identity = Principal('a', 'bob', 'operator')
        self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_reviewed_structured_recommendation_becomes_pending_plan(self):
        payload = self.fixture.payload
        run = self.fixture.store.runs[payload['run_id']]
        run['result']['output']['recommended_actions'] = [{'action': 'Restart the registered API after approval.',
            'requires_approval': True, 'repair': {key: payload[key] for key in ('action', 'target', 'parameters', 'evidence_ids')}}]
        response = self.client.post(self.base + '/from-recommendation', json={
            'request_key': 'from_agent', 'revision': 1, 'run_id': payload['run_id'], 'action_index': 0})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['status'], 'pending_approval')
        self.assertEqual(self.fixture.executor.writes, 0)

    def test_extra_commands_are_rejected_at_http_boundary(self):
        response = self.client.post(self.base, json={**self.fixture.payload, 'parameters': {'command': 'anything'}})
        self.assertEqual(response.status_code, 422)

    def test_detail_and_cursor_events_are_authorized_and_do_not_execute(self):
        plan = self.client.post(self.base, json=self.fixture.payload).json()
        route = self.base + '/' + plan['id']
        self.assertEqual(self.client.get(route).json()['effective_status'], 'pending_approval')
        self.identity = self.fixture.operator
        self.client.post(route + '/approval', json={'digest': plan['digest'], 'decision': 'approve'})
        first = self.client.get(route + '/events', params={'limit': 1}).json()
        self.assertEqual(first['items'][0]['seq'], 1)
        self.assertTrue(first['has_more'])
        tail = self.client.get(route + '/events', params={'after_seq': first['next_seq']}).json()
        self.assertEqual(tail['items'][0]['type'], 'approved')
        self.assertEqual(self.fixture.executor.writes, 0)
        self.identity = Principal('a', 'bob')
        self.assertEqual(self.client.get(route + '/events').status_code, 404)
