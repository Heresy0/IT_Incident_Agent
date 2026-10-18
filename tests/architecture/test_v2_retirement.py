"""Protect the retirement boundary and unchanged deterministic helpers."""
import ast
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from api.auth import Principal
from config.incident import IncidentConfig
from incidents.support import IncidentError
from providers.bound import catalog
from storage.runs import MemoryRunStore
from workflow.rollout import RolloutPolicy
from workflow.service import WorkflowService

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'archive/legacy'


class RetirementTests(unittest.TestCase):
    def test_archive_preserves_every_original_file_byte(self):
        manifest = json.loads((ARCHIVE / 'manifest.json').read_text(encoding='utf-8'))
        for row in manifest['items']:
            self.assertEqual(hashlib.sha256((ARCHIVE / row['archive_path']).read_bytes()).hexdigest(), row['sha256'])
            self.assertFalse((ROOT / row['original_path']).exists(), row['original_path'])

    def test_extracted_helper_bodies_are_unchanged(self):
        def definitions(path):
            return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text(encoding='utf-8')).body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for old, new, names in (
            ('app/agents/protocol.py', 'app/runtime/schema.py', ['compact_schema']),
            ('app/agents/investigation.py', 'app/evidence/validation.py', ['ReferenceValidationError', 'validate_output']),
            ('app/evidence/diagnosis.py', 'app/evidence/references.py',
             ['ProtocolError', 'diagnosis_reference_options', 'validate_selected_refs', 'expand_refs']),
        ):
            before, after = definitions(ARCHIVE / old), definitions(ROOT / new)
            for name in names:
                self.assertEqual(before[name], after[name], name)

    def test_default_api_service_is_v2_closed_loop_and_legacy_is_explicitly_retired(self):
        service = WorkflowService('unused', store=MemoryRunStore(), config=IncidentConfig())
        self.addCleanup(service.close)
        spec = service._incident.metadata({'execution_mode': 'scripted_control_only'})['run_spec']
        self.assertEqual(spec['engine_id'], 'autonomous_v2')
        self.assertEqual(spec['execution_variant'], 'closed_loop')
        self.assertTrue(spec['enable_rework'])
        self.assertTrue(spec['enable_challenge'])
        with self.assertRaisesRegex(ValueError, 'Only autonomous_v2'):
            WorkflowService('unused', incident_engine='legacy')

    def test_failed_admission_never_persists_a_run_or_submits_work(self):
        owner = Principal('retirement-test', 'operator', 'operator')
        store = MemoryRunStore()
        service = WorkflowService('unused', store=store, config=IncidentConfig(),
                                  incident_rollout=RolloutPolicy(mode='full'))
        self.addCleanup(service.close)
        incident = service.create_incident(catalog(owner)[0]['fields'], owner)
        with patch.object(service._executor, 'submit') as submit:
            with self.assertRaisesRegex(IncidentError, 'DIAGNOSIS_NOT_ADMITTED'):
                service.start_diagnosis(incident['id'], {'request_key': 'hold',
                    'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}, owner)
        submit.assert_not_called()
        self.assertFalse(store.runs)

    def test_cli_uses_same_closed_loop_with_zero_paid_calls(self):
        from scripts.investigate_incident import main
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--fake', '--case', 'case_001', '--output', folder]), 0)
            report = json.loads(next(Path(folder).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(report['engine_id'], 'autonomous_v2')
        self.assertEqual(report['execution_variant'], 'closed_loop')
        self.assertEqual(report['execution_mode'], 'scripted_control_only')
        self.assertTrue(report['semantic_review']['challenge_enabled'])

    def test_fresh_runtime_imports_work_with_all_retired_modules_blocked(self):
        manifest = json.loads((ARCHIVE / 'manifest.json').read_text(encoding='utf-8'))
        retired = [row['original_path'][4:-3].replace('/', '.') for row in manifest['items']
                   if row['original_path'].startswith('app/')]
        code = f'''
import builtins
original = builtins.__import__
retired = {retired!r}
def guarded(name, *args, **kwargs):
    if any(name == root or name.startswith(root + '.') for root in retired):
        raise AssertionError('Retired runtime import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from app_main import create_app
from workflow.v2.engine import investigate
from workflow.service import WorkflowService
print('v2 runtime imports are independent')
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('independent', result.stdout)
