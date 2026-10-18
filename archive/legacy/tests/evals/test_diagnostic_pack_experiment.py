"""Matched mechanical comparisons must retain costs and adverse boundary behavior."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.diagnostic_packs.experiment import cases, run_controls
from scripts.compare_diagnostic_packs import main


class PackExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.reports = run_controls(cases())
        cls.pairs = {r['case_id']: r for r in cls.result['pairs']}

    def test_missing_baseline_checks_are_collected_and_extra_cost_is_counted(self):
        for cid in ('connection_wait', 'different_mechanism', 'renamed_registration'):
            pair = self.pairs[cid]
            self.assertEqual(pair['baseline']['query_statuses'], ['not_executed', 'not_executed'])
            self.assertEqual(pair['pack']['query_statuses'], ['satisfied', 'satisfied'])
            self.assertEqual(pair['extra_tool_calls'], 2)

    def test_authentication_does_not_force_unrelated_pool_reads(self):
        pair = self.pairs['authentication']
        self.assertEqual(pair['extra_tool_calls'], 0)
        self.assertEqual(pair['pack']['observed_metrics'], [])
        self.assertEqual(pair['pack']['termination_reason'], 'NEEDS_INFORMATION')

    def test_unavailable_source_and_sampling_limits_remain_explicit(self):
        for cid, state in (('source_unavailable', 'failed'), ('empty_business_logs', 'empty'),
                           ('truncated_business_logs', 'truncated'), ('dense_business_logs', 'truncated')):
            pair = self.pairs[cid]
            self.assertEqual(pair['pack']['query_statuses'][0], state)
            self.assertTrue(pair['pack']['pack_handoff'])
        report = next(r for r in self.reports if r['experiment_case'] == 'source_unavailable' and r['experiment_arm'] == 'pack')
        logs = [e for e in report['events'] if e['type'] == 'tool_result' and e['name'] == 'get_service_logs']
        self.assertEqual(len(logs), 1)
        for cid in ('source_unavailable', 'truncated_business_logs', 'dense_business_logs', 'small_budget'):
            self.assertTrue(self.pairs[cid]['pack']['pack_blocking_check_ids'])
            self.assertEqual(self.pairs[cid]['pack']['pack_exit_decision'], 'partial')
        self.assertEqual(self.pairs['empty_business_logs']['pack']['pack_blocking_check_ids'], [])

    def test_state_summary_does_not_introduce_input_budget_rejection_in_dense_control(self):
        pair = self.pairs['dense_business_logs']
        self.assertEqual(pair['pack']['input_budget_rejections'], 0)
        report = next(r for r in self.reports if r['experiment_case'] == 'dense_business_logs' and r['experiment_arm'] == 'pack')
        self.assertLessEqual(pair['pack']['input_chars_max'], report['run_summary']['limits']['input_chars'])

    def test_status_check_and_missing_registration_keep_baseline_observations(self):
        for cid in ('status_only', 'unregistered_service'):
            self.assertEqual(self.pairs[cid]['extra_tool_calls'], 0)
            reports = [r for r in self.reports if r['experiment_case'] == cid]
            self.assertEqual({e['evidence_id'] for e in reports[0]['evidence']},
                             {e['evidence_id'] for e in reports[1]['evidence']})

    def test_small_budget_observation_loss_is_retained_as_an_adverse_result(self):
        pair = self.pairs['small_budget']
        self.assertGreater(pair['baseline']['observations_count'], 0)
        self.assertEqual(pair['pack']['observations_count'], 0)
        self.assertEqual(pair['pack']['pack_status'], 'budget_deferred')
        self.assertEqual(pair['pack']['tool_calls'], 0)
        self.assertEqual(pair['pack']['scripted_model_steps'], 0)
        self.assertTrue(pair['pack']['pack_handoff'])

    def test_all_runs_retain_accounting_and_make_no_semantic_claim(self):
        self.assertEqual(len(self.reports), 2 * self.result['cases'])
        self.assertEqual(self.result['paid_model_calls'], 0)
        for report in self.reports:
            summary, events = report['run_summary'], report['events']
            self.assertEqual([e['seq'] for e in events], list(range(1, len(events)+1)))
            for kind, key in (('model', 'model_calls'), ('tool', 'tool_calls')):
                self.assertEqual(sum(e['type'] == 'call_start' and e['kind'] == kind for e in events), summary[key])
                self.assertLessEqual(summary[key], summary['limits'][key])
            self.assertEqual(report['output']['hypotheses'], [])
            self.assertEqual(report['reviews'], [])
            self.assertEqual(report['execution_mode'], 'scripted_control_only')

    def test_default_plan_reads_no_credentials_answers_or_network(self):
        original = Path.read_text
        def guard(path, *args, **kwargs):
            self.assertNotIn(path.name, ('.env', '.auth.local.json', 'services.local.json'))
            self.assertNotIn('answers', path.parts)
            self.assertNotIn('gold', path.parts)
            return original(path, *args, **kwargs)
        with (patch.object(Path, 'read_text', guard), patch('scripts.compare_diagnostic_packs.run_controls',
                side_effect=AssertionError('Default planning cannot execute')),
                redirect_stdout(io.StringIO()) as output):
            self.assertEqual(main([]), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paid_model_calls'], 0)

    def test_explicit_control_writes_hash_bound_reports_and_keeps_same_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--execute-control', '--cases', 'authentication', '--output', tmp]), 0)
            directory = Path(output.getvalue().strip()).parent
            result = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
            reports = []
            for arm in ('baseline', 'pack'):
                row = result['pairs'][0][arm]
                path = directory/row['report_file']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['report_sha256'])
                reports.append(json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(reports[0]['run_summary']['limits'], reports[1]['run_summary']['limits'])
            self.assertEqual(reports[0]['fixture_snapshot_sha256'], reports[1]['fixture_snapshot_sha256'])


if __name__ == '__main__':
    unittest.main()
