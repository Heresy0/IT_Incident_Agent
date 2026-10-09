"""Untrusted structured logs must preserve evidence without copying business content."""
from dataclasses import replace
from datetime import timedelta
import json
import unittest

import httpx
from pydantic import ValidationError

from evidence.contracts import LogsArgs
from providers.fixtures import ProviderError
from providers.http import HTTPObservationProvider, safe_text
from providers.logs import LogMapping
from runtime.context import RunContext
from tools.executor import ToolExecutor
from tests.tools import test_registered_provider as fixtures


class LogMappingTests(unittest.TestCase):
    def test_events_keep_only_operational_fields_and_do_not_invent_level(self):
        mapping = LogMapping(event_only=True, event_allowlist=['indexing.job.failed'],
                             detail_fields=['attempt_count', 'error_type', 'status'])
        row = {'event': 'indexing.job.failed', 'attempt_count': 2, 'error_type': 'TimeoutError',
               'status': 'failed', 'message': 'secret business text', 'token': 'sensitive-token',
               'document_id': 'private-document', 'question': 'private question',
               'content': 'private content', 'details': {'password': 'secret'}}
        message, level, code = mapping.normalize(row)
        self.assertEqual(level, 'UNKNOWN')
        self.assertIsNone(code)
        self.assertIn('attempt_count=2', message)
        self.assertIn('error_type=TimeoutError', message)
        for private in ('secret', 'private', 'document_id', 'question', 'content', 'token'):
            self.assertNotIn(private, message)
        self.assertIsNone(mapping.normalize({'message': 'unstructured secret'}))
        self.assertIsNone(mapping.normalize({'event': 'unregistered.event', 'status': 'failed'}))

    def test_registered_fields_support_standard_and_alternate_log_formats(self):
        mapping = LogMapping(message_field='msg', level_field='severity', error_code_field='code')
        self.assertEqual(mapping.normalize({'msg': 'dependency timed out', 'severity': 'warning', 'code': 'DB_TIMEOUT'}),
                         ('dependency timed out', 'WARN', 'DB_TIMEOUT'))
        self.assertIsNone(mapping.normalize({'msg': '', 'document_id': 'metadata-only'}))
        self.assertEqual(LogMapping().normalize({'message': 'normal log', 'level': 'foo'}),
                         ('normal log', 'UNKNOWN', None))

    def test_unsafe_or_unbounded_event_mapping_is_rejected(self):
        for config in ({'detail_fields': ['token']}, {'detail_fields': ['document_id']},
                       {'detail_fields': ['message']}, {'event_only': True}, {'message_field': 'nested.secret'}):
            with self.assertRaises(ValidationError):
                LogMapping.model_validate(config)
        mapping = LogMapping(detail_fields=['duration_ms', 'error_type'])
        message, _, _ = mapping.normalize({'event': 'indexing.job.failed', 'duration_ms': float('inf'),
                                         'error_type': 'secret free text'})
        self.assertEqual(message, 'event=indexing.job.failed')
        message, _, _ = mapping.normalize({'event': 'indexing.job.failed', 'duration_ms': 10**500})
        self.assertEqual(message, 'event=indexing.job.failed')

    def test_json_credentials_urls_and_keys_are_redacted_before_evidence(self):
        text = '''{"token": "hidden-token", "password": "hidden-pass", "api_key": "hidden-key"} postgres://user:hidden-db@localhost sk-hidden-key Bearer hidden-bearer'''
        redacted = safe_text(text)
        self.assertNotIn('hidden', redacted)
        self.assertIn('[REDACTED]', redacted)

    def test_real_executor_retains_time_and_valid_refs_but_excludes_unusable_rows(self):
        fixture = fixtures.RegisteredProviderTests()
        fixture.setUp()
        binding = replace(fixture.binding, observations={**fixture.binding.observations,
            'log_mapping': {'event_only': True, 'event_allowlist': ['indexing.worker.started'],
                            'detail_fields': ['heartbeat_seconds']}})
        records = [{'event': 'indexing.worker.started', 'heartbeat_seconds': 5, 'document_id': 'hidden'},
                   {'message': 'private raw content'}, {'event': 'other.event'}]
        def reply(request):
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'streams', 'result': [
                {'values': [[str(int((fixture.start + timedelta(seconds=n)).timestamp() * 1e9)), json.dumps(row)]
                            for n, row in enumerate(records)]}]}})
        provider = HTTPObservationProvider(binding, fixture.ticket, fixture.principal, transport=httpx.MockTransport(reply))
        executor = ToolExecutor(provider, fixture.principal, fixture.ticket.scope, RunContext())
        result = executor.execute('get_service_logs', {'start': fixture.snapshot['start'],
                                  'end': fixture.snapshot['end'], 'category': 'resource'})
        self.assertEqual(result.status, 'ok')
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].observed_from, fixture.start)
        self.assertEqual(result.evidence[0].payload['level'], 'UNKNOWN')
        self.assertNotIn('private', result.model_dump_json())
        self.assertNotIn('hidden', result.model_dump_json())
        self.assertEqual(provider.last_log_stats['unusable_rows'], 2)
        self.assertTrue(any(r.field_path == 'message' for r in executor.references.values()))
        filtered = executor.execute('get_service_logs', {'start': fixture.snapshot['start'],
                                    'end': fixture.snapshot['end'], 'category': 'resource', 'level': 'ERROR'})
        self.assertEqual(filtered.status, 'empty')
        self.assertEqual(provider.last_log_stats['filtered_rows'], 1)

    def test_truncated_source_and_invalid_mapping_never_appear_complete(self):
        fixture = fixtures.RegisteredProviderTests()
        fixture.setUp()
        def reply(request):
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'streams', 'result': [
                {'values': [[str(int(fixture.start.timestamp() * 1e9)), '{"message":"' + 'x' * 1900 + '"}']]}]}})
        provider = HTTPObservationProvider(fixture.binding, fixture.ticket, fixture.principal,
                                           transport=httpx.MockTransport(reply))
        executor = ToolExecutor(provider, fixture.principal, fixture.ticket.scope, RunContext())
        result = executor.execute('get_service_logs', {'start': fixture.snapshot['start'],
                                  'end': fixture.snapshot['end'], 'category': 'resource'})
        self.assertTrue(result.truncated)
        with self.assertRaises(ProviderError) as error:
            HTTPObservationProvider(replace(fixture.binding, observations={'log_mapping': {'detail_fields': ['password']}}),
                                    fixture.ticket, fixture.principal)
        self.assertEqual(error.exception.code, 'LOG_MAPPING_INVALID')
