"""Feedback-driven wiring on the real engine; doubles do not score model quality."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from langchain_core.messages import AIMessage

from api.auth import Principal
from providers.fixtures import ProviderError
from providers.registry import ServiceBinding, ServiceRegistry
from runtime.context import Limits
from workflow.v2.engine import investigate


P = Principal('adaptation-tenant', 'reader')


class ScopedProvider:
    """A complete queryable world, with metadata independent of outcome."""
    name = 'adaptation_test_world'

    def __init__(self, *, outcome='high', metric='pending_units', service='unseen-worker', extra_metrics=()):
        self.outcome = outcome
        self.metric = metric
        start = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.binding = ServiceBinding(P.tenant_id, (P.user_id,), service, 'staging', 'v1', observations={
            'prometheus': {'url': 'http://127.0.0.1:9090'},
            'loki': {'url': 'http://127.0.0.1:3100'},
            'metrics': {name: {'query': 'operator_registered_query', 'unit': 'items',
                               'description': 'Count of work waiting to be processed',
                               'observed_object': 'pending work', 'kind': 'gauge'}
                        for name in (metric, *extra_metrics)},
            'log_queries': {name: 'operator_registered_log_partition'
                            for name in ('configuration', 'dependency', 'resource')},
            'log_metadata': {
                'configuration': {'description': 'Local configuration events'},
                'dependency': {'description': 'Outgoing request observations'},
                'resource': {'description': 'Worker capacity events'}}})
        snapshot = {'id': 'same-incident', 'title': 'Processing slowed',
            'symptoms': 'Tasks are taking longer than expected', 'service': service,
            'environment': 'staging', 'service_version': 'v1',
            'start': start.isoformat(), 'end': (start + timedelta(hours=1)).isoformat()}
        self._ticket = ServiceRegistry([self.binding]).ticket(snapshot, P)
        self.calls = []

    def ticket(self):
        return self._ticket.model_copy(deep=True)

    def authorize(self, principal, scope):
        if principal != P or scope != self._ticket.scope:
            raise ProviderError('SCOPE_DENIED')

    def row(self, args, identifier, **fields):
        return {'id': identifier, 'timestamp': args.end.isoformat(),
                'service': self.binding.service, 'environment': self.binding.environment,
                'data_version': self.binding.version, **fields}

    def query(self, name, args, scope, **options):
        self.authorize(P, scope)
        self.calls.append((name, args.model_dump(mode='json')))
        if name == 'get_service_metrics':
            if self.outcome == 'failed':
                raise ProviderError('PROVIDER_UNAVAILABLE')
            value = 20 if self.outcome == 'high' else 0
            actual_step = '20m' if (args.end - args.start).total_seconds() > 1200 else '1m'
            return [self.row(args, metric + '-sample', metric=metric, value=value,
                             unit='items', aggregation='registered', granularity=actual_step,
                             requested_granularity=args.granularity)
                    for metric in args.metrics], False
        if name == 'get_service_logs':
            return [self.row(args, args.category + '-event', category=args.category,
                             level='INFO', error_code=None,
                             message='A source record from the selected channel')], False
        raise AssertionError('Unexpected test query: ' + name)


class FeedbackModel:
    """Reads only the supplied capability catalog and actual engine feedback."""
    def __init__(self):
        self.requests = []

    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        self.requests.append(deepcopy(request))
        payload = request.get('input', request)
        if request.get('stage') == 'investigate':
            collection = payload['collection']
            if not collection:
                metric = next(name for name, metadata in payload['capabilities']['registered_metrics'].items()
                              if metadata.get('observed_object') == 'pending work')
                value = {'kind': 'query', 'tool': 'get_service_metrics', 'args': {'metrics': [metric]}}
            elif len(collection) == 1:
                if collection[0]['status'] == 'error':
                    category = 'configuration'
                else:
                    observed = next(source['payload']['value'] for source in
                                    payload['observations']['evidence'] if 'value' in source['payload'])
                    category = 'resource' if observed > 0 else 'dependency'
                value = {'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': category}}
            else:
                value = {'kind': 'finish'}
        elif request.get('stage') == 'draft':
            value = {'claims': [], 'next_steps': [], 'gaps': ['Wiring test has no semantic verdict.']}
        else:
            raise AssertionError('An empty test draft should not require semantic review')
        return AIMessage(content=json.dumps(value), response_metadata={'finish_reason': 'stop'})


class SequenceModel:
    def __init__(self, queries):
        self.queries = iter([*queries, {'kind': 'finish'}])

    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        value = next(self.queries) if request.get('stage') == 'investigate' else {
            'claims': [], 'next_steps': [], 'gaps': ['Acquisition coverage regression only.']}
        return AIMessage(content=json.dumps(value), response_metadata={'finish_reason': 'stop'})


class AutonomousAdaptationTests(unittest.TestCase):
    def run_engine(self, provider, model):
        return investigate(model, provider, P, limits=Limits(model_calls=12, tool_calls=8, seconds=180))

    def test_same_initial_input_changes_next_legal_channel_after_actual_observation_or_failure(self):
        initial_inputs = []
        for outcome, expected in (('high', 'resource'), ('low', 'dependency'), ('failed', 'configuration')):
            with self.subTest(outcome=outcome):
                provider, model = ScopedProvider(outcome=outcome), FeedbackModel()
                result = self.run_engine(provider, model)
                self.assertEqual([name for name, _ in provider.calls], ['get_service_metrics', 'get_service_logs'])
                self.assertEqual(provider.calls[1][1]['category'], expected)
                self.assertEqual(result['run_summary']['tool_calls'], 2)
                self.assertTrue(result['evidence'])
                first = model.requests[0]['input']
                initial_inputs.append({key: first[key] for key in ('ticket', 'capabilities', 'observations', 'collection')})
                feedback = model.requests[1]['input']['collection'][0]
                self.assertEqual(feedback['status'], 'error' if outcome == 'failed' else 'ok')
        self.assertEqual(initial_inputs[0], initial_inputs[1])
        self.assertEqual(initial_inputs[0], initial_inputs[2])

    def test_metric_coverage_does_not_combine_different_rows_or_block_new_narrow_windows(self):
        provider = ScopedProvider(metric='metric_a', extra_metrics=('metric_b',))
        scope = provider.ticket().scope
        narrow = (scope.end - timedelta(minutes=10)).isoformat()
        narrower = (scope.end - timedelta(minutes=5)).isoformat()

        def metric_query(name, start=None):
            return {'kind': 'query', 'tool': 'get_service_metrics',
                    'args': {'metrics': [name], **({'start': start} if start else {})}}

        model = SequenceModel([metric_query('metric_a'), metric_query('metric_b', narrow),
                               metric_query('metric_a', narrow), metric_query('metric_a', narrower)])
        result = self.run_engine(provider, model)
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual([args['metrics'] for _, args in provider.calls],
                         [['metric_a'], ['metric_b'], ['metric_a'], ['metric_a']])
        self.assertEqual(result['run_summary']['tool_calls'], 4)
        self.assertFalse(any(event['type'] == 'query_reused' for event in result['events']))
        self.assertEqual([datetime.fromisoformat(args['start']) for _, args in provider.calls][1:],
                         [datetime.fromisoformat(value) for value in (narrow, narrow, narrower)])

    def test_new_service_and_renamed_metric_use_same_semantics_without_fault_profile(self):
        for service, metric in (('worker-alpha', 'pending_units'), ('unseen-worker-beta', 'backlog_gauge')):
            with self.subTest(service=service, metric=metric):
                provider, model = ScopedProvider(service=service, metric=metric), FeedbackModel()
                result = self.run_engine(provider, model)
                self.assertNotIn('profile', provider.binding.observations)
                self.assertEqual(provider.calls[0][1]['metrics'], [metric])
                self.assertEqual(provider.calls[1][1]['category'], 'resource')
                first = model.requests[0]['input']['capabilities']
                self.assertEqual(first['registered_metrics'][metric]['observed_object'], 'pending work')
                self.assertEqual(result['engine_id'], 'autonomous_v2')
                self.assertFalse(result['repair_eligible'])


if __name__ == '__main__':
    unittest.main()
