"""Capability suite boundaries; scripted tests never establish model quality."""
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api.auth import Principal
from evidence.contracts import TOOL_ARGS
from evals.common import write_json
from evals.independent.control import smoke_factory
from evals.targeted.provider import TargetedProvider
from evals.targeted.suite import SUITE_ROOT, DATA_ROOT, check_suite, select_cases, digest, verify_freeze
from evals.targeted.scoring import evaluate, evaluate_batches, semantic_template, published_targets
from runtime.context import Limits
from scripts.compare_incident import capture
from scripts.compare_targeted import main, parse_args

P = Principal('synthetic_demo', 'cli_reader')
LIMITS = Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20)


class TargetedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pairs = {}
        for cid in ('probe_001', 'probe_002', 'probe_003', 'probe_007', 'probe_008'):
            provider = TargetedProvider(cid, P)
            cls.pairs[cid] = [capture(cid, flow, smoke_factory(provider), P, LIMITS,
                                     provider_factory=TargetedProvider) for flow in ('single', 'multi')]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def bundle(self, cases=('probe_001',), mode='scripted_control_only'):
        config = {'cases': list(cases), 'split': 'development', 'release_holdout': False,
                  'limits': LIMITS.__dict__, 'model': 'test-scoring-control', 'execution_mode': mode}
        provenance = {'dataset_version': 'targeted-synthetic-v1',
            'dataset_freeze_sha256': digest(SUITE_ROOT / 'freeze.json'),
            'fixture_sha256': {cid: digest(DATA_ROOT / f'{cid}.json') for cid in cases},
            'implementation_sha256_prefix': 'unit-control-only'}
        bundle = {'configuration': config, 'provenance': provenance, 'runs': []}
        for cid in cases:
            for original in self.pairs[cid]:
                report = {**copy.deepcopy(original), 'model': config['model'], 'execution_mode': mode,
                          'provenance': provenance}
                path = self.directory / f"{report['run_id']}.json"
                write_json(path, report)
                bundle['runs'].append({'case_id': cid, 'workflow': report['workflow'], 'report_file': path.name,
                    'report_sha256': digest(path), 'answer_sha256': digest(SUITE_ROOT / 'answers' / f'{cid}.json')})
        return bundle

    def reviews(self, reports):
        rows = []
        for saved in reports.values():
            row = semantic_template(saved['report'], saved['sha256'])
            row.update(approved=True, reviewer='评分器单元测试', rationale='模拟分值，仅验证汇总，不是模型成绩',
                       scores={key: 2 for key in row['scores']}, unsafe_advice=False, fabricated_evidence=False)
            for claim in row['claim_assessments']:
                claim.update(supported=True, reason='模拟逐项评审')
            for item in row['capability_assessments'].values():
                item.update(score=2, reason='模拟报告依据')
            if saved['report']['workflow'] == 'single':
                row['scores']['actions'] = 0  # Exercise a known numeric difference, not a semantic judgment.
            rows.append(row)
        return {'runs': rows}

    def test_frozen_eight_events_tools_and_distinct_splits(self):
        result = check_suite()
        self.assertEqual(result['cases'], 8)
        self.assertEqual(result['splits'], {'development': 5, 'holdout': 3})
        self.assertEqual(result['provider_queries'], 64)
        self.assertFalse(result['reserved_answers_parsed'])
        self.assertEqual(len(verify_freeze()['files']), 19)
        manifest = json.loads((SUITE_ROOT / 'manifest.json').read_text(encoding='utf-8'))
        for family in ('history', 'conflict', 'discrimination'):
            development = [cid for cid in manifest['splits']['development'] if manifest['case_metadata'][cid]['family'] == family]
            reserved = [cid for cid in manifest['splits']['holdout'] if manifest['case_metadata'][cid]['family'] == family]
            self.assertEqual((len(development), len(reserved)), (1, 1))
            a, b = (TargetedProvider(cid, P)._data for cid in (development[0], reserved[0]))
            self.assertNotEqual(a['ticket']['scope']['service'], b['ticket']['scope']['service'])
            self.assertNotEqual({r['error_code'] for r in a['logs']}, {r['error_code'] for r in b['logs']})

    def test_plan_and_holdout_flags_do_not_load_models_or_answers(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            self.assertNotIn('answers', path.parts)
            self.assertNotIn(path.name, ('.env.local', '.auth.local.json'))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guard), patch('scripts.compare_independent.capture', side_effect=AssertionError('No runs')), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([]), 0)
        plan = json.loads(out.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paired_runs'], 10)
        self.assertEqual(plan['configuration']['model'], 'qwen-turbo')
        for args in (['--fake', '--split', 'holdout'], ['--live'],
                     ['--live', '--execute-live', '--cases', 'probe_001', 'probe_002', 'probe_003',
                      '--model-budget', '16', '--tool-budget', '8']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)
        self.assertEqual(select_cases('holdout', release_holdout=True), ['probe_004', 'probe_005', 'probe_006'])

    def test_runtime_never_reads_answers_and_single_keeps_six_tools(self):
        from tools.executor import ToolExecutor
        from runtime.context import RunContext
        provider = TargetedProvider('probe_001', P)
        executor = ToolExecutor(provider, P, provider.ticket().scope,
                                RunContext(limits=LIMITS, scope=provider.ticket().scope), role='single')
        self.assertEqual({s['function']['name'] for s in executor.schemas()}, set(TOOL_ARGS))
        original = Path.read_text
        def guard(path, *args, **kwargs):
            if 'answers' in path.parts:
                self.fail('Runtime opened reference answer')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guard):
            result = capture('probe_001', 'single', smoke_factory(provider), P, LIMITS,
                             provider_factory=TargetedProvider)
        self.assertGreater(result['run_summary']['tool_calls'], 0)

    def test_history_available_without_answer_leak_and_logs_preserve_time_scope(self):
        provider = TargetedProvider('probe_001', P)
        args = TOOL_ARGS['search_incidents'].model_validate_json(json.dumps({'symptoms': 'delivery'}))
        rows, _ = provider.query('search_incidents', args, provider.ticket().scope)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['confirmed'])
        self.assertNotIn('capability_criteria', rows[0])
        provider = TargetedProvider('probe_002', P)
        args = TOOL_ARGS['get_service_logs'].model_validate_json(json.dumps({
            'start': '2026-10-12T10:00:00+00:00', 'end': '2026-10-12T10:20:00+00:00', 'category': 'resource'}))
        rows, _ = provider.query('get_service_logs', args, provider.ticket().scope)
        self.assertEqual(rows, [])  # old runner at09:52 cannot leak into narrower current query

    def test_claim_context_preserves_refutation_conditions_and_pending_state(self):
        report = copy.deepcopy(self.pairs['probe_001'][1])
        report['output']['hypotheses'] = [{'cause': 'CPU饱和', 'status': 'refuted', 'level': 'alternative',
            'support_refs': [], 'counter_refs': [], 'evidence_explanation': '当前CPU对照不支持', 'pending_checks': []}]
        report['output']['recommended_actions'] = [{'action': '调整编码器', 'condition': '兼容性确认后',
            'requires_approval': True, 'kind': 'manual_change', 'expected_result': '拒绝率下降', 'risk': '兼容风险'}]
        report['pending_actions'] = [{'action': '待确认回切', 'kind': 'manual_change', 'repair': None, 'executable': False}]
        rows = {row['target_id']: row for row in published_targets(report)}
        self.assertEqual(rows['H1']['claim_context']['status'], 'refuted')
        self.assertEqual(rows['A1']['claim_context']['condition'], '兼容性确认后')
        self.assertEqual(rows['P1']['published_state'], 'pending_not_executable')
        self.assertFalse(rows['P1']['claim_context']['executable'])

    def test_scripted_results_never_receive_quality_or_advantage(self):
        bundle = self.bundle()
        summary, saved = evaluate(bundle, self.directory)
        self.assertEqual(summary['quality_status'], 'excluded_scripted')
        self.assertIsNone(summary['advantage_signal']['threshold_met'])
        self.assertEqual(summary['case_groups']['targeted']['reviewed_pairs'], 0)
        with self.assertRaisesRegex(ValueError, 'live'):
            evaluate(bundle, self.directory, self.reviews(saved))

    def test_complete_semantic_context_and_capability_scores_required(self):
        bundle = self.bundle(mode='live')  # scoring-unit fixture only; no model calls
        _, saved = evaluate(bundle, self.directory)
        reviews = self.reviews(saved)
        self.assertNotIn('workflow', reviews['runs'][0])
        for mutation in (lambda r: r.update(capability_assessments={}),
                         lambda r: r['claim_assessments'][0]['claim_context'].update(statement='被修改'),
                         lambda r: next(iter(r['capability_assessments'].values())).update(score=True)):
            changed = copy.deepcopy(reviews)
            mutation(changed['runs'][0])
            with self.assertRaises(ValueError):
                evaluate(bundle, self.directory, changed)

    def test_bounded_batch_aggregation_retains_all_three_families_and_controls(self):
        batches = [(self.bundle((cid,), mode='live'), self.directory)
                   for cid in ('probe_001', 'probe_002', 'probe_003', 'probe_007')]
        reviews = {'runs': []}
        for bundle, directory in batches:
            _, saved = evaluate(bundle, directory)
            reviews['runs'].extend(self.reviews(saved)['runs'])
        summary, _ = evaluate_batches(batches, reviews)
        self.assertEqual(summary['batch_count'], 4)
        self.assertEqual(summary['case_groups']['targeted']['pairs'], 3)
        self.assertEqual(summary['case_groups']['control']['pairs'], 1)
        self.assertTrue(summary['advantage_signal']['threshold_met'])
        reviews['runs'][1]['unsafe_advice'] = True
        summary, _ = evaluate_batches(batches, reviews)
        self.assertEqual(len(summary['advantage_signal']['qualifying_families']), 2)
        with self.assertRaisesRegex(ValueError, 'Repeated'):
            evaluate_batches([batches[0], batches[0]])
        changed = copy.deepcopy(batches[1][0])
        changed['configuration']['limits'] = {**changed['configuration']['limits'], 'model_calls': 15}
        with self.assertRaisesRegex(ValueError, 'changed'):
            evaluate_batches([batches[0], (changed, self.directory)])

    def test_batch_rejects_report_and_answer_tampering(self):
        for key in ('report_sha256', 'answer_sha256'):
            bundle = self.bundle()
            bundle['runs'][0][key] = 'tampered'
            with self.assertRaises(ValueError):
                evaluate_batches([(bundle, self.directory)])

    def test_saved_pairs_exist_before_scoring_answers_and_reserved_not_run(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            if 'answers' in path.parts:
                self.assertNotIn(path.stem, ('probe_004', 'probe_005', 'probe_006'))
                bundle_path = next(self.directory.glob('*/bundle.json'))
                bundle = json.loads(original(bundle_path, encoding='utf-8'))
                self.assertEqual(len(bundle['runs']), 2)
                self.assertTrue(all((bundle_path.parent / r['report_file']).exists() for r in bundle['runs']))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guard), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--fake', '--cases', 'probe_001', '--output', str(self.directory)]), 0)


if __name__ == '__main__':
    unittest.main()
