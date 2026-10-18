"""Acceptance uses real application endpoints without fabricating model success."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.demo_frontend_acceptance import main, load_replays, seed_replays, validate_dsn, PRESERVED_FIELDS, PARTIAL_IDS, ROOT, sha, write
from api.auth import Principal
from storage.runs import MemoryRunStore


class FrontendAcceptanceGuards(unittest.TestCase):
    def setUp(self):
        # Deliberately synthetic unit inputs; no ignored study output or paid call.
        (ROOT / 'output').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / 'output')
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        entries, resources = [], []
        from uuid import uuid4
        for index, rid in enumerate(('unit_completed', *PARTIAL_IDS)):
            status = 'completed' if index == 0 else 'partial'
            native = {'run_id': str(uuid4()), 'status': status,
                'final': 'Explicit synthetic unit fixture; not model-quality evidence.',
                'evidence': [{'evidence_id': 'E1', 'kind': 'observation', 'payload': {'unit_fixture': True}}],
                'events': [{'type': 'unit_fixture', 'seq': 1}], 'output': {'hypotheses': []},
                'run_summary': {'model_calls': 0}}
            raw = {'report_id': rid, 'case_id': f'unit_{index}', 'stage': 'e2e', 'repeat': 1,
                'native_report': native, 'model_call_log': [{'model': 'qwen3.7-flash', 'unit_fixture': True}],
                'audit_ticket_question': {'symptoms': 'Synthetic unit fixture', 'scope': {
                    'service': 'unit_component', 'environment': 'staging', 'service_version': 'unit-v1',
                    'start': '2026-10-18T00:00:00Z', 'end': '2026-10-18T00:01:00Z'}}}
            path = self.folder / (rid + '.json')
            write(path, raw)
            entries.append({'report_id': rid, 'report_file': path.name, 'report_sha256': sha(path), 'stage': 'e2e'})
            resources.append({'report_id': rid, 'stage': 'e2e', 'delivery_status': status})
        self.bundle = self.folder / 'bundle.json'
        self.scorecard = self.folder / 'scores.json'
        write(self.bundle, {'execution_mode': 'live_observation_only', 'unit_fixture': True, 'runs': entries})
        write(self.scorecard, {'model': 'qwen3.7-flash', 'unit_fixture': True,
            'source_bindings': {'bundle': {'sha256': sha(self.bundle)}}, 'resource_run_metrics': resources})

    def records(self):
        return load_replays(self.bundle, self.scorecard)

    def test_plan_does_not_read_reports_or_open_store_or_serve(self):
        with patch('scripts.demo_frontend_acceptance.load_replays') as load, \
                patch('storage.runs.PostgresRunStore') as store, patch('uvicorn.run') as serve, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([]), 0)
        self.assertTrue(json.loads(out.getvalue())['plan_only'])
        load.assert_not_called(); store.assert_not_called(); serve.assert_not_called()

    def test_dsn_rejects_daily_remote_and_invalid_without_exposing_secrets(self):
        for dsn in ['postgresql://u:secret@127.0.0.1/daily',
                    'postgresql://u:secret@remote.example/demo_test', 'invalid-secret']:
            with self.subTest(dsn=dsn), self.assertRaises(ValueError) as err:
                validate_dsn(dsn)
            self.assertNotIn('secret', str(err.exception))
        validate_dsn('postgresql://u:p@127.0.0.1:55435/acceptance_test')

    def test_serve_refuses_daily_database_before_any_pool(self):
        with patch.dict(os.environ, {'FRONTEND_ACCEPTANCE_DSN': 'postgresql://u:secret@127.0.0.1/daily'}), \
                patch('storage.runs.PostgresRunStore') as store, redirect_stderr(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                main(['--serve'])
        store.assert_not_called()
        self.assertNotIn('secret', out.getvalue())

    def test_import_preserves_partial_and_completed_content_and_one_terminal_event(self):
        principal = Principal('frontend_acceptance', 'operator', 'operator')
        store = MemoryRunStore()
        records = self.records()
        before = deepcopy(records)
        imported = seed_replays(store, principal, records)
        self.assertEqual([r['status'] for r in imported], ['completed', 'partial', 'partial', 'partial'])
        self.assertEqual(records, before)
        for record, entry in zip(records, imported):
            run = store.get(entry['run_id'], principal)
            for key in PRESERVED_FIELDS:
                self.assertEqual(run['result'].get(key), record['report']['native_report'].get(key), key)
            self.assertEqual(run['result']['execution_mode'], 'recorded_live_replay')
            self.assertFalse(run['result']['frontend_acceptance_replay']['new_diagnosis'])
            events = store.events(entry['run_id'], limit=1000)
            self.assertEqual(sum(e['type'] == 'final' for e in events), 1)
            self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
            self.assertIsNone(store.get(entry['run_id'], Principal('other', 'operator')))

    def test_report_tamper_fails_before_import(self):
        (self.folder / 'unit_completed.json').write_text('{}', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.records()

    def test_restart_reuses_identical_frozen_replays_without_duplicate_events(self):
        store = MemoryRunStore()
        principal = Principal('frontend_acceptance', 'operator', 'operator')
        records = self.records()
        first = seed_replays(store, principal, records)
        history = deepcopy(store.history)
        self.assertEqual(seed_replays(store, principal, records), first)
        self.assertEqual(len(store.runs), 4)
        self.assertEqual(store.history, history)
        store.runs[first[0]['run_id']]['result']['status'] = 'partial'
        with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
            seed_replays(store, principal, records)

    def test_http_checker_rejects_remote_origins_before_transmitting_token(self):
        from scripts.check_frontend_acceptance import check_http
        for origin in ['https://remote.example', 'http://127.0.0.1.evil.example', 'http://user:pass@127.0.0.1',
                       'http://127.0.0.1/path', 'http://127.0.0.1?token=bad']:
            with self.subTest(origin=origin), patch('httpx.Client') as client:
                with self.assertRaises(ValueError):
                    check_http(origin)
                client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
