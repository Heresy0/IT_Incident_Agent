"""Free controls for paired native-review diagnostics and non-gold reporting."""
from copy import deepcopy
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
from evals.rca_contracts import canonical_sha, read_json, sha_file
from scripts.evaluate_rca_integration import (TASK_FIELDS, MemoryPaidBudget, RcaBroker, acquired_content_sha,
    acquired_pack_sha, application_freeze, load_tasks, make_plan, make_schedule,
    run_collection, task_spec, verify_inputs, write_json)
from scripts.summarize_rca_integration import (build_summary, citation_observation,
    load_bundle, reconcile_accounting)


def task_document(directory, *, wording=False):
    public_path = ROOT / 'evals/datasets/rca_v1/public/rca_candidate_001.json'
    diagnostic = read_json(ROOT / 'evals/datasets/rca_v1/diagnostics/rca_candidate_001.json')
    control = diagnostic['fixed_controls'][0]
    base = {'task_id': 'full', 'group_id': 'refs', 'case_id': 'rca_candidate_001',
        'comparison_kind': 'reference_ablation', 'condition': 'full_refs',
        'public_path': str(public_path), 'source_queries': diagnostic['source_queries'],
        'statement': control['statement'], 'level': control['level'], 'selectors': control['selectors']}
    rows = [base, {**deepcopy(base), 'task_id': 'none', 'condition': 'no_refs', 'selectors': []},
        {**deepcopy(base), 'task_id': 'wrong', 'condition': 'wrong_field', 'selectors': [['row_01', 'error_code']]}]
    if wording:
        base = {**deepcopy(base), 'task_id': 'old', 'group_id': 'wording',
            'comparison_kind': 'wording_pair', 'condition': 'wording_v1',
            'statement': diagnostic['fixed_controls'][1]['statement']}
        rows += [base, {**deepcopy(base), 'task_id': 'new', 'condition': 'wording_v2',
                       'statement': '该故障观测时间窗内，远端数据库进程发生崩溃。'}]
    path = Path(directory) / 'tasks.json'
    write_json(path, {'schema_version': 'rca-integration-tasks-v1', 'tasks': rows})
    return path


def arguments(directory, tasks, **updates):
    return SimpleNamespace(**{'tasks': tasks, 'application_root': ROOT, 'repeats': 3,
        'output': Path(directory), 'live': False, 'prespecified_plan': None, **updates})


class RcaIntegrationTests(unittest.TestCase):
    def test_public_task_boundary_rejects_private_labels_and_changed_pair_inputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = task_document(directory)
            document = read_json(path)
            document['tasks'][0]['expected_relation'] = 'supported'
            write_json(path, document)
            with self.assertRaisesRegex(ValueError, 'ALLOWLIST'):
                load_tasks(path)
            path = task_document(directory)
            document = read_json(path)
            document['tasks'][1]['source_queries'] = document['tasks'][1]['source_queries'][:1]
            write_json(path, document)
            with self.assertRaisesRegex(ValueError, 'PAIRED_PUBLIC_INPUTS_DIFFER'):
                load_tasks(path)

    def test_wrong_field_keeps_same_source_rows_and_no_refs_is_empty(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = task_document(directory)
            document = read_json(path)
            document['tasks'][2]['selectors'] = [['row_02', 'error_code']]
            write_json(path, document)
            with self.assertRaisesRegex(ValueError, 'WRONG_FIELD_MUST_KEEP_SOURCE_ROWS'):
                load_tasks(path)

    def test_complete_cells_rotated_three_times_without_treating_repeats_as_cases(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            data = load_tasks(task_document(directory, wording=True))
            schedule = list(make_schedule(data['tasks'], 3))
            self.assertEqual(len(schedule), 15)
            self.assertEqual({r['repeat'] for r in schedule}, {1, 2, 3})
            first = [next(r['condition'] for r in schedule if r['repeat'] == n and r['group_id'] == 'refs') for n in (1, 2, 3)]
            self.assertEqual(len(set(first)), 3)
            spec = task_spec(data['tasks'][0])
            self.assertEqual(set(spec), {'world', 'capability_metadata', 'initial_checks', 'review_controls'})
            self.assertNotIn('truth', json.dumps(spec))

    def test_plan_is_free_frozen_and_never_promotes_native_verdicts_to_gold(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            args = arguments(directory, task_document(directory))
            data = load_tasks(args.tasks)
            plan = make_plan(args, data, 'provider-hash')
            self.assertEqual(plan['planned_runs'], 9)
            self.assertEqual(plan['maximum_model_calls'], 144)
            self.assertEqual(plan['limits']['model_calls'], 16)
            self.assertEqual(plan['limits']['tool_calls'], 8)
            self.assertEqual(plan['limits']['seconds'], 180)
            self.assertIsNone(plan['formal_metrics'])
            self.assertFalse(plan['human_verified'])
            write_json(Path(directory) / 'plan.json', plan)
            args.prespecified_plan = Path(directory) / 'plan.json'
            self.assertEqual(make_plan(args, data, 'provider-hash'), plan)
            changed = deepcopy(plan)
            changed['model_settings']['temperature'] = 1
            write_json(args.prespecified_plan, changed)
            with self.assertRaisesRegex(ValueError, 'FROZEN_PLAN'):
                make_plan(args, data, 'provider-hash')
            with self.assertRaisesRegex(ValueError, 'DECLARATION'):
                verify_inputs(plan, args.tasks, args.prespecified_plan)

    def test_content_identity_excludes_retrieval_time_but_keeps_query_quality_separate(self):
        report = {'native_report': {'evidence': [{'evidence_id': 'x', 'payload': {'a': 1}, 'retrieved_at': 'first'}],
                                    'collection': [{'status': 'ok'}]}}
        altered = deepcopy(report)
        altered['native_report']['evidence'][0]['retrieved_at'] = 'later'
        self.assertEqual(acquired_content_sha(report), acquired_content_sha(altered))
        self.assertEqual(acquired_pack_sha(report), acquired_pack_sha(altered))
        altered['native_report']['collection'][0]['status'] = 'truncated'
        self.assertEqual(acquired_content_sha(report), acquired_content_sha(altered))
        self.assertNotEqual(acquired_pack_sha(report), acquired_pack_sha(altered))
        altered['native_report']['evidence'][0]['payload']['a'] = 2
        self.assertNotEqual(acquired_content_sha(report), acquired_content_sha(altered))

    def test_real_native_workers_keep_same_pack_across_all_reference_variants(self):
        from workflow.v2.scripted import scripted_model
        from scripts.evaluate_rca import execute_worker
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            data = load_tasks(task_document(directory))
            args = arguments(directory, Path(data['path']))
            plan = make_plan(args, data, 'provider-hash')
            reports = []
            entries = []
            for index, task in enumerate(data['tasks']):
                broker = RcaBroker(scripted_model(None), None, 'scripted-wiring')
                report = execute_worker(task_spec(task), application_freeze(ROOT), 'fixed_review', broker)
                self.assertEqual(report['native_report']['status'], 'fixed_review_completed')
                self.assertEqual(report['run_summary']['limits']['model_calls'], 16)
                report.update(plan['schedule'][index], report_id='free_' + task['task_id'],
                    model_call_log=broker.calls, application=plan['application'],
                    model_settings=plan['model_settings'], provider_configuration_sha256='provider-hash',
                    execution_mode='scripted_control_only', public_freeze=plan['public_freezes'][task['task_id']],
                    task={key: deepcopy(task[key]) for key in TASK_FIELDS}, decision_trace=[])
                report['acquired_content_sha256'] = acquired_content_sha(report)
                report['acquired_pack_sha256'] = acquired_pack_sha(report)
                name = report['report_id'] + '.json'
                write_json(Path(directory) / name, report)
                entries.append({**plan['schedule'][index], 'report_id': report['report_id'],
                    'report_file': name, 'report_sha256': sha_file(Path(directory) / name)})
                reports.append(report)
            self.assertEqual(len({acquired_pack_sha(r) for r in reports}), 1)
            self.assertTrue(all(citation_observation(r)['mechanical_author_reference_values_valid'] for r in reports))
            empty = citation_observation(reports[1])
            self.assertEqual(empty['author_selected_fields'], [])
            self.assertTrue(empty['acquired_sources_without_claim_reference'])
            # Reviewer has all sources even when the author selected none.
            self.assertEqual(len(empty['acquired_sources']), len(citation_observation(reports[0])['acquired_sources']))
            bundle = {'schema_version': 'rca-integration-bundle-v1', 'plan': plan, 'runs': entries,
                'scheduled_pending': [{**e, 'reason': 'free_test_only'} for e in plan['schedule'][3:]],
                'paid_budget': None, 'execution_mode': 'scripted_control_only'}
            write_json(Path(directory) / 'bundle.json', bundle)
            actual_bundle, actual_reports = load_bundle(Path(directory) / 'bundle.json')
            summary = build_summary(actual_bundle, actual_reports)
            self.assertTrue(summary['paired_groups'][0]['paired_inputs_confirmed'])
            self.assertIsNone(summary['formal_metrics'])
            self.assertIsNone(summary['semantic_error_rate'])
            self.assertEqual(summary['accounting']['status'], 'scripted_no_paid_ledger')

    def test_harness_failure_and_fatal_admission_preserve_every_unexecuted_cell(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            args = arguments(directory, task_document(directory), live=True)
            data = load_tasks(args.tasks)
            plan = make_plan(args, data, 'provider-hash')
            path = Path(directory) / 'ledger.json'
            write_json(path, {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'model_seconds': 0,
                'unknown_usage_calls': 0, 'max_requests': 144, 'max_input_tokens': 1000000,
                'max_output_tokens': 1000000, 'max_model_seconds': 1000})
            budget = MemoryPaidBudget(path, 144)
            def fail(*args, **kwargs):
                budget.state.update(admission_closed=True, admission_close_reason='AUTH_ERROR')
                raise RuntimeError('free failure')
            with patch('scripts.evaluate_rca_integration.verify_inputs', return_value=data), \
                 patch('scripts.evaluate_rca_integration.execute_worker', side_effect=fail):
                output = run_collection(args, plan, data, model=object(), budget=budget)
            bundle, reports = load_bundle(output / 'bundle.json')
            self.assertEqual(len(reports), 1)
            self.assertEqual(len(bundle['scheduled_pending']), 8)
            summary = build_summary(bundle, reports)
            self.assertIsNone(summary['formal_metrics'])
            self.assertIsNone(summary['semantic_error_rate'])
            self.assertFalse(summary['paired_groups'][0]['paired_inputs_confirmed'])
            tampered = read_json(output / bundle['runs'][0]['report_file'])
            tampered['condition'] = 'changed'
            write_json(output / bundle['runs'][0]['report_file'], tampered)
            with self.assertRaisesRegex(ValueError, 'HASH'):
                load_bundle(output / 'bundle.json')

    def test_known_usage_settles_exactly_and_never_invents_bill_for_unknown_usage(self):
        class FreeModel:
            json_output = True
            def invoke(self, messages):
                return AIMessage(content='{"items":[]}', usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10})
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            write_json(path, {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'model_seconds': 0,
                'unknown_usage_calls': 0, 'max_requests': 10, 'max_input_tokens': 1000000,
                'max_output_tokens': 1000000, 'max_model_seconds': 1000})
            budget = MemoryPaidBudget(path, 2)
            broker = RcaBroker(FreeModel(), budget, 'qwen3.7-flash', run_identity={'task_id': 'free'})
            message = HumanMessage(content='{"stage":"reviewer"}')
            broker.handle({'request_id': 1, 'role': 'reviewer', 'tools': [],
                'messages': [{'type': message.type, 'data': message.model_dump(mode='json')}]})
            report = {'model_call_log': broker.calls}
            result = reconcile_accounting({'paid_budget': budget.summary()}, [report])
            self.assertEqual(result['known_usage_calls'], 1)
            self.assertEqual(result['status'], 'reconciled')
            bad = deepcopy(report)
            bad['model_call_log'][0]['usage']['input_tokens'] = 8
            with self.assertRaisesRegex(ValueError, 'KNOWN_USAGE'):
                reconcile_accounting({'paid_budget': budget.summary()}, [bad])


if __name__ == '__main__':
    unittest.main()
