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
    def test_residual_archive_preserves_original_bytes_and_removed_module_boundary(self):
        archive = ARCHIVE / 'residual-20261019'
        manifest = json.loads((archive / 'manifest.json').read_text(encoding='utf-8'))
        for row in manifest['items']:
            self.assertEqual(hashlib.sha256((archive / row['archive_path']).read_bytes()).hexdigest(), row['sha256'])
            if row['action'] == 'archive_module':
                self.assertFalse((ROOT / row['original_path']).exists(), row['original_path'])

    def test_residual_cleanup_preserves_used_helpers_and_correction_allowances(self):
        archive = ARCHIVE / 'residual-20261019'
        for relative, names in (
            ('app/evidence/model_view.py', ['substantive', 'evidence_view']),
            ('app/evidence/ownership.py', ['observed_owner_team']),
            ('app/evidence/observations.py', ['source_statement']),
            ('app/workflow/task_checks.py', ['completion']),
            ('app/tools/executor.py', ['execute', 'validate_args', 'schemas', 'capabilities',
                                       'prepare_snapshot', 'register_snapshot']),
            ('app/runtime/context.py', ['reserve', 'add_usage', 'error', 'summary', 'span', 'invoke_chat_model']),
        ):
            def functions(path):
                return {n.name: ast.dump(n, include_attributes=False) for n in ast.walk(
                    ast.parse(path.read_text(encoding='utf-8'))) if isinstance(n, ast.FunctionDef)}
            before, after = functions(archive / relative), functions(ROOT / relative)
            # Checkpoint recovery intentionally wraps reservations and uses a
            # restored retrieval timestamp/elapsed clock. Compare the original
            # non-durable branches after removing only those explicit adapters;
            # User-source adapters intentionally label unverified submissions.
            # Strip only their exact source-type branches; retain the original
            # non-user evidence view and exact protection for all other code.
            class WithoutRecoveryAdapters(ast.NodeTransformer):
                def visit_If(self, node):
                    for expression in ("item.payload.get('source_type') == 'user_submission'",
                                       "payload.get('source_type') == 'user_submission'"):
                        if ast.dump(node.test, include_attributes=False) == ast.dump(ast.parse(expression, mode='eval').body, include_attributes=False):
                            return None
                    return self.generic_visit(node)

                def visit_FunctionDef(self, node):
                    node = self.generic_visit(node)
                    if (node.name == 'evidence_view' and len(node.body) == 2
                            and isinstance(node.body[0], ast.Assign)
                            and isinstance(node.body[0].value, ast.Dict)
                            and isinstance(node.body[1], ast.Return)
                            and isinstance(node.body[1].value, ast.Name) and node.body[1].value.id == 'view'):
                        node.body = [ast.Return(value=node.body[0].value)]
                    return node

                def visit_Call(self, node):
                    if (isinstance(node.func, ast.Attribute) and node.func.attr == 'elapsed'
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'):
                        return ast.parse('time.monotonic() - self.started', mode='eval').body
                    return self.generic_visit(node)

                def visit_IfExp(self, node):
                    original_clock = ast.parse('datetime.now(timezone.utc)', mode='eval').body
                    if ast.dump(node.orelse, include_attributes=False) == ast.dump(original_clock, include_attributes=False):
                        return node.orelse
                    return self.generic_visit(node)
            current = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
            normalized = WithoutRecoveryAdapters().visit(current)
            normalized_functions = {node.name: node for node in ast.walk(normalized) if isinstance(node, ast.FunctionDef)}
            for name in ('prepare_snapshot', 'summary', 'evidence_view', 'source_statement'):
                if name in names:
                    after[name] = ast.dump(normalized_functions[name], include_attributes=False)
            if 'reserve' in names:
                memory_reserve = normalized_functions['_reserve_memory']
                memory_reserve.name = 'reserve'
                after['reserve'] = ast.dump(memory_reserve, include_attributes=False)
            for name in names:
                self.assertEqual(before[name], after[name], f'{relative}:{name}')
        from runtime.context import RunContext
        old = ast.parse((archive / 'app/runtime/context.py').read_text(encoding='utf-8'))
        original = next(n for n in ast.walk(old) if isinstance(n, ast.FunctionDef) and n.name == 'claim_repair')
        namespace = {}
        exec(compile(ast.Module(body=[original], type_ignores=[]), '<original-correction>', 'exec'), namespace)
        context = RunContext()
        for repairs in range(5):
            for reviewed in range(repairs + 1):
                for reviewer in (False, True):
                    before = {'repairs': repairs, 'reviewer_repairs': reviewed}
                    after = dict(before)
                    expected = namespace['claim_repair'](context, before, reviewer=reviewer)
                    self.assertEqual(context.claim_repair(after, reviewer=reviewer), expected)
                    self.assertEqual(after, before)

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
        residual = json.loads((ARCHIVE / 'residual-20261019/manifest.json').read_text(encoding='utf-8'))
        manifest['items'].extend(row for row in residual['items'] if row['action'] == 'archive_module')
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
