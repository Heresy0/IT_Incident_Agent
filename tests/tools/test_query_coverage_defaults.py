"""Exact attempted reads use contract defaults, while incomplete supersets do not cover."""
import json
from datetime import datetime, timedelta, timezone
import unittest

from evidence.contracts import TOOL_ARGS
from tools.query_coverage import matching_reads


class QueryCoverageDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 17, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(minutes=20)
        self.window = {'start': self.start.isoformat(), 'end': self.end.isoformat()}

    def ledger(self, tool, raw, *, status='error', truncated=False):
        canonical = TOOL_ARGS[tool].model_validate_json(json.dumps(raw)).model_dump(mode='json')
        return {('read', 1): {'tool': tool, 'args': canonical, 'status': status,
                            'truncated': truncated, 'error': 'UPSTREAM_TIMEOUT' if status == 'error' else None,
                            'evidence_ids': []}}

    def assert_exact_attempt_matches_and_narrow_does_not(self, tool, raw):
        for status, truncated in (('error', False), ('ok', True)):
            with self.subTest(tool=tool, status=status, truncated=truncated):
                results = self.ledger(tool, raw, status=status, truncated=truncated)
                row = next(iter(results.values()))
                self.assertNotEqual(raw['start'], row['args']['start'])
                self.assertTrue(raw['start'].endswith('+00:00'))
                self.assertTrue(row['args']['start'].endswith('Z'))
                self.assertEqual(matching_reads(tool, raw, results), [row])
                self.assertEqual(matching_reads(tool, row['args'], results), [row])
                narrow = {**raw, 'start': (self.start + timedelta(minutes=10)).isoformat()}
                self.assertEqual(matching_reads(tool, narrow, results), [])

    def test_recent_changes_defaults_and_equivalent_utc_rendering_preserve_attempt(self):
        raw = dict(self.window)
        self.assert_exact_attempt_matches_and_narrow_does_not('get_recent_changes', raw)
        results = self.ledger('get_recent_changes', raw)
        self.assertEqual(matching_reads('get_recent_changes', {**raw, 'category': None}, results),
                         list(results.values()))

    def test_logs_omitted_level_and_error_code_preserve_attempt(self):
        raw = {**self.window, 'category': 'dependency'}
        self.assert_exact_attempt_matches_and_narrow_does_not('get_service_logs', raw)
        results = self.ledger('get_service_logs', raw)
        explicit = {**raw, 'level': None, 'error_code': None}
        self.assertEqual(matching_reads('get_service_logs', explicit, results), list(results.values()))
        # An unfiltered failed attempt cannot stand in for a new restricted read.
        self.assertEqual(matching_reads('get_service_logs', {**raw, 'level': 'ERROR'}, results), [])

    def test_metrics_default_granularity_and_canonical_names_preserve_attempt(self):
        raw = {**self.window, 'metrics': ['request_latency', 'dependency_error_rate', 'request_latency']}
        self.assert_exact_attempt_matches_and_narrow_does_not('get_service_metrics', raw)
        results = self.ledger('get_service_metrics', raw)
        explicit = {**raw, 'granularity': '1m', 'metrics': ['dependency_error_rate', 'request_latency']}
        self.assertEqual(matching_reads('get_service_metrics', explicit, results), list(results.values()))
        # Reading a different sample interval remains a genuinely different query.
        self.assertEqual(matching_reads('get_service_metrics', {**explicit, 'granularity': '5m'}, results), [])

    def test_complete_superset_still_covers_a_new_narrow_window(self):
        for tool, params in (('get_recent_changes', {}),
                             ('get_service_logs', {'category': 'dependency'}),
                             ('get_service_metrics', {'metrics': ['request_latency']})):
            with self.subTest(tool=tool):
                raw = {**self.window, **params}
                results = self.ledger(tool, raw, status='empty')
                narrow = {**raw, 'start': (self.start + timedelta(minutes=10)).isoformat()}
                self.assertEqual(matching_reads(tool, narrow, results), list(results.values()))

    def test_malformed_or_unknown_requests_do_not_match_unrelated_attempts(self):
        raw = dict(self.window)
        for status in ('error', 'empty'):
            results = self.ledger('get_recent_changes', raw, status=status)
            for tool, args in (('get_recent_changes', {'start': 'invalid', 'end': 'invalid'}),
                               ('unknown_tool', raw),
                               ('get_service_logs', {**raw, 'category': 'unknown'})):
                with self.subTest(status=status, tool=tool, args=args):
                    self.assertEqual(matching_reads(tool, args, results), [])


if __name__ == '__main__':
    unittest.main()
