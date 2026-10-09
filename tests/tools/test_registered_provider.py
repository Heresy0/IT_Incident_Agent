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
    def test_registered_service_can_search_own_confirmed_history_without_leaking_shared_service_cases(self):
        from providers.history import ProviderWithHistory
        from storage.runs import MemoryRunStore
        from evidence.contracts import IncidentsArgs
        from dataclasses import replace
        store = MemoryRunStore()
        store.cases['confirmed'] = {'id': 'confirmed', 'tenant_id': 'tenant', 'user_id': 'alice', 'validity': 'active',
            'revision': 1, 'confirmed_at': self.start.isoformat(), 'document': {'incident': self.snapshot,
                'confirmation': {'confirmed_cause': 'worker unavailable', 'resolution': 'restart worker token=hidden', 'error_codes': []}}}
        binding = replace(self.binding, users=('alice', 'bob'))
        registry = ServiceRegistry([binding])
        for principal, expected in ((self.principal, 1), (Principal('tenant', 'bob'), 0)):
            ticket = registry.ticket(self.snapshot, principal)
            base = HTTPObservationProvider(binding, ticket, principal)
            rows, _ = ProviderWithHistory(base, store, principal).query('search_incidents',
                IncidentsArgs(symptoms='worker'), ticket.scope)
            self.assertEqual(len(rows), expected)
            self.assertNotIn('hidden', str(rows))
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

    def test_registered_categories_are_advertised_and_unavailable_reads_cost_no_tools(self):
        provider = HTTPObservationProvider(self.binding, self.ticket, self.principal,
            transport=httpx.MockTransport(lambda _: self.fail('unregistered reads must not reach HTTP')))
        ex = ToolExecutor(provider, self.principal, self.ticket.scope, RunContext())
        schema = next(s['function'] for s in ex.schemas() if s['function']['name'] == 'get_service_logs')
        self.assertEqual(schema['parameters']['properties']['category']['enum'], ['resource'])
        self.assertEqual(ex.capabilities()['registered_log_categories'], ['resource'])
        metric = next(s['function'] for s in ex.schemas() if s['function']['name'] == 'get_service_metrics')
        self.assertEqual(metric['parameters']['properties']['metrics']['items']['enum'], ['queue_depth'])
        self.assertNotIn('sum(queue_depth', str(ex.capabilities()))
        for _ in range(2):
            result = ex.execute('get_service_logs', {'start': self.snapshot['start'], 'end': self.snapshot['end'],
                                                     'category': 'configuration'})
            self.assertEqual(result.error.code, 'LOG_CATEGORY_NOT_REGISTERED')
        self.assertEqual(ex.context.counts['tool_calls'], 0)
        self.assertEqual(ex.seen, set())

    def test_long_metric_window_fits_full_evidence_budget_and_records_actual_step(self):
        from dataclasses import replace
        from providers.history import ProviderWithHistory
        from storage.runs import MemoryRunStore
        binding = replace(self.binding, observations={**self.binding.observations, 'metrics': {
            'queue_depth': {'query': 'sum(queue)', 'unit': 'jobs'},
            'workers_active': {'query': 'sum(workers)', 'unit': 'workers'}}})
        snapshot = {**self.snapshot, 'start': (self.start + timedelta(microseconds=538105)).isoformat(),
            'end': (self.start + timedelta(hours=24, microseconds=538105)).isoformat()}
        ticket = ServiceRegistry([binding]).ticket(snapshot, self.principal)
        calls = []
        def reply(request):
            calls.append(request)
            params = request.url.params
            start, end, step = float(params['start']), float(params['end']), int(params['step'])
            self.assertGreaterEqual(start, ticket.scope.start.timestamp())
            self.assertLessEqual(end, ticket.scope.end.timestamp())
            self.assertAlmostEqual(end * 1000, round(end * 1000), places=3)
            points = [[start + i * step, '1'] for i in range(int((end - start) // step) + 1)]
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix',
                'result': [{'values': points}]}})
        base = HTTPObservationProvider(binding, ticket, self.principal, transport=httpx.MockTransport(reply))
        ex = ToolExecutor(ProviderWithHistory(base, MemoryRunStore(), self.principal), self.principal, ticket.scope, RunContext())
        result = ex.execute('get_service_metrics', {'start': snapshot['start'], 'end': snapshot['end'],
            'metrics': ['queue_depth', 'workers_active'], 'granularity': '1m'})
        self.assertEqual(result.status, 'ok')
        self.assertFalse(result.truncated)
        self.assertLessEqual(len(result.model_dump_json()), 12000)
        self.assertEqual({e.payload['metric'] for e in result.evidence}, {'queue_depth', 'workers_active'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(ex.context.counts['tool_calls'], 1)
        self.assertTrue(all(e.observed_to <= ticket.scope.end for e in result.evidence))
        self.assertLess((ticket.scope.end - result.evidence[0].observed_to).total_seconds(), .001)
        for item in result.evidence:
            self.assertEqual(item.payload['requested_granularity'], '1m')
            self.assertEqual(item.payload['granularity'], calls[0].url.params['step'] + 's')
        # A short focused query retains the requested five-minute interval.
        focused = ex.execute('get_service_metrics', {'start': (ticket.scope.end-timedelta(minutes=5)).isoformat(),
            'end': snapshot['end'], 'metrics': ['queue_depth'], 'granularity': '5m'})
        self.assertFalse(focused.truncated)
        self.assertEqual(focused.evidence[0].payload['granularity'], '5m')

    def test_unexpected_extra_metric_samples_are_still_reported_as_truncated(self):
        calls = []
        def reply(request):
            calls.append(request)
            points = [[self.start.timestamp() + i, '1'] for i in range(61)]
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix',
                'result': [{'values': points}]}})
        provider = HTTPObservationProvider(self.binding, self.ticket, self.principal, transport=httpx.MockTransport(reply))
        ex = ToolExecutor(provider, self.principal, self.ticket.scope, RunContext())
        result = ex.execute('get_service_metrics', {'start': self.snapshot['start'], 'end': self.snapshot['end'],
            'metrics': ['queue_depth']})
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.model_dump_json()), 12000)

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
