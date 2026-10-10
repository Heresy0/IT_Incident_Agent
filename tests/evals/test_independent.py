"""Free independent-evaluation boundaries; no credentials, networks or semantic model claims."""
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
from providers.fixtures import ProviderError
from runtime.context import Limits
from scripts.compare_incident import capture
from scripts.compare_independent import parse_args, main
from evals.common import write_json
from evals.independent.control import smoke_factory
from evals.independent.provider import IndependentProvider
from evals.independent.suite import SUITE_ROOT, DATA_ROOT, digest, select_cases, check_suite, verify_freeze
from evals.independent.scoring import evaluate, semantic_template

P = Principal('synthetic_demo', 'cli_reader')
LIMITS = Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20)


class IndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        provider = IndependentProvider('eval_001', P)
        cls.pair = [capture('eval_001', flow, smoke_factory(provider), P, LIMITS,
                            provider_factory=IndependentProvider) for flow in ('single', 'multi')]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def bundle(self, mode='scripted_control_only'):
        provenance = {'dataset_version': 'independent-synthetic-v1',
                      'dataset_freeze_sha256': digest(SUITE_ROOT / 'freeze.json'),
                      'fixture_sha256': {'eval_001': digest(DATA_ROOT / 'eval_001.json')}}
        config = {'cases': ['eval_001'], 'split': 'validation', 'release_holdout': False,
                  'limits': LIMITS.__dict__, 'model': 'unit-control', 'execution_mode': mode}
        bundle = {'configuration': config, 'provenance': provenance, 'runs': []}
        for original in self.pair:
            report = {**copy.deepcopy(original), 'model': config['model'], 'execution_mode': mode,
                      'provenance': provenance}
            path = self.directory / f"{report['run_id']}.json"
            write_json(path, report)
            bundle['runs'].append({'case_id': 'eval_001', 'workflow': report['workflow'],
                'report_file': path.name, 'report_sha256': digest(path),
                'answer_sha256': digest(SUITE_ROOT / 'answers/eval_001.json')})
        return bundle

    def reviews(self, reports):
        rows = []
        for run in reports.values():
            row = semantic_template(run['report'], run['sha256'])
            # Synthetic grading fixtures exercise validation only, never real model quality.
            row.update(reviewer='Unit test control', approved=True, rationale='测试评分器，不代表真实诊断质量',
                       scores={key: 2 for key in row['scores']}, unsafe_advice=False, fabricated_evidence=False)
            for claim in row['claim_assessments']:
                claim.update(supported=True, reason='测试逐项评分校验')
            rows.append(row)
        return {'runs': rows}

    def test_frozen_ten_cases_and_distinct_splits(self):
        result = check_suite()
        self.assertEqual(result['cases'], 10)
        self.assertEqual(result['splits'], {'validation': 6, 'holdout': 4})
        self.assertEqual(result['provider_queries'], 80)
        self.assertEqual(result['model_calls'], 0)
        self.assertFalse(result['reserved_answers_parsed'])

    def test_holdout_and_mixed_cases_denied_by_default(self):
        for args in [('holdout', None, False), ('validation', ['eval_007'], False),
                     ('validation', ['eval_001', 'eval_001'], False)]:
            with self.assertRaises(ValueError):
                select_cases(*args)
        self.assertEqual(len(select_cases('holdout', release_holdout=True)), 4)

    def test_live_flags_and_small_batch_enforced_before_credentials(self):
        invalid = [ ['--execute-live'], ['--live'],
            ['--live', '--execute-live', '--cases', 'eval_001'],
            ['--live', '--execute-live', '--cases', 'eval_001', 'eval_002', 'eval_003',
             '--model-budget', '16', '--tool-budget', '8'],
            ['--fake', '--split', 'holdout'], ['--plan', '--model-budget', '17'] ]
        for argv in invalid:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_default_plan_never_loads_answers_models_or_credentials(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            self.assertNotIn('answers', path.parts)
            self.assertNotIn(path.name, ('.env.local', '.auth.local.json', '.demo-credentials.local.json'))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guard), patch('scripts.compare_independent.capture', side_effect=AssertionError('No runs')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paired_runs'], 12)

    def test_runtime_cannot_read_answers_and_preserves_scope_and_time(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            if 'answers' in path.parts:
                self.fail('Runtime attempted to read reference answer')
            return original(path, *args, **kwargs)
        provider = IndependentProvider('eval_002', P)
        with patch.object(Path, 'read_text', guard):
            report = capture('eval_002', 'single', smoke_factory(provider), P, LIMITS,
                             provider_factory=IndependentProvider)
        self.assertGreater(report['run_summary']['tool_calls'], 0)
        with self.assertRaises(ProviderError):
            provider.authorize(Principal('other', 'cli_reader'), provider.ticket().scope)
        args = TOOL_ARGS['get_recent_changes'].model_validate_json(json.dumps({
            'start': '2026-10-01T10:05:00+00:00', 'end': '2026-10-01T10:15:00+00:00'}))
        rows, _ = provider.query('get_recent_changes', args, provider.ticket().scope)
        self.assertEqual(rows, [])  # deployment at10:02 is outside this narrower query

    def test_channel_error_is_not_empty_or_health(self):
        provider = IndependentProvider('eval_005', P)
        args = TOOL_ARGS['get_service_metrics'].model_validate_json(json.dumps({
            'start': '2026-10-01T09:50:00+00:00', 'end': '2026-10-01T10:15:00+00:00',
            'metrics': ['request_error_rate']}))
        with self.assertRaises(ProviderError) as error:
            provider.query('get_service_metrics', args, provider.ticket().scope)
        self.assertEqual(error.exception.code, 'SOURCE_UNAVAILABLE')
        self.assertFalse(error.exception.retryable)

    def test_source_unavailable_pair_is_partial_and_empty_refs_are_valid(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--fake', '--cases', 'eval_005', '--output', str(self.directory)]), 0)
        bundle_path = next(self.directory.glob('*/bundle.json'))
        bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
        result, _ = evaluate(bundle, bundle_path.parent)
        for row in result['runs']:
            self.assertEqual(row['status'], 'partial')
            self.assertTrue(row['mechanical_pass'])
            self.assertIsNone(row['semantic_success'])

    def test_changed_freeze_missing_pair_and_report_tamper_rejected(self):
        with patch('evals.independent.suite.digest', return_value='tampered'), self.assertRaises(ValueError):
            verify_freeze()
        bundle = self.bundle()
        with self.assertRaises(ValueError):
            evaluate({**bundle, 'runs': bundle['runs'][:1]}, self.directory)
        path = self.directory / bundle['runs'][0]['report_file']
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            evaluate(bundle, self.directory)

    def test_answer_hash_unequal_budget_and_path_escape_rejected(self):
        for variant in ('answer', 'budget', 'path'):
            bundle = self.bundle()
            entry = bundle['runs'][0]
            if variant == 'answer':
                entry['answer_sha256'] = 'wrong'
            elif variant == 'path':
                entry['report_file'] = '../outside.json'
            else:
                path = self.directory / entry['report_file']
                report = json.loads(path.read_text(encoding='utf-8'))
                report['run_summary']['limits']['tool_calls'] = 9
                write_json(path, report)
                entry['report_sha256'] = digest(path)
            with self.assertRaises(ValueError):
                evaluate(bundle, self.directory)

    def test_no_field_coverage_scripted_quality_or_internal_pass_score(self):
        result, reports = evaluate(self.bundle(), self.directory)
        self.assertEqual(result['quality_status'], 'excluded_scripted')
        for row in result['runs']:
            self.assertNotIn('necessary_check_coverage', row)
            self.assertIsNone(row['semantic_success'])
            self.assertTrue(row['mechanical_pass'])
        for group in result['workflows'].values():
            self.assertIsNone(group['semantic_success_rate_reviewed'])
        with self.assertRaisesRegex(ValueError, 'live'):
            evaluate(self.bundle(), self.directory, self.reviews(reports))

    def test_partial_semantic_grading_and_blinded_template(self):
        bundle = self.bundle(mode='live')  # scoring-unit fixture only; no live calls
        _, reports = evaluate(bundle, self.directory)
        reviews = self.reviews(reports)
        self.assertNotIn('workflow', reviews['runs'][0])
        result, _ = evaluate(bundle, self.directory, reviews)
        self.assertEqual(result['quality_status'], 'reviewed')
        multi = next(row for row in result['runs'] if row['workflow'] == 'multi')
        self.assertEqual(multi['status'], 'partial')
        self.assertTrue(multi['semantic_success'])  # run status alone must not determine quality
        reviews['runs'][0]['unsafe_advice'] = True
        result, _ = evaluate(bundle, self.directory, reviews)
        self.assertFalse(next(row for row in result['runs'] if row['run_id'] == reviews['runs'][0]['run_id'])['semantic_success'])

    def test_incomplete_changed_claim_or_bad_review_is_rejected(self):
        bundle = self.bundle(mode='live')
        _, reports = evaluate(bundle, self.directory)
        for mutate in (lambda row: row['scores'].update(outcome=True),
                       lambda row: row.update(report_sha256='wrong'),
                       lambda row: row['claim_assessments'][0].update(text='changed'),
                       lambda row: row.update(claim_assessments=[])):
            reviews = self.reviews(reports)
            mutate(reviews['runs'][0])
            with self.assertRaises(ValueError):
                evaluate(bundle, self.directory, reviews)

    def test_fake_saves_both_reports_before_answer_parsing(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            if 'answers' in path.parts:
                bundles = list(self.directory.glob('*/bundle.json'))
                self.assertEqual(len(bundles), 1)
                saved = json.loads(original(bundles[0], encoding='utf-8'))
                self.assertEqual(len(saved['runs']), 2)
                self.assertTrue(all((bundles[0].parent / row['report_file']).exists() for row in saved['runs']))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guard), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--fake', '--cases', 'eval_001', '--output', str(self.directory)]), 0)
        self.assertEqual(len(list(self.directory.glob('*/run-index.json'))), 1)


if __name__ == '__main__':
    unittest.main()
