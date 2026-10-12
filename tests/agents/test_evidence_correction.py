"""Verify correction routing against actual HTTP tool serialization and ledgers."""
import json
import unittest
from unittest.mock import patch
import httpx

from agents.models import build_model
from agents.investigation import investigate
from providers.fixtures import ProviderError
from tests.tools.test_incident_tools import P, executor, window
from tests.agents import test_incident_investigation as fixture


class EvidenceCorrectionTests(unittest.TestCase):
    def run_sequence(self, ex, reply):
        requests = []
        def send(client, request, **kwargs):
            payload = json.loads(request.content)
            requests.append(payload)
            return httpx.Response(200, request=request, json=reply(len(requests), payload))
        args = {**window(ex), 'metrics': ['db_cpu']}
        model = build_model('test-only', base_url='https://example.invalid/v1', extra_body={})
        ex.context.scope = ex.scope
        with patch('httpx.Client.send', autospec=True, side_effect=send):
            result = investigate(model, ex.provider, P, executor=ex, context=ex.context,
                event_log=[], objective={'required_checks': [{'tool': 'get_service_metrics', 'args': args}]})
        return result, requests

    def test_invalid_finding_before_read_keeps_tools_for_required_check(self):
        ex = executor()
        args = {**window(ex), 'metrics': ['db_cpu']}
        def reply(n, payload):
            if n == 1:
                return fixture.IncidentInvestigationTests.compatible_reply('{"findings":[{"statement":"unobserved","refs":[]}]}')
            if n == 2:
                self.assertIn('tools', payload)
                self.assertNotIn('No additional tool calls required', json.dumps(payload))
                return fixture.IncidentInvestigationTests.compatible_reply('', tools=[{'id': 'read', 'type': 'function',
                    'function': {'name': 'get_service_metrics', 'arguments': json.dumps(args)}}])
            self.assertNotIn('tools', payload)
            source = next(iter(ex.evidence.values()))
            ref = next(o.reference_id for o in source.reference_options if o.field_path == 'value')
            return fixture.IncidentInvestigationTests.compatible_reply(json.dumps({'findings': [
                {'statement': 'Current measurement', 'refs': [{'reference_id': ref}]}]}))
        result, requests = self.run_sequence(ex, reply)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['run_summary']['model_calls'], 3)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertTrue(result['output']['findings'][0]['refs'])

    def test_completed_read_corrects_refs_without_repeating_tool(self):
        ex = executor()
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        source = next(iter(ex.evidence.values()))
        ref = next(o.reference_id for o in source.reference_options if o.field_path == 'value')
        def reply(n, payload):
            self.assertNotIn('tools', payload)
            content = ('{"findings":[{"statement":"measurement","refs":[]}]}' if n == 1 else
                       json.dumps({'findings': [{'statement': 'measurement', 'refs': [{'reference_id': ref}]}]}))
            return fixture.IncidentInvestigationTests.compatible_reply(content)
        result, requests = self.run_sequence(ex, reply)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['run_summary']['tool_calls'], 1)

    def test_failed_read_preserves_gap_without_fake_ref_or_retry(self):
        ex = executor()
        args = {**window(ex), 'metrics': ['db_cpu']}
        def reply(n, payload):
            if n == 1:
                return fixture.IncidentInvestigationTests.compatible_reply('', tools=[{'id': 'read', 'type': 'function',
                    'function': {'name': 'get_service_metrics', 'arguments': json.dumps(args)}}])
            if n == 2:
                return fixture.IncidentInvestigationTests.compatible_reply('{"findings":[{"statement":"no result","refs":[]}]}')
            self.assertNotIn('tools', payload)
            return fixture.IncidentInvestigationTests.compatible_reply('{"findings":[],"missing_information":["SOURCE_UNAVAILABLE; human follow-up required"]}')
        with patch.object(ex.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')):
            result, requests = self.run_sequence(ex, reply)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['output']['findings'], [])
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertEqual(result['run_summary']['model_calls'], 3)
        self.assertIn('SOURCE_UNAVAILABLE', json.dumps(result['output']))


if __name__ == '__main__':
    unittest.main()
