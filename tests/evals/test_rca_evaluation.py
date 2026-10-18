"""Free RCA wiring, isolation, admission and missing-run accounting checks."""
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from langchain_core.messages import AIMessage, HumanMessage
from evals.rca_contracts import SCHEMA_VERSION, canonical_sha
from scripts.evaluate_rca import (RUN_LIMITS, STAGES, MemoryPaidBudget, RcaBroker,
    application_freeze, case_freeze, execute_worker, make_plan, parse_args,
    run_collection, schedule, worker_case, validate_worker_world)


def public_case():
    from evals.budget_matrix_cases import build_case_specs
    spec = build_case_specs(ROOT)['world_followup_available']
    public = {'schema_version': SCHEMA_VERSION, 'world': deepcopy(spec['world']),
              'capability_metadata': deepcopy(spec['capability_metadata'])}
    controls = [{'control_id': 'diagnostic-one', 'statement': row['statement'],
        'level': row.get('level', 'mechanism'), 'selectors': row['selectors']}
        for row in spec['review_controls'] if row['kind'] == 'factual_relation'][:1]
    diagnostics = {'schema_version': SCHEMA_VERSION,
        'source_queries': deepcopy(spec['initial_checks']), 'fixed_controls': controls}
    return {'case_id': 'free-case', 'split': 'calibration', 'independence_group': 'one',
        'public': public, 'public_diagnostics': diagnostics,
        'truth': {'SECRET_PRIVATE_ANSWER': 'NEVER_SEND'},
        'public_sha256': canonical_sha(public), 'truth_sha256': 'private-file-sha',
        'diagnostics_sha256': canonical_sha(diagnostics)}


def arguments(directory, **changes):
    values = {'application_root': ROOT, 'dataset': ROOT / 'not-read.json',
        'split': 'calibration', 'cases': None, 'stages': ['e2e'], 'repeats': 1,
        'system_id': 'baseline', 'model': 'qwen3.7-flash', 'jobs': 1,
        'prespecified_plan': None, 'live': False, 'output': Path(directory)}
    return SimpleNamespace(**{**values, **changes})


def dataset(case):
    return {'cases': [case], 'fingerprint': 'free-dataset', 'manifest_path': 'not-read'}


def free_plan(args, data):
    with patch('scripts.evaluate_rca.dataset_freeze', return_value={'files_sha256': {}}):
        return make_plan(args, data, 'public-provider-sha')


def ledger(path):
    path.write_text(json.dumps({'requests': 0, 'input_tokens': 0, 'output_tokens': 0,
        'model_seconds': 0, 'unknown_usage_calls': 0, 'max_requests': 10,
        'max_input_tokens': 1000000, 'max_output_tokens': 1000000,
        'max_model_seconds': 1000}), encoding='utf-8')


def model_request():
    message = HumanMessage(content=json.dumps({'stage': 'investigate', 'input': {'feedback': None}}))
    return {'request_id': 1, 'role': 'investigation', 'tools': [],
        'messages': [{'type': message.type, 'data': message.model_dump(mode='json')}]}


class RcaEvaluationTests(unittest.TestCase):
    def test_e2e_worker_boundary_allowlists_only_public_sources(self):
        spec = worker_case(public_case(), 'e2e')
        self.assertEqual(set(spec), {'world', 'capability_metadata'})
        self.assertNotIn('NEVER_SEND', json.dumps(spec))
        self.assertNotIn('initial_checks', spec)
        self.assertNotIn('review_controls', spec)

    def test_hidden_fields_in_public_or_diagnostic_inputs_are_rejected(self):
        case = public_case()
        case['public']['world']['truth'] = 'leak'
        with self.assertRaises(ValueError):
            worker_case(case, 'e2e')
        case = public_case()
        case['public_diagnostics']['fixed_controls'][0]['expected_relation'] = 'supported'
        with self.assertRaises(ValueError):
            worker_case(case, 'fixed_review', 'diagnostic-one')

    def test_observed_worker_world_and_capability_are_parent_verified(self):
        spec = worker_case(public_case(), 'e2e')
        result = {'observed_world_sha256': canonical_sha(spec['world']),
                  'observed_capability_sha256': canonical_sha(spec['capability_metadata'])}
        self.assertTrue(validate_worker_world(result, spec))
        for field in ('observed_world_sha256', 'observed_capability_sha256'):
            with self.assertRaisesRegex(ValueError, 'Worker observed public input mismatch'):
                validate_worker_world({**result, field: 'tampered'}, spec)
            missing = deepcopy(result)
            missing.pop(field)
            with self.assertRaises(ValueError):
                validate_worker_world(missing, spec)

    def test_diagnostic_inputs_are_public_only_and_one_original_control(self):
        case = public_case()
        spec = worker_case(case, 'fixed_review', 'diagnostic-one')
        self.assertEqual(spec['initial_checks'], case['public_diagnostics']['source_queries'])
        self.assertEqual(len(spec['review_controls']), 1)
        self.assertEqual(spec['review_controls'][0]['target_id'], 'C1')
        self.assertEqual(spec['review_controls'][0]['original_text'],
                         case['public_diagnostics']['fixed_controls'][0]['statement'])
        with self.assertRaises(ValueError):
            worker_case(case, 'fixed_review', 'missing')

    def test_schedule_keeps_stages_repeats_and_control_identity(self):
        case = public_case()
        rows = list(schedule([case], STAGES, 3, 'candidate_version'))
        self.assertEqual(len(rows), 9)
        self.assertEqual({row['repeat'] for row in rows}, {1, 2, 3})
        self.assertEqual({row['system_id'] for row in rows}, {'candidate_version'})
        self.assertEqual([row['control_id'] for row in rows if row['stage'] == 'fixed_review'],
                         ['diagnostic-one'] * 3)

    def test_live_requires_plan_ledger_cap_and_explicit_pending_collection(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            parse_args(['--live', '--application-root', str(ROOT)])
        with redirect_stderr(errors), self.assertRaises(SystemExit):
            parse_args(['--live', '--application-root', str(ROOT), '--prespecified-plan', 'p',
                        '--budget-ledger', 'l', '--max-model-calls', '16'])
        _, args = parse_args(['--live', '--collect-pending', '--application-root', str(ROOT),
            '--prespecified-plan', 'p', '--budget-ledger', 'l', '--max-model-calls', '16'])
        self.assertTrue(args.collect_pending)

    def test_plan_is_non_gold_and_has_no_budget_adapter(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            plan = free_plan(arguments(directory), dataset(public_case()))
        self.assertEqual(plan['limits']['model_calls'], 16)
        self.assertEqual(plan['limits']['tool_calls'], 8)
        self.assertEqual(plan['native_arm'], 'current')
        self.assertIsNone(plan['runtime_budget_adapter'])
        self.assertIsNone(plan['semantic_accuracy'])
        self.assertFalse(plan['formal_acceptance_eligible'])

    def test_plan_rejects_hash_settings_and_identity_tampering_before_execution(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            args, data = arguments(directory), dataset(public_case())
            plan = free_plan(args, data)
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps(plan), encoding='utf-8')
            args.prespecified_plan = path
            self.assertEqual(free_plan(args, data), plan)
            for field, value in (('system_id', 'different'), ('limits', {}),
                                  ('evaluation_source_sha256', {}), ('application', {})):
                changed = deepcopy(plan)
                changed[field] = value
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.assertRaises(ValueError):
                    free_plan(args, data)

    def test_empty_or_duplicate_case_selection_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            args, data = arguments(directory, cases=['free-case', 'free-case']), dataset(public_case())
            with self.assertRaises(ValueError):
                free_plan(args, data)

    def test_known_usage_and_reservation_identity_are_preserved(self):
        class FreeModel:
            json_output = True
            def invoke(self, messages):
                return AIMessage(content='{"kind":"finish","question":"free"}',
                    usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10})
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path)
            budget = MemoryPaidBudget(path, 2)
            identity = {'case_id': 'x', 'repeat': 2, 'stage': 'e2e', 'system_id': 'baseline'}
            broker = RcaBroker(FreeModel(), budget, 'free', run_identity=identity)
            broker.handle(model_request())
            summary = budget.summary()
            self.assertEqual(summary['charged_delta']['input_tokens'], 7)
            self.assertEqual(summary['pending_reservation_ids'], [])
            self.assertEqual(summary['session_reservation_events'][0]['request_context']['case_id'], 'x')
            self.assertEqual(broker.calls[0]['reservation_id'], 1)

    def test_quota_error_keeps_original_code_unknown_usage_and_closes_admission(self):
        from runtime.context import ExecutionError
        class QuotaModel:
            json_output = True
            def invoke(self, messages):
                raise ExecutionError('QUOTA_EXCEEDED', 'model')
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path)
            budget = MemoryPaidBudget(path, 2)
            broker = RcaBroker(QuotaModel(), budget, 'free')
            response = broker.handle(model_request())
            self.assertEqual(response['error']['code'], 'QUOTA_EXCEEDED')
            self.assertEqual(broker.calls[0]['error']['code'], 'QUOTA_EXCEEDED')
            self.assertTrue(budget.snapshot()['admission_closed'])
            self.assertEqual(budget.snapshot()['admission_close_reason'], 'QUOTA_EXCEEDED')
            self.assertEqual(budget.summary()['unknown_usage_calls_delta'], 1)
            self.assertGreater(budget.summary()['charged_delta']['input_tokens'], 0)
            self.assertEqual(budget.summary()['pending_reservation_ids'], [])

    def test_global_circuit_breaker_preserves_unexecuted_schedule(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            case = public_case()
            args = arguments(directory, repeats=3, live=True)
            data = dataset(case)
            plan = free_plan(args, data)
            path = Path(directory) / 'ledger.json'
            ledger(path)
            budget = MemoryPaidBudget(path, 10)
            def fake_worker(*args, **kwargs):
                budget.state.update(admission_closed=True, admission_close_reason='AUTH_ERROR')
                return {'native_report': {'status': 'partial'}, 'normalized': {}, 'run_summary': {}}
            with patch('scripts.evaluate_rca.verify_inputs', return_value=data), \
                 patch('scripts.evaluate_rca.execute_worker', side_effect=fake_worker):
                output = run_collection(args, plan, data, model=object(), budget=budget)
            bundle = json.loads((output / 'bundle.json').read_text(encoding='utf-8'))
            self.assertEqual(len(bundle['runs']), 1)
            self.assertEqual(len(bundle['scheduled_pending']), 2)
            self.assertEqual({row['reason'] for row in bundle['scheduled_pending']}, {'AUTH_ERROR'})
            self.assertEqual([row['repeat'] for row in bundle['scheduled_pending']], [2, 3])

    def smoke(self, stage):
        from workflow.v2.scripted import scripted_model
        case = public_case()
        spec = worker_case(case, stage, 'diagnostic-one' if stage == 'fixed_review' else None)
        broker = RcaBroker(scripted_model(None), None, 'scripted-free')
        before = application_freeze(ROOT)
        result = execute_worker(spec, before, stage, broker)
        self.assertEqual(application_freeze(ROOT), before)
        self.assertNotIn('execution_error', result['native_report'])
        self.assertIsNone(result['runtime_budget_adapter'])
        self.assertEqual(result['run_summary']['limits'], RUN_LIMITS)
        self.assertEqual(result['run_summary']['model_calls'], len(broker.calls))
        self.assertTrue(all(not row['charged_request'] for row in broker.calls))
        self.assertEqual(result['observed_world_sha256'], canonical_sha(case['public']['world']))
        self.assertTrue(all(Path(path).is_relative_to(ROOT / 'app')
                            for path in result['loaded_application_modules'].values()))
        return result

    def test_native_e2e_scripted_worker_uses_current_without_adapter(self):
        self.smoke('e2e')

    def test_native_sufficient_evidence_skips_collection_and_rework(self):
        result = self.smoke('sufficient_evidence')
        self.assertEqual(result['native_report']['execution_variant'], 'fixed_public_evidence_diagnostic_only')
        self.assertFalse(result['native_report'].get('reworks'))
        self.assertEqual(len(result['native_report']['fixed_prefix']['source_checks']),
                         len(public_case()['public_diagnostics']['source_queries']))

    def test_native_fixed_review_keeps_exact_original_sentence(self):
        result = self.smoke('fixed_review')
        controls = result['normalized']['review_controls']
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]['target_id'], 'C1')
        self.assertIn('primary_review', result['native_report'])


if __name__ == '__main__':
    unittest.main()
