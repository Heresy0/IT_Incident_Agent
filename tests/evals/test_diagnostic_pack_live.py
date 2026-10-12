"""No real requests: validate planning, persistence and Chinese report generation."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config.incident import IncidentConfig
from evals.diagnostic_packs.experiment import cases, run_controls
from evals.diagnostic_packs.reporting import control_markdown
from scripts.validate_diagnostic_packs import main
from tests.workflow.test_diagnostic_pack import collect_remaining_metrics_models
from runtime.context import ExecutionError


class PackLiveTests(unittest.TestCase):
    def test_plan_never_builds_models_or_spends_calls(self):
        with patch('scripts.validate_diagnostic_packs.build_roles', side_effect=AssertionError('No live call')), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paired_runs'], 6)
        self.assertEqual(plan['configuration']['limits']['input_chars'], 56000)
        self.assertNotIn('api_key', json.dumps(plan))

    def test_live_requires_explicit_case_and_budget_before_model_configuration(self):
        with patch('scripts.validate_diagnostic_packs.IncidentConfig.from_file', side_effect=AssertionError('No config')):
            with self.assertRaises(SystemExit):
                main(['--execute-live'])

    def test_budget_upper_bound_is_enforced(self):
        with patch('scripts.validate_diagnostic_packs.IncidentConfig.from_file', side_effect=AssertionError('No config')):
            with self.assertRaises(SystemExit):
                main(['--input-chars', '999999'])

    def test_control_summary_table_uses_chinese_display_labels(self):
        summary, _ = run_controls([next(case for case in cases() if case['case_id'] == 'source_unavailable')])
        summary['source_version'] = 'test'
        table = '\n'.join(line for line in control_markdown(summary).splitlines() if line.startswith('|'))
        self.assertIn('取证数据源不可用', table)
        self.assertIn('业务失败记录', table)
        self.assertNotIn('failed', table)
        self.assertNotIn('source_unavailable', table)
        self.assertNotIn('partial', table)

    def test_script_saves_both_arms_and_translates_the_live_table(self):
        config = IncidentConfig(api_key='fake-for-patched-test', model='fake')
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output, \
                patch('scripts.validate_diagnostic_packs.IncidentConfig.from_file', return_value=config), \
                patch('scripts.validate_diagnostic_packs.build_roles', side_effect=lambda *a, **k: collect_remaining_metrics_models()):
            self.assertEqual(main(['--execute-live', '--cases', 'authentication', '--model-budget', '48',
                '--tool-budget', '64', '--input-chars', '56000', '--output', temp]), 0)
            directory = Path(output.getvalue().splitlines()[-1]).parent
            summary = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(len(summary['runs']), 2)
            for row in summary['runs']:
                self.assertTrue((directory/row['report_file']).exists())
                self.assertIn('query_statuses', row)
            table = '\n'.join(line for line in (directory/'summary.md').read_text(encoding='utf-8').splitlines() if line.startswith('|'))
            self.assertIn('认证异常', table)
            self.assertNotIn('baseline', table)
            self.assertNotIn('authentication', table)
            self.assertNotIn('MODEL_ERROR', table)

    def test_authentication_failure_stops_the_batch_and_retains_failed_run(self):
        config = IncidentConfig(api_key='fake-for-patched-test', model='fake')
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output, \
                patch('scripts.validate_diagnostic_packs.IncidentConfig.from_file', return_value=config), \
                patch('scripts.validate_diagnostic_packs.build_roles', side_effect=ExecutionError('AUTH_ERROR')) as factory:
            self.assertEqual(main(['--execute-live', '--cases', 'connection_wait', 'authentication',
                '--model-budget', '48', '--tool-budget', '64', '--input-chars', '56000', '--output', temp]), 2)
            self.assertEqual(factory.call_count, 1)
            directory = Path(output.getvalue().splitlines()[-1]).parent
            summary = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
            self.assertTrue(summary['stopped_early'])
            self.assertEqual(len(summary['runs']), 1)
            self.assertEqual(summary['runs'][0]['termination_reason'], 'AUTH_ERROR')

    def test_review_protection_comparison_keeps_same_pack_and_marks_each_rule(self):
        from scripts.compare_incident import capture
        config = IncidentConfig(api_key='fake-for-patched-test', model='fake')
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()) as output, \
                patch('scripts.validate_diagnostic_packs.IncidentConfig.from_file', return_value=config), \
                patch('scripts.validate_diagnostic_packs.build_roles', side_effect=lambda *a, **k: collect_remaining_metrics_models()), \
                patch('scripts.validate_diagnostic_packs.capture', wraps=capture) as runner:
            self.assertEqual(main(['--execute-live', '--comparison', 'review-protection', '--cases', 'authentication',
                '--model-budget', '48', '--tool-budget', '64', '--input-chars', '56000', '--output', temp]), 0)
            calls = [call.kwargs for call in runner.call_args_list]
            self.assertEqual([c['review_protection'] for c in calls], ['strict_v1', 'conservative_v1'])
            self.assertEqual(calls[0]['diagnostic_pack'].fingerprint, calls[1]['diagnostic_pack'].fingerprint)
            directory = Path(output.getvalue().splitlines()[-1]).parent
            summary = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual([r['arm'] for r in summary['runs']], ['strict', 'conservative'])
            text = (directory/'summary.md').read_text(encoding='utf-8')
            self.assertIn('调整前复核规则', text)
            self.assertIn('调整后复核规则', text)


if __name__ == '__main__':
    unittest.main()
