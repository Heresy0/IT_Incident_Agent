"""Probe registered observations without models, persistence or external operations."""
from datetime import datetime, timezone
import json
import unittest

import httpx

from api.auth import Principal
from providers.http import HTTPObservationProvider
from providers.registry import ServiceBinding, ServiceRegistry
from scripts.probe_registered_service import probe


class RegisteredProbeTests(unittest.TestCase):
    def setup_registry(self, *, logs=True, owners=True, fail=False, second_metric=False):
        self.now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.calls = []
        observations = {
            'prometheus': {'url': 'http://127.0.0.1:9090'}, 'loki': {'url': 'http://127.0.0.1:3100'},
            'metrics': {'workers_active': {'query': 'sum(workers)', 'unit': 'workers'}},
            'log_queries': {'resource': '{service="worker"}'},
            'log_mapping': {'event_only': True, 'event_allowlist': ['indexing.worker.started']},
            'owners': [{'timestamp': self.now.isoformat(), 'team': 'alice', 'aliases': ['enterprise-indexing']}]
                      if owners else [],
            'runbooks': [{'timestamp': self.now.isoformat(), 'text': 'enterprise-indexing Worker 检查手册', 'versions': ['v1']}],
        }
        if second_metric:
            observations['metrics']['queue_depth'] = {'query': 'sum(queue)', 'unit': 'jobs'}
        binding = ServiceBinding('team_a', ('alice',), 'enterprise-indexing', 'staging', 'v1', observations=observations)
        def reply(request):
            self.calls.append(request)
            self.assertEqual(request.method, 'GET')
            if fail:
                return httpx.Response(503)
            if request.url.path == '/api/v1/query_range':
                results = [] if second_metric and request.url.params['query'] == 'sum(queue)' else [
                    {'values': [[float(request.url.params['start']), '1']]}]
                return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'matrix', 'result': results}})
            values = [[str(int(self.now.timestamp() * 1e9)), json.dumps({'event': 'indexing.worker.started',
                       'message': 'hidden text', 'token': 'private-token'})]] if logs else []
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'streams', 'result': [{'values': values}]}})
        return ServiceRegistry([binding], provider_factory=lambda *args: HTTPObservationProvider(
            *args, transport=httpx.MockTransport(reply)))

    def test_free_probe_uses_all_existing_checks_refs_scope_and_one_shared_counter(self):
        registry = self.setup_registry()
        report = probe(registry, Principal('team_a', 'alice', 'operator'), 'enterprise-indexing', minutes=1440, now=self.now)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(len(report['checks']), 4)
        self.assertTrue(all(c['validated_references'] == 1 for c in report['checks']))
        self.assertEqual(report['run_summary']['model_calls'], 0)
        self.assertEqual(report['run_summary']['tool_calls'], 4)
        self.assertFalse(report['diagnosis_performed'])
        self.assertFalse(report['repairs_performed'])
        self.assertNotIn('private-token', json.dumps(report))
        self.assertNotIn('hidden text', json.dumps(report))
        metric_start = float(self.calls[0].url.params['start'])
        self.assertEqual(self.now.timestamp() - metric_start, 300)
        self.assertEqual(self.calls[1].url.params['limit'], '51')

    def test_empty_logs_and_owner_are_gaps_not_success_or_model_fallback(self):
        registry = self.setup_registry(logs=False, owners=False)
        report = probe(registry, Principal('team_a', 'alice'), 'enterprise-indexing', now=self.now)
        self.assertEqual(report['status'], 'partial')
        self.assertIn('logs/resource: NO_MATCHING_OBSERVATIONS', report['gaps'])
        self.assertIn('owner: NO_MATCHING_OBSERVATIONS', report['gaps'])
        self.assertEqual(report['run_summary']['model_calls'], 0)

    def test_http_failure_remains_error_and_does_not_become_empty(self):
        registry = self.setup_registry(fail=True)
        report = probe(registry, Principal('team_a', 'alice'), 'enterprise-indexing', now=self.now)
        self.assertEqual(report['status'], 'partial')
        self.assertIn('metrics/workers_active: PROVIDER_HTTP_ERROR', report['gaps'])
        self.assertEqual(report['checks'][0]['status'], 'error')

    def test_scope_and_window_rejections_do_not_send_requests(self):
        registry = self.setup_registry()
        for principal, minutes in ((Principal('team_a', 'bob'), 60), (Principal('other', 'alice'), 60),
                                   (Principal('team_a', 'alice'), 1441)):
            with self.assertRaises(ValueError):
                probe(registry, principal, 'enterprise-indexing', minutes=minutes, now=self.now)
        self.assertEqual(self.calls, [])

    def test_one_missing_metric_cannot_be_hidden_by_another_valid_series(self):
        registry = self.setup_registry(second_metric=True)
        report = probe(registry, Principal('team_a', 'alice'), 'enterprise-indexing', now=self.now)
        self.assertEqual(report['status'], 'partial')
        self.assertIn('metrics: NO_SAMPLES_FOR_queue_depth', report['gaps'])
