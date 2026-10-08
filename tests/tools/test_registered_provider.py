from datetime import datetime, timezone, timedelta
import unittest
import httpx
from api.auth import Principal
from evidence.contracts import MetricsArgs, LogsArgs
from providers.registry import ServiceBinding, ServiceRegistry
from providers.http import HTTPObservationProvider, request_json
from providers.fixtures import ProviderError
from tools.executor import ToolExecutor
from runtime.context import RunContext


class RegisteredProviderTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal('tenant', 'alice')
        self.start = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.snapshot = {'id': 'ticket', 'title': 'queue stuck', 'symptoms': 'worker unavailable',
            'service': 'example', 'environment': 'staging', 'service_version': 'v1',
            'start': self.start.isoformat(), 'end': (self.start + timedelta(minutes=30)).isoformat()}
        self.binding = ServiceBinding('tenant', ('alice',), 'example', 'staging', 'v1',
            observations={'prometheus': {'url': 'http://127.0.0.1:9090'}, 'loki': {'url': 'http://127.0.0.1:3100'},
                'metrics': {'queue_depth': {'query': 'sum(queue_depth{service="example"})', 'unit': 'jobs'}},
                'log_queries': {'resource': '{service="example"}'}})
        self.registry = ServiceRegistry([self.binding])
        self.ticket = self.registry.ticket(self.snapshot, self.principal)

    def test_scope_does_not_expand_with_caller_service_version_or_user(self):
        for snapshot, principal in (({**self.snapshot, 'service': 'other'}, self.principal),
                                    ({**self.snapshot, 'service_version': 'v2'}, self.principal),
                                    (self.snapshot, Principal('tenant', 'bob')),
                                    (self.snapshot, Principal('other', 'alice'))):
            with self.assertRaises(ProviderError):
                self.registry.ticket(snapshot, principal)

    def test_registered_metric_and_fixed_query_reach_existing_evidence_executor(self):
        calls = []
        def reply(request):
            calls.append(request)
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix',
                'result': [{'values': [[self.start.timestamp(), '7']]}]}})
        provider = HTTPObservationProvider(self.binding, self.ticket, self.principal, transport=httpx.MockTransport(reply))
        executor = ToolExecutor(provider, self.principal, self.ticket.scope, RunContext())
        self.assertIn('queue_depth', str(executor.schemas()))
        result = executor.execute('get_service_metrics', {'start': self.snapshot['start'], 'end': self.snapshot['end'],
                                                         'metrics': ['queue_depth']})
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.evidence[0].payload['value'], 7)
        self.assertEqual(result.evidence[0].payload['unit'], 'jobs')
        self.assertEqual(calls[0].url.params['query'], self.binding.observations['metrics']['queue_depth']['query'])
        self.assertEqual(executor.context.counts['tool_calls'], 1)
        result = executor.execute('get_service_metrics', {'start': self.snapshot['start'], 'end': self.snapshot['end'],
                                                         'metrics': ['unregistered']})
        self.assertEqual(result.error.code, 'METRIC_NOT_REGISTERED')
        self.assertEqual(len(calls), 1)

    def test_log_messages_are_redacted_and_missing_metadata_is_explicit(self):
        def reply(request):
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'streams', 'result': [
                {'values': [[str(int(self.start.timestamp() * 1e9)), '{"level":"ERROR","message":"token=hidden password=hidden Bearer hidden"}']]}]}})
        provider = HTTPObservationProvider(self.binding, self.ticket, self.principal, transport=httpx.MockTransport(reply))
        args = LogsArgs(start=self.start, end=self.ticket.scope.end, category='resource')
        rows, _ = provider.query('get_service_logs', args, self.ticket.scope)
        self.assertNotIn('hidden', rows[0]['message'])
        with self.assertRaises(ProviderError) as raised:
            provider.query('get_service_owner', None, self.ticket.scope)
        self.assertEqual(raised.exception.code, 'SOURCE_NOT_CONFIGURED')

    def test_multi_series_redirect_and_nonlocal_cleartext_are_rejected(self):
        args = MetricsArgs(start=self.start, end=self.ticket.scope.end, metrics=['queue_depth'])
        provider = HTTPObservationProvider(self.binding, self.ticket, self.principal, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix', 'result': [{}, {}]}})))
        with self.assertRaises(ProviderError):
            provider.query('get_service_metrics', args, self.ticket.scope)
        for base, transport in (('http://remote.example', None), ('http://127.0.0.1', httpx.MockTransport(
                lambda request: httpx.Response(302, headers={'Location': 'https://other.example'})))):
            with self.assertRaises(ProviderError):
                request_json(base, 'GET', '/health', transport=transport)

    def test_free_ticket_uses_registered_scope_but_does_not_run_case_script(self):
        import time
        from storage.runs import MemoryRunStore
        from workflow.service import WorkflowService
        from tests.support import TestConfig
        service = WorkflowService('unused', store=MemoryRunStore(), config=TestConfig(), service_registry=self.registry)
        try:
            fields = {key: value for key, value in self.snapshot.items() if key != 'id'}
            fields['occurred_at'] = fields['start']
            incident = service.create_incident(fields, self.principal)
            run_id, _ = service.start_diagnosis(incident['id'], {'request_key': 'free', 'execution_mode': 'scripted_control_only',
                'model_budget': None, 'tool_budget': None}, self.principal)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                run = service.get_run(run_id, self.principal)
                if run['status'] != 'running':
                    break
                time.sleep(.02)
            self.assertEqual(run['result']['run_summary']['termination_reason'], 'LIVE_DIAGNOSIS_REQUIRED')
            self.assertEqual(run['result']['run_summary']['model_calls'], 0)
        finally:
            service.close()
