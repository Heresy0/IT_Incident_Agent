"""Paid caps, fair primary quotas, and free plan-only execution."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from config.incident import IncidentConfig
from scripts import compare_reviewer_recovery as runner


class ReviewerRecoveryTests(unittest.TestCase):
    def freeze(self, directory):
        path = Path(directory) / 'freeze.json'
        path.write_text(json.dumps({'source_version': 'fixture', 'model': 'fixture',
            'files': {}, 'driver_files': {}, 'application_commit': 'fixture',
            'previous_summary': 'historic', 'previous_summary_sha256': 'fixture',
            'rubric_defect_preserved': 'completion time is not start time'}), encoding='utf-8')
        return path

    def test_fixed_21_jobs_fair_primary_quotas_and_distinct_low_cap_probes(self):
        with TemporaryDirectory(dir=runner.ROOT / 'output') as directory:
            with patch.object(runner, 'LOCK', self.freeze(directory)), patch.object(runner, 'implementation_version', return_value='fixture'):
                plan = runner.execution_plan(IncidentConfig(model='fixture'))
        jobs = plan['execution_order']
        self.assertEqual(len(jobs), 21)
        self.assertEqual(plan['maximum_paid_attempts'], 336)
        self.assertEqual(len({(j['case_id'], j['orchestration'], j['repetition'], j['profile']) for j in jobs}), 21)
        self.assertTrue(all(j['reviewer_output_tokens'] == 4800 for j in jobs[:18]))
        self.assertEqual(sum(j['orchestration'] == 'legacy' for j in jobs[:18]), 9)
        self.assertTrue(all(j['reviewer_output_tokens'] == 2200 and j['orchestration'] == 'review_driven' for j in jobs[18:]))
        self.assertEqual(plan['limits']['reserve_seconds'], 90)
        self.assertEqual(plan['seeded_turns'], 0)

    def test_plan_only_never_constructs_or_calls_models(self):
        with TemporaryDirectory(dir=runner.ROOT / 'output') as directory:
            with patch.object(runner, 'LOCK', self.freeze(directory)), patch.object(runner, 'implementation_version', return_value='fixture'), patch.object(runner.IncidentConfig, 'from_file', return_value=IncidentConfig(model='fixture')):
                with patch.object(runner, 'build_roles', side_effect=AssertionError('no paid models')), patch.object(runner, 'build_model', side_effect=AssertionError('no paid models')), redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.main([]), 0)

    def test_only_reviewer_builder_receives_explicit_eval_output_timeout_override(self):
        config = IncidentConfig(api_key='test-only', model='fixture')
        with patch.object(runner, 'build_roles', return_value={'diagnosis': object(), 'reviewer': object()}) as roles, patch.object(runner, 'build_model') as reviewer:
            models = runner.models_for(config, {'reviewer_output_tokens': 4800}, runner.RequestLedger())
        self.assertNotIn('max_output_tokens', roles.call_args.kwargs)
        self.assertEqual(reviewer.call_args.kwargs['max_output_tokens'], 4800)
        self.assertEqual(reviewer.call_args.kwargs['request_timeout'], 60)
        self.assertEqual(models['reviewer'].model, reviewer.return_value)


if __name__ == '__main__':
    unittest.main()
