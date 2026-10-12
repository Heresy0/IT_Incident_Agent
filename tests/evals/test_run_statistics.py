"""Statistics must not confuse protocol success, semantic quality and missing measurements."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from evals.run_statistics import load_reports, summarize, review_template, markdown
from scripts.summarize_runs import main


def saved(mode='live', rid='run-1'):
    report = {'run_id': rid, 'incident_id': 'case-1', 'workflow': 'multi', 'model': 'unit-model',
        'execution_mode': mode, 'status': 'partial', 'review_status': 'passed',
        'provenance': {'implementation_sha256_prefix': 'source-1', 'dataset_version': 'unit-v1'},
        'output': {'recommended_actions': [{'action': 'Verify certificate', 'refs': []}]},
        'reviews': [{'revision': 1, 'review': {'assessments': [
            {'target_id': 'A1', 'verdict': 'uncertain', 'reason': 'Needs verification'}]}}],
        'events': [{'type': 'validation_failure', 'role': 'reviewer', 'reason': 'schema_mismatch'},
                   {'type': 'validation_repair', 'role': 'reviewer'},
                   {'type': 'tool_result', 'status': 'error', 'truncated': False}],
        'task_results': [{'role': 'investigation', 'completion': {'checks': [
            {'tool': 'logs', 'args': {'category': 'resource'}, 'status': 'not_executed'},
            {'tool': 'logs', 'args': {'category': 'dependency'}, 'status': 'failed'}]}}],
        'run_summary': {'model_calls': 2, 'tool_calls': 1, 'duration_ms': 1000,
            'termination_reason': 'BUDGET_EXCEEDED', 'limits': {'model_calls': 8, 'tool_calls': 8},
            'token_usage': {'status': 'known', 'known_calls': 2, 'input_tokens': 1000, 'output_tokens': 500}}}
    digest = hashlib.sha256(json.dumps(report).encode()).hexdigest()
    return {'report': report, 'sha256': digest}


def approved(row):
    review = review_template([row])['runs'][0]
    review.update(approved=True, reviewer='Unit human', rationale='Offline contract test', conclusion_correct=False,
                  critical_checks=[{'check_id': 'resource', 'status': 'missed', 'reason': 'Not queried'},
                                   {'check_id': 'dependency', 'status': 'observed', 'reason': 'Observed'},
                                   {'check_id': 'owner', 'status': 'unavailable', 'reason': 'Source absent'}],
                  handoff={'required': True, 'actionable': False, 'reason': 'No next step'},
                  human_effort={'baseline_ms': 500, 'assisted_ms': 800})
    for action in review['actions']:
        action.update(supported=False, reason='No evidence')
    for block in review['review_blocks']:
        block.update(unjustified=True, reason='Reasonable conditional check')
    return {'runs': [review]}


class StatisticsTests(unittest.TestCase):
    def test_pending_is_unknown_even_if_internal_review_passed(self):
        result = summarize([saved()])
        q = result['groups'][0]['quality']
        self.assertIsNone(q['wrong_conclusion']['rate'])
        self.assertIsNone(q['reviewer_false_block']['rate'])
        self.assertIsNone(q['actionable_handoff']['rate'])
        self.assertEqual(result['groups'][0]['engineering']['protocol_error_count_total'], 1)
        self.assertEqual(result['groups'][0]['engineering']['declared_checks_not_executed_total'], 1)
        self.assertIn('未知', markdown(result))

    def test_absent_events_do_not_imply_zero_protocol_errors(self):
        row = saved()
        row['report'].pop('events')
        result = summarize([row])
        e = result['groups'][0]['engineering']
        self.assertEqual(e['events_known_runs'], 0)
        self.assertIsNone(e['protocol_error_run_rate']['rate'])
        self.assertIsNone(e['protocol_error_count_total'])
        self.assertIsNone(result['runs'][0]['engineering']['query_errors'])

    def test_human_quality_rates_have_separate_denominators_and_keep_negative_savings(self):
        row = saved()
        result = summarize([row], approved(row))
        q = result['groups'][0]['quality']
        self.assertEqual(q['critical_check_omission'], {'numerator': 1, 'denominator': 2, 'rate': .5})
        self.assertEqual(q['critical_checks_unavailable'], 1)
        self.assertEqual(q['unsupported_published_advice']['rate'], 1)
        self.assertEqual(q['reviewer_false_block']['rate'], 1)
        self.assertEqual(q['actionable_handoff']['rate'], 0)
        self.assertEqual(q['wrong_conclusion']['rate'], 1)
        self.assertEqual(q['human_effort_difference']['mean'], -300)

    def test_assistant_review_excluded(self):
        row = saved()
        review = approved(row)
        review['runs'][0]['reviewer_type'] = 'assistant'
        result = summarize([row], review)
        self.assertEqual(result['groups'][0]['quality']['human_reviewed_runs'], 0)

    def test_scripted_quality_excluded_and_cannot_be_approved(self):
        row = saved('scripted_control_only')
        self.assertEqual(summarize([row])['runs'][0]['quality_status'], 'excluded_scripted')
        with self.assertRaisesRegex(ValueError, 'Scripted'):
            summarize([row], approved(row))

    def test_review_hash_and_content_and_complete_coverage_required(self):
        row = saved()
        for change in (lambda r: r.update(report_sha256='modified'),
                       lambda r: r['actions'][0]['action'].update(action='Changed'),
                       lambda r: r.update(review_blocks=[]),
                       lambda r: r.update(handoff={'required': True, 'actionable': None}),
                       lambda r: r.update(human_effort={'baseline_ms': 500, 'assisted_ms': None})):
            review = approved(row)
            change(review['runs'][0])
            with self.assertRaises(ValueError):
                summarize([row], review)

    def test_unknown_or_duplicate_reviews_rejected(self):
        row = saved()
        review = approved(row)
        review['runs'].append(copy.deepcopy(review['runs'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            summarize([row], review)

    def test_failures_and_partial_retained_and_versions_modes_budgets_separated(self):
        rows = [saved(rid=f'run-{i}') for i in range(6)]
        rows[1]['report']['status'] = 'failed'
        rows[2]['report']['model'] = 'other-model'
        rows[3]['report']['provenance']['implementation_sha256_prefix'] = 'source-2'
        rows[4]['report']['execution_mode'] = 'scripted_control_only'
        rows[5]['report']['run_summary']['limits']['model_calls'] = 16
        result = summarize(rows)
        self.assertEqual(result['runs_total'], 6)
        self.assertEqual(len(result['groups']), 5)
        group = next(g for g in result['groups'] if g['runs'] == 2)
        self.assertEqual(group['statuses'], {'partial': 1, 'failed': 1})

    def test_absent_source_fingerprint_cannot_be_pooled(self):
        rows = [saved(rid=f'run-{i}') for i in range(2)]
        for row in rows:
            row['report'].pop('provenance')
        self.assertEqual(len(summarize(rows)['groups']), 2)

    def test_dynamic_prompt_hashes_retained_without_splitting_same_source(self):
        rows = [saved(rid=f'run-{i}') for i in range(2)]
        for i, row in enumerate(rows):
            row['report']['prompt_version'] = f'dynamic-{i}'
        result = summarize(rows)
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(result['groups'][0]['prompt_versions_observed'], ['dynamic-0', 'dynamic-1'])

    def test_dirty_git_commit_is_not_an_implementation_fingerprint(self):
        rows = [saved(rid=f'run-{i}') for i in range(2)]
        for row in rows:
            row['report']['provenance'] = {'git_commit': 'same-head', 'working_tree_dirty': True}
        self.assertEqual(len(summarize(rows)['groups']), 2)

    def test_cost_requires_exact_model_price_and_complete_usage(self):
        row = saved()
        prices = {'currency': 'CNY', 'models': {'unit-model': {'input_per_million': 2, 'output_per_million': 4}}}
        self.assertIsNone(summarize([row])['runs'][0]['engineering']['estimated_token_cost'])
        self.assertAlmostEqual(summarize([row], pricing=prices)['runs'][0]['engineering']['estimated_token_cost'], .004)
        missing = saved(rid='run-2')
        missing['report']['run_summary']['token_usage']['status'] = 'partial_or_unknown'
        cost = summarize([row, missing], pricing=prices)['groups'][0]['engineering']['estimated_token_cost']
        self.assertIsNone(cost['total'])
        self.assertAlmostEqual(cost['known_total'], .004)
        self.assertEqual(cost['known_runs'], 1)
        row['report']['model'] = 'other'
        self.assertIsNone(summarize([row], pricing=prices)['runs'][0]['engineering']['estimated_token_cost'])

    def test_embedding_run_is_not_priced_with_chat_only_rates(self):
        row = saved()
        row['report']['events'].append({'type':'call_start','kind':'model','name':'knowledge_embedding'})
        prices = {'currency':'CNY','models':{'unit-model':{'input_per_million':2,'output_per_million':4}}}
        self.assertIsNone(summarize([row],pricing=prices)['runs'][0]['engineering']['estimated_token_cost'])

    def test_invalid_prices_rejected(self):
        for price in (-1, float('nan'), True):
            with self.assertRaises(ValueError):
                summarize([saved()], pricing={'currency': 'CNY', 'models': {'unit-model': {
                    'input_per_million': price, 'output_per_million': 1}}})

    def test_declared_check_rework_deduplicated_and_latest_status_used(self):
        row = saved()
        task = copy.deepcopy(row['report']['task_results'][0])
        task['completion']['checks'][0]['status'] = 'satisfied'
        row['report']['task_results'].append(task)
        e = summarize([row])['runs'][0]['engineering']
        self.assertEqual(e['declared_checks_total'], 2)
        self.assertEqual(e['declared_checks_not_executed'], 0)

    def test_load_bundle_hash_paths_duplicate_and_offline_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = saved()
            path = root / 'report.json'
            path.write_text(json.dumps(row['report']), encoding='utf-8')
            report = row['report']
            bundle = {'configuration': {k: report[k] for k in ('model', 'execution_mode')},
                      'provenance': report['provenance'], 'runs': [{'case_id': 'case-1', 'workflow': 'multi',
                          'report_file': path.name, 'report_sha256': row['sha256']}]}
            bundle['configuration']['limits'] = report['run_summary']['limits']
            bp = root / 'bundle.json'
            bp.write_text(json.dumps(bundle), encoding='utf-8')
            self.assertEqual(len(load_reports(bundle_paths=[bp])), 1)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--bundle', str(bp), '--output', str(root / 'stats')]), 0)
            self.assertTrue((root / 'stats/statistics.md').exists())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--report', str(path), '--output', str(root / 'stats')])
            with self.assertRaisesRegex(ValueError, 'unique'):
                load_reports([path, path])
            bundle['runs'][0]['report_file'] = '../report.json'
            bp.write_text(json.dumps(bundle), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'directory'):
                load_reports(bundle_paths=[bp])
            bundle['runs'][0]['report_file'] = path.name
            bundle['runs'][0]['report_sha256'] = 'wrong'
            bp.write_text(json.dumps(bundle), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'hash'):
                load_reports(bundle_paths=[bp])


if __name__ == '__main__':
    unittest.main()
