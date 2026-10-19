"""Acquisition and metadata boundaries; these tests do not measure model judgment."""
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from api.auth import Principal
from providers.configuration import ServiceConfig
from providers.fixtures import FixtureProvider, ProviderError
from providers.http import HTTPObservationProvider, safe_text
from providers.registry import ServiceBinding, ServiceRegistry
from runtime.context import RunContext
from tools.executor import ToolExecutor
from tools.query_coverage import complete_read, read_state
from workflow.task_checks import completion


class AutonomousCoverageTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal('demo', 'reader')
        self.provider = FixtureProvider('case_001', self.principal)
        scope = self.provider.ticket().scope
        self.executor = ToolExecutor(self.provider, self.principal, scope, RunContext(scope=scope), role='single')
        self.broad = self.check(scope.start, scope.end)
        self.narrow = self.check(scope.end - timedelta(minutes=2), scope.end)

    def check(self, start, end):
        args = self.executor.validate_args('get_service_logs', {
            'start': start.isoformat(), 'end': end.isoformat(), 'category': 'dependency'})
        return {'tool': 'get_service_logs', 'args': args.model_dump(mode='json')}

    def test_failed_or_truncated_broad_attempt_does_not_cover_new_narrow_query(self):
        for variant in ('failed', 'truncated'):
            with self.subTest(variant=variant):
                self.setUp()
                original = self.provider.query

                def incomplete(*args, **kwargs):
                    if variant == 'failed':
                        raise ProviderError('PROVIDER_UNAVAILABLE')
                    rows, _ = original(*args, **kwargs)
                    return rows, True

                with patch.object(self.provider, 'query', side_effect=incomplete):
                    self.executor.execute(self.broad['tool'], self.broad['args'])
                self.assertEqual(completion([self.broad], self.executor)['checks'][0]['status'], variant)
                self.assertEqual(completion([self.narrow], self.executor)['checks'][0]['status'], 'not_executed')
                result = self.executor.execute(self.narrow['tool'], self.narrow['args'])
                self.assertNotEqual(result.status, 'error')
                self.assertEqual(self.executor.context.counts['tool_calls'], 2)

    def test_complete_empty_superset_covers_narrow_request_without_claiming_health(self):
        with patch.object(self.provider, 'query', return_value=([], False)):
            self.executor.execute(self.broad['tool'], self.broad['args'])
        state = completion([self.narrow], self.executor)
        self.assertEqual(state['checks'][0]['status'], 'empty')
        self.assertTrue(state['query_complete'])
        self.assertFalse(state['goal_verified'])

    def test_quality_classification_is_independent_of_semantic_success(self):
        row = {'status': 'empty', 'truncated': False}
        self.assertTrue(complete_read(row))
        self.assertEqual(read_state(row), 'complete_empty')
        self.assertEqual(read_state({**row, 'truncated': True}), 'truncated')
        self.assertEqual(read_state({'status': 'error', 'error': 'SOURCE_NOT_CONFIGURED'}), 'channel_unavailable')
        self.assertEqual(read_state({'status': 'error', 'error': 'TIMEOUT'}), 'failed')

    def test_missing_metric_samples_are_visible_in_ledger_without_invented_values(self):
        original = self.provider.query

        def missing(*args, **kwargs):
            rows, truncated = original(*args, **kwargs)
            return [row for row in rows if row['metric'] != 'request_latency'], truncated

        args = {'start': self.executor.scope.start.isoformat(), 'end': self.executor.scope.end.isoformat(),
                'metrics': ['dependency_error_rate', 'request_latency']}
        with patch.object(self.provider, 'query', side_effect=missing):
            self.executor.execute('get_service_metrics', args)
        row = next(iter(self.executor.check_results.values()))
        self.assertEqual(row['read_state'], 'missing_samples')
        self.assertEqual(row['missing_metrics'], ['request_latency'])
        self.assertEqual({e.payload['metric'] for e in self.executor.evidence.values()}, {'dependency_error_rate'})


class AutonomousCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal('tenant', 'reader')
        start = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.snapshot = {'id': 'incident', 'title': 'requests delayed', 'symptoms': 'latency increased',
            'service': 'unknown-app', 'environment': 'staging', 'service_version': 'v1',
            'start': start.isoformat(), 'end': (start + timedelta(minutes=10)).isoformat()}
        self.binding = ServiceBinding('tenant', ('reader',), 'unknown-app', 'staging', 'v1', observations={
            'prometheus': {'url': 'http://127.0.0.1:9090', 'authorization_env': 'METRICS_KEY'},
            'loki': {'url': 'http://127.0.0.1:3100'},
            'metrics': {'pending_units': {'query': 'private_registered_query', 'unit': 'items',
                'description': 'Number of tasks waiting for an available worker', 'kind': 'gauge',
                'observed_object': 'worker queue', 'sampling': 'one sample per minute',
                'source': 'queue exporter', 'version': '2'}},
            'log_queries': {'resource': 'private_log_selector'},
            'log_metadata': {'resource': {'description': 'Worker lifecycle events',
                'fields': {'event': 'Lifecycle event name'}, 'version': '3'}}})

    def executor(self, binding=None):
        binding = binding or self.binding
        registry = ServiceRegistry([binding])
        ticket = registry.ticket(self.snapshot, self.principal)
        provider = HTTPObservationProvider(binding, ticket, self.principal)
        return ToolExecutor(provider, self.principal, ticket.scope, RunContext(scope=ticket.scope), role='single')

    def test_optional_metadata_validates_without_a_fault_profile(self):
        document = {**self.binding.__dict__, 'users': list(self.binding.users), 'dependencies': []}
        parsed = ServiceConfig.model_validate(document)
        self.assertEqual(parsed.observations.metrics['pending_units'].kind, 'gauge')
        self.assertEqual(parsed.observations.log_metadata['resource'].fields['event'], 'Lifecycle event name')
        ex = self.executor()
        capabilities = ex.capabilities(generic=True)
        self.assertEqual(capabilities['registered_metrics']['pending_units']['observed_object'], 'worker queue')
        self.assertEqual(capabilities['log_channels']['resource']['version'], '3')
        visible = json.dumps(capabilities) + json.dumps(ServiceRegistry([self.binding]).catalog(self.principal))
        for secret in ('private_registered_query', 'private_log_selector', '127.0.0.1', 'METRICS_KEY'):
            self.assertNotIn(secret, visible)

    def test_generic_schema_uses_registered_semantics_and_preserves_authorization(self):
        ex = self.executor()
        schema = {row['function']['name']: row['function'] for row in ex.schemas(generic=True)}
        self.assertEqual(len(schema), 6)
        metric = schema['get_service_metrics']
        self.assertIn('Number of tasks waiting', metric['description'])
        self.assertEqual(metric['parameters']['properties']['metrics']['items']['enum'], ['pending_units'])
        self.assertNotIn('db_cpu', metric['parameters']['properties']['metrics']['description'])
        self.assertNotIn('private_registered_query', json.dumps(schema))
        result = ex.execute('get_service_metrics', {'start': self.snapshot['start'], 'end': self.snapshot['end'],
                                                   'metrics': ['db_cpu']})
        self.assertEqual(result.error.code, 'METRIC_NOT_REGISTERED')
        self.assertEqual(ex.context.counts['tool_calls'], 0)

    def test_metric_rename_keeps_metadata_and_missing_description_is_unknown(self):
        observations = dict(self.binding.observations)
        observations['metrics'] = {'unseen_gauge': dict(observations['metrics']['pending_units']),
                                   'undocumented_gauge': {'query': 'private_query', 'unit': 'items'}}
        ex = self.executor(replace(self.binding, observations=observations))
        metadata = ex.capabilities(generic=True)['registered_metrics']
        self.assertEqual(metadata['unseen_gauge']['description'],
                         'Number of tasks waiting for an available worker')
        self.assertEqual(metadata['undocumented_gauge']['description'], 'unknown')

    def test_legacy_view_is_unchanged_and_fixture_view_does_not_infer_semantics(self):
        ex = self.executor()
        self.assertEqual(ex.capabilities()['registered_metrics'], {'pending_units': {'unit': 'items'}})
        legacy = next(s['function'] for s in ex.schemas() if s['function']['name'] == 'get_service_metrics')
        self.assertIn('db_cpu', legacy['parameters']['properties']['metrics']['description'])
        principal = Principal('demo', 'reader')
        provider = FixtureProvider('case_001', principal)
        fixture = ToolExecutor(provider, principal, provider.ticket().scope, RunContext(), role='single')
        self.assertEqual(fixture.capabilities(), {})
        self.assertTrue(fixture.capabilities(generic=True)['registered_metrics'])
        self.assertTrue(all(row['description'] == 'unknown' for row in
                            fixture.capabilities(generic=True)['registered_metrics'].values()))
        hidden = dict(provider._data['metrics'][0], metric='other_tenant_metric', tenant_id='other',
                      visibility='private')
        provider._data['metrics'].append(hidden)
        self.assertNotIn('other_tenant_metric', fixture.capabilities(generic=True)['registered_metrics'])

    def test_basic_auth_credentials_are_redacted_before_model_visible_text(self):
        for text in ('Authorization: Basic ZHVtbXk6ZHVtbXk=',
                     '{"authorization":"Basic ZHVtbXk6ZHVtbXk="}',
                     'Bearer secret_token'):
            self.assertNotIn('ZHVtbXk6ZHVtbXk=', safe_text(text))
            self.assertNotIn('secret_token', safe_text(text))


if __name__ == '__main__':
    unittest.main()
