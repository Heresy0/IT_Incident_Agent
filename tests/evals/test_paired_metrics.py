"""Free metric-contract tests. Re-tagged fixtures below are NOT live quality results."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from api.auth import Principal
from config.incident import IncidentConfig
from evals.common import write_json
from evals.independent import suite
from evals.independent.control import smoke_factory
from evals.independent.provider import IndependentProvider
from evals.paired_metrics import (VERSION, arm_order, fingerprint, prepare_experiment,
    qualified_targets, review_entry, summarize_pairs, validate_batch, validate_manifest, markdown, claim_attempt)
from runtime.context import Limits
from scripts.compare_incident import capture
from scripts.compare_independent import main as compare_main
from scripts.evaluate_paired import main


class PairedMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = IncidentConfig(model='unit-model', base_url='https://unit.invalid',
                                    extra_body={'test_marker': 'never-persist-this-value'})
        cls.frozen = prepare_experiment(cls.config, 'unit-paired')
        principal = Principal('synthetic_demo', 'cli_reader')
        cls.originals = {}
        for cid in cls.frozen['protocol']['cases']:
            provider = IndependentProvider(cid, principal)
            for flow in ('single', 'multi'):
                cls.originals[cid, flow] = capture(cid, flow, smoke_factory(provider), principal,
                    Limits(**cls.frozen['limits']), provider_factory=IndependentProvider, investigation_mode='agent')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = deepcopy(self.frozen)

    def bundle(self, cases=None, *, mode='live', model=None, app=None, repeat=1):
        # Synthetic grading fixtures exercise the scorer only; never exported as experiment evidence.
        cases = cases or self.manifest['protocol']['batches'][0]
        directory = self.root / str(uuid4())
        model = model or (self.config.model if mode == 'live' else 'scripted')
        config = {'cases': cases, 'split': 'validation', 'release_holdout': False,
                  'limits': self.manifest['limits'], 'model': model, 'execution_mode': mode,
                  'model_settings': self.manifest['model_settings'],
                  'request_config_sha256': self.manifest['request_config_sha256']}
        prov = {'implementation_sha256_prefix': app or self.manifest['application_version'],
                'dataset_version': self.manifest['dataset_version'],
                'dataset_freeze_sha256': self.manifest['dataset_freeze_sha256'],
                'fixture_sha256': {cid: self.manifest['fixture_sha256'][cid] for cid in cases},
                'evaluation_code_sha256': self.manifest['evaluation_code_sha256'],
                'metrics_experiment': {'experiment_id': self.manifest['experiment_id'],
                    'manifest_sha256': fingerprint(self.manifest), 'repeat_id': repeat, 'order_offset': repeat - 1}}
        bundle = {'configuration': config, 'provenance': prov, 'runs': []}
        for cid in cases:
            for flow in arm_order(cid, repeat - 1):
                report = deepcopy(self.originals[cid, flow])
                report.update(run_id=str(uuid4()), model=model, execution_mode=mode, provenance=prov)
                for event in report['events']:
                    event['run_id'] = report['run_id']
                path = directory / (report['run_id'] + '.json')
                write_json(path, report)
                bundle['runs'].append({'case_id': cid, 'workflow': flow, 'report_file': path.name,
                    'report_sha256': suite.digest(path), 'answer_sha256': suite.digest(suite.SUITE_ROOT / 'answers' / f'{cid}.json')})
        path = directory / 'bundle.json'
        write_json(path, bundle)
        return path

    def full(self):
        return [self.bundle(cases) for cases in self.manifest['protocol']['batches']]

    def approved(self, paths):
        _, worksheet = summarize_pairs(paths, self.manifest)
        for row in worksheet['runs']:
            row.update(approved=True, reviewer='Unit test grading fixture, not a real human study',
                       rationale='Only validate aggregation contracts',
                       scores={k: 2 for k in row['scores']}, unsafe_advice=False, fabricated_evidence=False)
            report = next(json.loads(p.read_text(encoding='utf-8')) for bp in paths for p in bp.parent.glob('*.json')
                          if p.name == row['run_id'] + '.json')
            current = [e['evidence_id'] for e in report['evidence'] if e['kind'] == 'observation']
            for check in row['critical_checks']:
                observed = check['availability'] == 'available' and bool(current)
                check.update(status='unavailable' if check['availability'] == 'unavailable' else 'observed' if observed else 'missed',
                             reason='Unit fixture status', evidence_ids=current[:1] if observed else [])
            for claim in row['claim_assessments']:
                claim.update(supported=True, reason='Unit fixture assertion')
        return worksheet

    def edit_report(self, bp, index, change):
        bundle = suite.read_json(bp)
        entry = bundle['runs'][index]
        path = bp.parent / entry['report_file']
        report = suite.read_json(path)
        change(report)
        write_json(path, report)
        entry['report_sha256'] = suite.digest(path)
        write_json(bp, bundle)

    def test_prepare_is_free_bounded_and_does_not_persist_secrets(self):
        self.assertEqual(self.manifest['planned_runs'], 12)
        self.assertEqual(self.manifest['maximum_model_calls'], 192)
        self.assertEqual(self.manifest['maximum_tool_calls'], 96)
        self.assertEqual(self.manifest['batches'][0]['maximum_model_calls'], 64)
        self.assertEqual(self.manifest['paid_requests_sent'], 0)
        self.assertNotIn('never-persist-this-value', json.dumps(self.manifest))
        self.assertNotIn('https://unit.invalid', json.dumps(self.manifest))
        for value in (0, 3, True):
            with self.assertRaises(ValueError):
                prepare_experiment(self.config, 'unit', repeats=value)

    def test_second_repeat_reverses_order_without_inflating_unique_cases(self):
        frozen = prepare_experiment(self.config, 'unit2', repeats=2)
        self.assertEqual((frozen['planned_runs'], frozen['planned_pairs'], frozen['unique_cases']), (24, 12, 6))
        self.assertEqual(frozen['batches'][0]['arm_order']['eval_001'],
                         list(reversed(frozen['batches'][3]['arm_order']['eval_001'])))

    def test_selected_cases_freeze_only_four_runs_with_unchanged_rules(self):
        frozen = prepare_experiment(self.config, 'subset', cases=['eval_005', 'eval_001'])
        self.assertEqual(frozen['selected_cases'], ['eval_001', 'eval_005'])
        self.assertEqual((frozen['planned_runs'], frozen['planned_pairs'], frozen['unique_cases']), (4, 2, 2))
        self.assertEqual((frozen['maximum_model_calls'], frozen['maximum_tool_calls']), (64, 32))
        self.assertEqual(frozen['protocol'], self.manifest['protocol'])
        self.assertEqual(frozen['limits'], self.manifest['limits'])
        self.assertEqual(frozen['batches'], self.manifest['batches'][:1])
        self.assertEqual(set(frozen['fixture_sha256']), {'eval_001', 'eval_005'})
        self.assertEqual(len(validate_manifest(frozen)), 4)
        for cases in ([], ['eval_001', 'eval_001'], ['eval_999']):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                prepare_experiment(self.config, 'invalid', cases=cases)
        for cases in (['eval_001'], ['eval_001', 'eval_005', 'eval_002'], ['eval_005', 'eval_001']):
            bad = deepcopy(frozen)
            bad['selected_cases'] = cases
            with self.subTest(tampered=cases), self.assertRaises(ValueError):
                validate_manifest(bad)

    def test_subset_summary_denominator_is_two_pairs_and_rejects_unselected_batches(self):
        self.manifest = prepare_experiment(self.config, 'subset-summary', cases=['eval_001', 'eval_005'])
        path = self.bundle()
        result, _ = summarize_pairs([path], self.manifest, self.approved([path]))
        group = result['cohorts'][0]
        self.assertTrue(group['complete'])
        self.assertEqual(group['planned_runs'], 4)
        self.assertEqual(group['missing_slots'], [])
        self.assertEqual(group['workflows']['single']['qualified_reports']['denominator'], 2)
        self.assertIsNotNone(group['workflows']['single']['qualified_reports']['rate'])
        with self.assertRaises(ValueError):
            validate_batch(self.manifest, self.config, ['eval_002', 'eval_003'], self.manifest['limits'])

    def test_live_attempt_receipt_is_exclusive_and_retains_interrupted_directory(self):
        path = self.root / 'experiment.json'
        write_json(path, self.manifest)
        receipt_path, receipt = claim_attempt(path, self.manifest['batches'][0], self.root / 'run')
        self.assertEqual(receipt['status'], 'claimed')
        self.assertTrue(receipt_path.exists())
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            claim_attempt(path, self.manifest['batches'][0], self.root / 'replacement')
        self.assertEqual(suite.read_json(receipt_path)['run_directory'], str((self.root / 'run').resolve()))

    def test_complete_token_differences_and_supported_advice_denominators(self):
        paths = self.full()
        for bp in paths:
            for index in range(4):
                def update(report):
                    multi = report['workflow'] == 'multi'
                    report['run_summary']['token_usage'] = {'status': 'known',
                        'known_calls': report['run_summary']['model_calls'],
                        'input_tokens': 200 if multi else 100, 'output_tokens': 20}
                    report['output']['recommended_actions'] = [{'action': 'read-only human check',
                        'condition': 'verify scope', 'risk': 'read only', 'expected_result': 'more evidence',
                        'requires_approval': False}]
                self.edit_report(bp, index, update)
        sheet = self.approved(paths)
        for row in sheet['runs']:
            if row['case_id'] == 'eval_001':
                next(c for c in row['claim_assessments'] if c['target_id'] == 'A1')['supported'] = False
        result, _ = summarize_pairs(paths, self.manifest, sheet)
        group = result['cohorts'][0]
        self.assertEqual(group['workflows']['single']['advice_supported']['numerator'], 5)
        self.assertEqual(group['workflows']['single']['advice_supported']['denominator'], 6)
        self.assertEqual(group['workflows']['single']['complete_total_tokens'], 720)
        self.assertEqual(group['paired_outcomes'][0]['total_tokens_difference_multi_minus_single'], 100)
        self.assertAlmostEqual(group['comparison']['total_tokens_relative_change_multi_vs_single'], 5 / 6)

    def test_preflight_rejects_config_source_and_limits_drift(self):
        cases = self.manifest['protocol']['batches'][0]
        validate_batch(self.manifest, self.config, cases, self.manifest['limits'])
        for config in (replace(self.config, model='other'), replace(self.config, extra_body={}),
                       replace(self.config, base_url='https://other.invalid')):
            with self.assertRaises(ValueError):
                validate_batch(self.manifest, config, cases, self.manifest['limits'])
        for key in ('application_version', 'dataset_freeze_sha256'):
            bad = deepcopy(self.manifest)
            bad[key] = 'changed'
            with self.assertRaises(ValueError):
                validate_batch(bad, self.config, cases, bad['limits'])
        with self.assertRaises(ValueError):
            validate_batch(self.manifest, self.config, cases, {**self.manifest['limits'], 'tool_calls': 9})

    def test_unreviewed_and_incomplete_are_not_quality_results(self):
        paths = [self.bundle()]
        result, _ = summarize_pairs(paths, self.manifest, self.approved(paths))
        group = result['cohorts'][0]
        self.assertEqual(group['observed_runs'], 4)
        self.assertEqual(len(group['missing_slots']), 8)
        self.assertIsNone(group['workflows']['single']['qualified_reports']['rate'])
        full, _ = summarize_pairs(self.full(), self.manifest)
        self.assertTrue(full['cohorts'][0]['complete'])
        self.assertEqual(full['cohorts'][0]['workflows']['multi']['human_reviewed_runs'], 0)
        self.assertIsNone(full['cohorts'][0]['workflows']['multi']['advice_supported']['rate'])

    def test_complete_review_uses_all_runs_and_allows_partial_but_not_failed(self):
        paths = self.full()
        # The free control yields honest partial reports. Mark one failed to test the delivery gate.
        self.edit_report(paths[0], 1, lambda r: r.update(status='failed'))
        result, _ = summarize_pairs(paths, self.manifest, self.approved(paths))
        group = result['cohorts'][0]
        self.assertEqual(group['quality_status'], 'reviewed')
        self.assertEqual(group['workflows']['single']['qualified_reports'], {'numerator': 6, 'denominator': 6, 'rate': 1})
        self.assertEqual(group['workflows']['multi']['qualified_reports']['numerator'], 5)
        self.assertEqual(len(group['paired_outcomes']), 6)
        self.assertAlmostEqual(group['comparison']['qualified_rate_difference_percentage_points'], -100 / 6)
        self.assertEqual(sum(g['critical_checks_unavailable'] for g in group['workflows'].values()), 4)

    def test_mechanical_failure_remains_in_denominator_and_costs(self):
        paths = self.full()
        self.edit_report(paths[0], 0, lambda r: r['events'][0].update(seq=999))
        result, _ = summarize_pairs(paths, self.manifest, self.approved(paths))
        arm = result['cohorts'][0]['workflows']['single']
        self.assertEqual(arm['qualified_reports']['denominator'], 6)
        self.assertEqual(arm['qualified_reports']['numerator'], 5)
        self.assertEqual(arm['duration_ms']['known_samples'], 6)

    def test_assistant_approval_never_becomes_human_quality(self):
        paths = self.full()
        reviews = self.approved(paths)
        for row in reviews['runs']:
            row['reviewer_type'] = 'assistant'
        result, _ = summarize_pairs(paths, self.manifest, reviews)
        self.assertTrue(all(a['human_reviewed_runs'] == 0 for a in result['cohorts'][0]['workflows'].values()))

    def test_scripted_reports_cannot_receive_formal_scores(self):
        path = self.bundle(mode='scripted_control_only')
        result, sheet = summarize_pairs([path], self.manifest)
        self.assertEqual(result['cohorts'][0]['quality_status'], 'excluded_scripted')
        sheet['runs'][0]['approved'] = True
        with self.assertRaisesRegex(ValueError, 'Scripted'):
            summarize_pairs([path], self.manifest, sheet)

    def test_modified_report_and_path_traversal_rejected(self):
        path = self.bundle()
        bundle = suite.read_json(path)
        report_path = path.parent / bundle['runs'][0]['report_file']
        report_path.write_text(report_path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'hash'):
            summarize_pairs([path], self.manifest)
        bundle['runs'][0]['report_file'] = '../escape.json'
        write_json(path, bundle)
        with self.assertRaises(ValueError):
            summarize_pairs([path], self.manifest)

    def test_duplicates_missing_pairs_and_changed_order_are_rejected(self):
        path = self.bundle()
        with self.assertRaisesRegex(ValueError, 'Duplicate run_id'):
            summarize_pairs([path, path], self.manifest)
        bundle = suite.read_json(path)
        bundle['runs'].reverse()
        write_json(path, bundle)
        with self.assertRaisesRegex(ValueError, 'order'):
            summarize_pairs([path], self.manifest)
        bundle['runs'].pop()
        write_json(path, bundle)
        with self.assertRaises(ValueError):
            summarize_pairs([path], self.manifest, historical=True)

    def test_same_case_arm_cannot_be_replaced_by_a_retry(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate case'):
            summarize_pairs([self.bundle(), self.bundle()], self.manifest)

    def test_unknown_review_wrong_hash_and_duplicate_reviews_rejected(self):
        paths = [self.bundle()]
        for change in (lambda s: s['runs'][0].update(report_sha256='changed'),
                       lambda s: s['runs'].append(deepcopy(s['runs'][0])),
                       lambda s: s['runs'][0].update(run_id='unknown')):
            sheet = self.approved(paths)
            change(sheet)
            with self.assertRaises(ValueError):
                summarize_pairs(paths, self.manifest, sheet)

    def test_frozen_checks_cannot_hide_omissions_or_use_unknown_evidence(self):
        paths = [self.bundle()]
        mutations = (lambda c: c.update(status='unavailable', evidence_ids=[]),
                     lambda c: c.update(evidence_ids=['EV_invented']),
                     lambda c: c.update(requirement='different'), lambda c: c.update(reason=''))
        for change in mutations:
            sheet = self.approved(paths)
            row = next(r for r in sheet['runs'] if r['case_id'] == 'eval_001')
            change(row['critical_checks'][0])
            with self.assertRaises(ValueError):
                summarize_pairs(paths, self.manifest, sheet)
        sheet = self.approved(paths)
        sheet['runs'][0]['critical_checks'].pop()
        with self.assertRaises(ValueError):
            summarize_pairs(paths, self.manifest, sheet)

    def test_action_and_hypothesis_qualifiers_are_retained(self):
        report = deepcopy(self.originals['eval_001', 'single'])
        report['output']['hypotheses'] = [{'cause': 'candidate', 'status': 'unresolved', 'level': 'mechanism'}]
        report['output']['recommended_actions'] = [{'action': 'check', 'condition': 'before writing', 'risk': 'outage'}]
        entries = qualified_targets(report)
        self.assertEqual(next(r for r in entries if r['target_id'] == 'H1')['published_content']['status'], 'unresolved')
        self.assertEqual(next(r for r in entries if r['target_id'] == 'A1')['published_content']['condition'], 'before writing')

    def test_qualified_content_changes_are_rejected(self):
        paths = [self.bundle()]
        sheet = self.approved(paths)
        claim = next(c for r in sheet['runs'] for c in r['claim_assessments'])
        claim['published_content']['statement'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'original'):
            summarize_pairs(paths, self.manifest, sheet)

    def test_history_partitioned_without_current_quality_or_rewriting(self):
        paths = [self.bundle(app='old-app-1'), self.bundle(app='old-app-2', model='old-model')]
        before = {p: p.read_bytes() for bp in paths for p in bp.parent.glob('*.json')}
        result, sheet = summarize_pairs(paths, self.manifest, historical=True)
        self.assertEqual((result['runs_total'], len(result['cohorts'])), (8, 2))
        self.assertTrue(all(c['historical_only'] and not c['complete'] for c in result['cohorts']))
        self.assertEqual(len(sheet['runs']), 8)
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
        with self.assertRaises(ValueError):
            summarize_pairs(paths, self.manifest)

    def test_unknown_token_totals_not_filled_with_zero_and_no_advice_not_perfect(self):
        paths = self.full()
        result, _ = summarize_pairs(paths, self.manifest, self.approved(paths))
        arm = result['cohorts'][0]['workflows']['single']
        self.assertIsNone(arm['complete_total_tokens'])
        self.assertIsNone(arm['estimated_cost'])
        self.assertIsNone(arm['advice_supported']['rate'])
        self.assertIn('未形成成绩', markdown(result))

    def test_prepare_and_plan_cli_never_initialize_models_or_authenticate(self):
        path = self.root / 'experiment.json'
        with patch('config.incident.IncidentConfig.from_file', return_value=self.config), \
             patch('api.auth.get_current_principal', side_effect=AssertionError('No auth')), \
             patch('scripts.compare_independent.capture', side_effect=AssertionError('No runs')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['prepare', '--experiment-id', 'unit-cli', '--cases', 'eval_001', 'eval_005',
                '--output', str(path)]), 0)
            self.assertEqual(compare_main(['--plan', '--experiment-file', str(path), '--cases', 'eval_001', 'eval_005',
                '--plan-output', str(self.root / 'batch.json')]), 0)
        self.assertEqual(suite.read_json(self.root / 'batch.json')['maximum_model_calls'], 64)
        self.assertEqual(suite.read_json(path)['planned_runs'], 4)
        self.assertFalse((self.root / 'attempts').exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['prepare', '--experiment-id', 'unit-cli', '--output', str(path)])

    def test_live_entry_forwards_snapshot_and_receipt_blocks_second_attempt_without_requests(self):
        # Exercise live dispatch flags with mocked constructors/capture only. No supplier HTTP.
        path = self.root / 'experiment.json'
        write_json(path, self.manifest)
        principal = Principal('synthetic_demo', 'cli_reader')
        cfg = replace(self.config, api_key='unit-key')
        args = ['--live', '--execute-live', '--experiment-file', str(path), '--cases', 'eval_001', 'eval_005',
                '--model-budget', '16', '--tool-budget', '8', '--output', str(self.root / 'live-mock')]
        def mock_capture(cid, flow, factory, *a, **kw):
            factory(flow)
            return deepcopy(self.originals[cid, flow])
        with patch('config.incident.IncidentConfig.from_file', return_value=cfg), \
             patch.dict('os.environ', {'INCIDENT_BEARER_TOKEN': 'unit-token'}), \
             patch('api.auth.get_current_principal', return_value=principal), \
             patch('agents.models.build_model') as single, patch('agents.models.build_roles') as multi, \
             patch('scripts.compare_independent.capture', side_effect=mock_capture) as run, \
             redirect_stdout(io.StringIO()):
            compare_main(args)
            self.assertEqual(run.call_count, 4)
            for call in [*single.call_args_list, *multi.call_args_list]:
                self.assertEqual(call.kwargs, {'base_url': cfg.base_url,
                    'extra_body': cfg.extra_body, 'max_output_tokens': 2200})
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                compare_main(args)
            self.assertEqual(run.call_count, 4)
        receipt = suite.read_json(self.root / 'attempts/repeat-1-batch-1.json')
        self.assertEqual(receipt['status'], 'reports_saved')
        self.assertEqual(receipt['saved_runs'], 4)

    def test_offline_summary_cli_never_reads_runtime_configuration_or_auth(self):
        manifest_path = self.root / 'experiment.json'
        write_json(manifest_path, self.manifest)
        bundle_path = self.bundle(mode='scripted_control_only')
        with patch('config.incident.IncidentConfig.from_file', side_effect=AssertionError('No config read')), \
             patch('api.auth.get_current_principal', side_effect=AssertionError('No auth')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['summarize', '--experiment-file', str(manifest_path),
                '--bundle', str(bundle_path), '--output', str(self.root / 'summary')]), 0)
        self.assertTrue((self.root / 'summary/human-reviews.template.json').exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['summarize', '--experiment-file', str(manifest_path),
                '--bundle', str(bundle_path), '--output', str(self.root / 'summary')])


if __name__ == '__main__':
    unittest.main()
