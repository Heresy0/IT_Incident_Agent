"""Fixed follow-up plan must retain the initial batch and cumulative cap."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from config.incident import IncidentConfig
from scripts import verify_reviewer_correction as runner


class ReviewerCorrectionTests(unittest.TestCase):
    def freeze(self, directory, paid=96, complete=True):
        previous = Path(directory) / 'summary.json'
        previous.write_text(json.dumps({'runs': [{}] * (21 if complete else 20), 'paid_attempts': paid}), encoding='utf-8')
        path = Path(directory) / 'freeze.json'
        path.write_text(json.dumps({'source_version': 'fixture', 'model': 'fixture',
            'files': {}, 'driver_files': {}, 'application_commit': 'fixture',
            'previous_summary': previous.relative_to(runner.ROOT).as_posix(),
            'previous_summary_sha256': runner.digest(previous), 'previous_paid_attempts': paid,
            'rubric_defect_preserved': 'completion time is not start time'}), encoding='utf-8')
        return path

    def test_fixed_six_followups_and_cumulative_paid_cap(self):
        with TemporaryDirectory(dir=runner.ROOT / 'output') as directory:
            with patch.object(runner, 'LOCK', self.freeze(directory)), patch.object(runner, 'implementation_version', return_value='fixture'):
                plan = runner.execution_plan(IncidentConfig(model='fixture'))
        self.assertEqual(len(plan['execution_order']), 6)
        self.assertEqual(plan['maximum_paid_attempts'], 96)
        self.assertEqual(plan['maximum_cumulative_paid_attempts'], 192)
        self.assertTrue(all(j['reviewer_output_tokens'] == 4800 for j in plan['execution_order']))
        self.assertTrue(all(j['orchestration'] == 'review_driven' for j in plan['execution_order']))

    def test_incomplete_initial_batch_or_excessive_cumulative_budget_is_rejected(self):
        with TemporaryDirectory(dir=runner.ROOT / 'output') as directory:
            for paid, complete, message in [(96, False, 'incomplete'), (300, True, 'budget')]:
                with patch.object(runner, 'LOCK', self.freeze(directory, paid, complete)), patch.object(runner, 'implementation_version', return_value='fixture'):
                    with self.assertRaisesRegex(ValueError, message):
                        runner.execution_plan(IncidentConfig(model='fixture'))

    def test_plan_only_never_constructs_models(self):
        with TemporaryDirectory(dir=runner.ROOT / 'output') as directory:
            with patch.object(runner, 'LOCK', self.freeze(directory)), patch.object(runner, 'implementation_version', return_value='fixture'), patch.object(runner.IncidentConfig, 'from_file', return_value=IncidentConfig(model='fixture')):
                with patch.object(runner, 'models_for', side_effect=AssertionError('no paid models')), redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.main([]), 0)


if __name__ == '__main__':
    unittest.main()
