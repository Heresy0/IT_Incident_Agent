"""Same engine with response doubles; semantic quality is measured separately."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from langchain_core.messages import AIMessage
from api.auth import Principal
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from workflow.v2.engine import investigate
from workflow.v2.scripted import scripted_model

P = Principal('team_a', 'alice')


class QueueModel:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.payloads = []

    def invoke(self, messages):
        payload = json.loads(messages[-1].content)
        self.payloads.append(payload)
        stage = payload.get('stage')
        if stage == 'investigate':
            value = next(self.steps)
        elif stage == 'draft':
            value = {'claims': [], 'next_steps': [], 'gaps': ['需要进一步核实机制。']}
        else:
            value = {'items': []}
        return AIMessage(content=value if isinstance(value, str) else json.dumps(value), response_metadata={'finish_reason': 'stop'})


def query(tool, **args):
    return {'kind': 'query', 'question': '检查登记观测', 'tool': tool, 'args': args}


class AutonomousEngineTests(unittest.TestCase):
    def run_steps(self, steps, *, provider=None, budget=10, tools=8):
        provider = provider or FixtureProvider('case_001', P)
        model = QueueModel(steps)
        result = investigate(model, provider, P, limits=Limits(model_calls=budget, tool_calls=tools, seconds=180))
        return result, model

    def test_scripted_same_engine_runs_multiple_services_without_profile(self):
        for cid in ('case_001', 'case_004'):
            with self.subTest(cid=cid):
                provider = FixtureProvider(cid, P)
                result = investigate(scripted_model(provider), provider, P, limits=Limits(model_calls=8, tool_calls=6))
                self.assertTrue(result['evidence'])
                self.assertTrue(result['item_reviews'])
                self.assertEqual(result['engine_id'], 'autonomous_v2')
                self.assertFalse(result['repair_eligible'])
                self.assertLessEqual(result['run_summary']['model_calls'], 8)
                self.assertIn('源记录', result['final'])

    def test_invalid_request_can_change_tool_without_executing_bad_target(self):
        result, model = self.run_steps([query('shell', command='whoami'),
                                      query('get_service_metrics', metrics=['db_cpu']), {'kind': 'finish'}])
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertTrue(result['evidence'])
        self.assertIn('TOOL_DENIED', [e.get('code') for e in result['events']])
        self.assertEqual(model.payloads[1]['input']['feedback']['code'], 'TOOL_DENIED')

    def test_partial_evidence_survives_malformed_step(self):
        result, _ = self.run_steps([query('get_service_metrics', metrics=['db_cpu']), 'invalid json', {'kind': 'finish'}])
        self.assertTrue(result['output']['findings'])
        self.assertEqual(result['repairs'], 1)
        self.assertTrue(any(e['category'] == 'syntax' for e in result['protocol_failures']))

    def test_failed_broad_query_does_not_block_narrow_read(self):
        provider = FixtureProvider('case_001', P)
        original = provider.query
        calls = []
        def read(name, args, scope, **kw):
            calls.append(args)
            if len(calls) == 1:
                raise ProviderError('PROVIDER_UNAVAILABLE')
            return original(name, args, scope, **kw)
        from datetime import timedelta
        narrow = (provider.ticket().scope.start + timedelta(minutes=1)).isoformat()
        with patch.object(provider, 'query', side_effect=read):
            result, _ = self.run_steps([query('get_service_logs', category='dependency'),
                query('get_service_logs', category='dependency', start=narrow), {'kind': 'finish'}], provider=provider)
        self.assertEqual(len(calls), 2)
        self.assertTrue(result['evidence'])
        self.assertEqual(result['status'], 'partial')

    def test_budget_increase_never_rejects_same_trajectory_earlier(self):
        counts = []
        for budget in (6, 7, 8, 9, 10):
            steps = [query('get_service_metrics', metrics=['db_cpu']),
                     query('get_service_logs', category='dependency'), {'kind': 'finish'}]
            result, _ = self.run_steps(steps, budget=budget)
            counts.append(result['run_summary']['tool_calls'])
            self.assertLessEqual(result['run_summary']['model_calls'], budget)
        self.assertEqual(counts, sorted(counts))

    def test_same_query_is_not_executed_twice(self):
        q = query('get_service_metrics', metrics=['db_cpu'])
        result, _ = self.run_steps([q, q, q, q, {'kind': 'finish'}], budget=12)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertIn('query_reused', [e['type'] for e in result['events']])

    def test_tool_budget_one_still_publishes_source_facts(self):
        result, _ = self.run_steps([query('get_service_metrics', metrics=['db_cpu'])], budget=6, tools=1)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertTrue(result['output']['findings'])

    def test_failure_never_becomes_empty_or_confirmed_cause(self):
        provider = FixtureProvider('case_001', P)
        with patch.object(provider, 'query', side_effect=ProviderError('PROVIDER_AUTH_MISSING')):
            result, _ = self.run_steps([query('get_service_metrics', metrics=['db_cpu'])], provider=provider)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['output']['hypotheses'], [])
        self.assertEqual(result['collection'][0]['status'], 'error')

    def test_status_check_does_not_publish_causal_claims(self):
        provider = FixtureProvider('case_001', P)
        provider._data['ticket']['purpose'] = 'status_check'
        result = investigate(scripted_model(provider), provider, P, limits=Limits(model_calls=8, tool_calls=6))
        self.assertEqual(result['output']['hypotheses'], [])
        self.assertEqual(result['output']['recommended_actions'], [])
        self.assertTrue(result['output']['findings'])


if __name__ == '__main__':
    unittest.main()
