"""New service profiles reuse scoped providers; no real observations or models."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from api.auth import Principal
from providers.configuration import read_configuration, validate_configuration
from providers.http import HTTPObservationProvider
from providers.registry import ServiceRegistry
from runtime.context import RunContext
from scripts.register_service import main, ROOT
from tools.executor import ToolExecutor


class EnterpriseServiceTests(unittest.TestCase):
    def setUp(self):
        self.data = read_configuration(ROOT / 'examples/services.enterprise-stack.example.json')
        self.start = datetime(2026, 10, 10, tzinfo=timezone.utc)
        self.principal = Principal('example_tenant', 'example_operator', 'operator')

    def executor(self, row, reply):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'services.json'
            path.write_text(json.dumps({'services': [row]}), encoding='utf-8')
            registry = ServiceRegistry.from_file(path)
        binding = registry._bindings[0]
        ticket = registry.ticket({'id': 'test', 'title': '服务核查', 'symptoms': 'test',
            'service': binding.service, 'environment': binding.environment, 'service_version': binding.version,
            'start': self.start.isoformat(), 'end': (self.start + timedelta(minutes=5)).isoformat()}, self.principal)
        provider = HTTPObservationProvider(binding, ticket, self.principal, transport=httpx.MockTransport(reply))
        return ToolExecutor(provider, self.principal, ticket.scope, RunContext())

    def window(self):
        return {'start': self.start.isoformat(), 'end': (self.start + timedelta(minutes=5)).isoformat()}

    def test_three_profiles_are_valid_and_keep_missing_metrics_explicit(self):
        issues = validate_configuration(self.data)
        self.assertFalse([item for item in issues if item['level'] == 'error'])
        missing = [item for item in issues if item['path'].endswith('.metrics')]
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(item['level'] == 'warning' for item in missing))
        for row in self.data['services']:
            self.assertEqual(row['repairs'], {})
        self.assertEqual(len(self.data['services'][0]['observations']['metrics']), 4)

    def test_profile_generation_adapts_identity_owner_and_runbook_version_offline(self):
        for profile in ('enterprise-api', 'postgres', 'redis'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'services.json'
                with redirect_stdout(io.StringIO()), patch('providers.http.httpx.Client', side_effect=AssertionError('no network')):
                    code = main(['template', '--profile', profile, '--service', 'registered-' + profile,
                        '--tenant', 'team', '--user', 'alice', '--version', 'v2', '--owner-team', 'alice',
                        '--output', str(path)])
                self.assertEqual(code, 0)
                row = read_configuration(path)['services'][0]
                self.assertEqual(row['repairs'], {})
                self.assertEqual(row['observations']['owners'][0]['aliases'], [row['service']])
                self.assertEqual(row['observations']['runbooks'][0]['versions'], ['v2'])
                self.assertIn(row['service'], row['observations']['runbooks'][0]['text'])

    def test_log_only_services_hide_metric_tools_and_reject_reads_before_budget(self):
        for row in self.data['services'][1:]:
            with self.subTest(service=row['service']):
                executor = self.executor(row, lambda _: self.fail('unavailable metric must not make requests'))
                names = {schema['function']['name'] for schema in executor.schemas()}
                self.assertIn('get_service_logs', names)
                self.assertNotIn('get_service_metrics', names)
                result = executor.execute('get_service_metrics', {**self.window(), 'metrics': ['api_up']})
                self.assertEqual(result.error.code, 'SOURCE_NOT_CONFIGURED')
                self.assertEqual(executor.context.counts['tool_calls'], 0)

    def test_api_events_remove_business_fields_but_keep_operational_error_type(self):
        def reply(request):
            self.assertIn('service="api"', request.url.params['query'])
            record = {'event': 'qa.failed', 'error_type': 'TimeoutError', 'tenant_id': 'private-tenant',
                      'request_id': 'private-id', 'question': 'private-question', 'message': 'private-business-content'}
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'streams',
                'result': [{'values': [[str(int(self.start.timestamp() * 1e9)), json.dumps(record)]]}]}})
        executor = self.executor(self.data['services'][0], reply)
        result = executor.execute('get_service_logs', {**self.window(), 'category': 'dependency'})
        self.assertEqual(result.status, 'ok')
        self.assertEqual(len(result.evidence), 1)
        text = result.model_dump_json()
        self.assertIn('qa.failed', text)
        self.assertIn('TimeoutError', text)
        self.assertNotIn('private-', text)

    def test_projected_database_and_cache_logs_use_their_own_fixed_streams(self):
        for row, component, line in zip(self.data['services'][1:], ('postgres', 'redis'),
                ('database system is ready to accept connections', 'Ready to accept connections')):
            calls = []
            def reply(request):
                calls.append(request)
                self.assertEqual(request.method, 'GET')
                query = request.url.params['query']
                self.assertIn('service="' + component + '"', query)
                self.assertIn('| regexp ', query)
                self.assertIn('| line_format "{{.state}}"', query)
                return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'streams',
                    'result': [{'values': [[str(int(self.start.timestamp() * 1e9)), line]]}]}})
            executor = self.executor(row, reply)
            result = executor.execute('get_service_logs', {**self.window(), 'category': 'resource'})
            self.assertEqual(result.status, 'ok')
            self.assertEqual(result.evidence[0].payload['message'], line)
            self.assertEqual(len(calls), 1)

    def test_empty_api_observations_remain_empty_and_registry_scopes_new_services(self):
        row = deepcopy(self.data['services'][0])
        executor = self.executor(row, lambda _: httpx.Response(200, json={'status': 'success',
            'data': {'resultType': 'matrix', 'result': []}}))
        result = executor.execute('get_service_metrics', {**self.window(), 'metrics': ['api_up']})
        self.assertEqual(result.status, 'empty')
        self.assertFalse(result.evidence)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'services.json'
            path.write_text(json.dumps(self.data), encoding='utf-8')
            registry = ServiceRegistry.from_file(path)
        self.assertEqual(len(registry.catalog(self.principal)), 3)
        self.assertEqual(registry.catalog(Principal('other', 'example_operator')), [])
        self.assertEqual(registry.catalog(Principal('example_tenant', 'other')), [])
