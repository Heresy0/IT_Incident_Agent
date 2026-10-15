"""Live validation is opt-in, frozen and bounded across jobs, including failures."""
from contextlib import redirect_stdout
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runtime.context import ExecutionError
from config.incident import IncidentConfig
from scripts.verify_review_driven import main, PAID_CAP, PaidBudget, RecordedModel


class ReviewDrivenValidationTests(unittest.TestCase):
    def test_focused_probe_plan_has_independent_case_allowances_and_no_live_models(self):
        from scripts.verify_revision_probe import main as focused_main
        out = io.StringIO()
        with redirect_stdout(out), patch('scripts.verify_revision_probe.build_roles', side_effect=AssertionError('No paid models')):
            self.assertEqual(focused_main([]), 0)
        plan = json.loads(out.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paid_cap_per_case'], 8)
        self.assertEqual(plan['maximum_paid_attempts'], 24)
        self.assertEqual(plan['variants'], ['wording', 'trigger_unknown', 'unsupported_mechanism'])
        self.assertFalse(plan['independent_quality_score'])

    def test_focused_case_cap_cannot_starve_other_case(self):
        first, second = PaidBudget(maximum=8), PaidBudget(maximum=8)
        for _ in range(8):
            first.claim()
        with self.assertRaises(ExecutionError):
            first.claim()
        second.claim()
        self.assertEqual((first.used, second.used), (8, 1))
        for invalid in (0, -1, 51, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PaidBudget(maximum=invalid)

    def test_focused_followup_carries_per_case_counts_and_rejects_inconsistent_total(self):
        from scripts.verify_revision_probe import main as focused_main
        out = io.StringIO()
        with redirect_stdout(out):
            focused_main([])
        plan = json.loads(out.getvalue())
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.json'
            previous = {'plan': plan, 'paid_attempts': 8, 'runs': [
                {'variant': 'wording', 'paid_attempts': 3},
                {'variant': 'trigger_unknown', 'paid_attempts': 3},
                {'variant': 'unsupported_mechanism', 'paid_attempts': 2}]}
            path.write_text(json.dumps(previous), encoding='utf-8')
            out = io.StringIO()
            with redirect_stdout(out), patch('scripts.verify_revision_probe.build_roles', side_effect=AssertionError('No models')):
                focused_main(['--previous-summary', str(path)])
            followup = json.loads(out.getvalue())
            self.assertEqual(followup['prior_case_paid_attempts'], {
                'wording': 3, 'trigger_unknown': 3, 'unsupported_mechanism': 2})
            self.assertEqual(followup['remaining_case_paid_attempts'], {
                'wording': 5, 'trigger_unknown': 5, 'unsupported_mechanism': 6})
            self.assertTrue(followup['previous_summary']['sha256'])
            previous['paid_attempts'] = 7
            path.write_text(json.dumps(previous), encoding='utf-8')
            with self.assertRaises(ValueError):
                focused_main(['--previous-summary', str(path)])

    def test_plan_does_not_initialize_or_invoke_live_models(self):
        out = io.StringIO()
        with redirect_stdout(out), patch('scripts.verify_review_driven.build_roles', side_effect=AssertionError('No paid models')):
            self.assertEqual(main([]), 0)
        plan = json.loads(out.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['maximum_paid_attempts'], 50)
        self.assertEqual(plan['attempts_per_case_arm'], 1)
        self.assertFalse(plan['independent_quality_score'])

    def test_global_paid_cap_is_shared_and_failures_are_charged_and_retained(self):
        class Failure:
            def invoke(self, messages):
                raise ExecutionError('NETWORK_ERROR')
        budget = PaidBudget()
        model = RecordedModel(Failure(), budget)
        with self.assertRaises(ExecutionError):
            model.invoke([])
        self.assertEqual(budget.used, 1)
        self.assertEqual(model.replies, [{'error_code': 'NETWORK_ERROR'}])
        for _ in range(PAID_CAP - 1):
            budget.claim()
        with self.assertRaises(ExecutionError) as error:
            model.invoke([])
        self.assertEqual(error.exception.code, 'BUDGET_EXCEEDED')
        self.assertEqual(budget.used, PAID_CAP)
        self.assertEqual(model.attempts, 1)

    def test_followup_plan_carries_paid_count_and_rejects_inconsistent_summary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.json'
            previous = {'paid_attempts': 48, 'runs': [{'paid_attempts': 48}],
                        'plan': {'maximum_paid_attempts': 50, 'model': 'fixture-model'}}
            path.write_text(json.dumps(previous), encoding='utf-8')
            out = io.StringIO()
            with patch('scripts.verify_review_driven.IncidentConfig.from_file', return_value=IncidentConfig(model='fixture-model')):
                with redirect_stdout(out):
                    self.assertEqual(main(['--previous-summary', str(path)]), 0)
                plan = json.loads(out.getvalue())
                self.assertEqual(plan['remaining_paid_attempts'], 2)
                self.assertEqual(plan['prior_paid_attempts'], 48)
                self.assertTrue(plan['previous_summary']['sha256'])
                previous['paid_attempts'] = 47
                path.write_text(json.dumps(previous), encoding='utf-8')
                with self.assertRaises(ValueError):
                    main(['--previous-summary', str(path)])


if __name__ == '__main__':
    unittest.main()
