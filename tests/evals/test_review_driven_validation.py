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
    def test_reviewer_followup_plan_is_fresh_prioritized_and_reserves_final_review(self):
        from scripts.verify_revision_probe import main as focused_main
        out = io.StringIO()
        with redirect_stdout(out), patch('scripts.verify_revision_probe.build_roles', side_effect=AssertionError('No models')):
            focused_main(['--validation-session', 'reviewer_contract_followup'])
        plan = json.loads(out.getvalue())
        self.assertEqual(plan['paid_cap_per_case'], 12)
        self.assertEqual(plan['maximum_paid_attempts'], 36)
        self.assertEqual(plan['final_review_reserve_per_case'], 2)
        self.assertEqual(plan['execution_order'][0], 'unsupported_mechanism')
        self.assertIsNone(plan['replayed_initial_revision'])
        self.assertEqual(plan['limits']['model_calls'], 16)

    def test_nonfinal_calls_cannot_consume_terminal_paid_allowance(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        class Live:
            def invoke(self, messages):
                return AIMessage(content='reply')
        budget = PaidBudget(maximum=12, final_review_reserve=2)
        for _ in range(10): budget.claim()
        reviewer = RecordedModel(Live(), budget, role='reviewer')
        diagnosis = RecordedModel(Live(), budget, role='diagnosis')
        def messages(final, concerns):
            return [SystemMessage(content='test'), HumanMessage(content=json.dumps({'input': {
                'review_round': {'draft_revision': 3, 'final_candidate': final}, 'revision_concerns': concerns}}))]
        with self.assertRaises(ExecutionError): diagnosis.invoke(messages(True, []))
        with self.assertRaises(ExecutionError): reviewer.invoke(messages(True, [{'target_id': 'H1'}]))
        with self.assertRaises(ExecutionError): reviewer.invoke(messages(False, []))
        self.assertEqual(budget.used, 10)
        reviewer.invoke(messages(True, []))
        reviewer.invoke(messages(True, []))
        self.assertEqual(reviewer.final_review_attempts, 2)
        self.assertEqual(budget.used, 12)
        with self.assertRaises(ExecutionError): reviewer.invoke(messages(True, []))

    def test_final_review_metric_cannot_count_review_of_previous_draft(self):
        from scripts.verify_revision_probe import control_checks
        draft = {'findings': [], 'hypotheses': [], 'recommended_actions': [{'action': 'neutral check'}]}
        report = {'drafts': [{'revision': index, 'draft': draft} for index in (1, 2, 3)],
                  'reviews': [{'revision': index} for index in (1, 2)]}
        self.assertFalse(control_checks(report)['final_review_completed'])
        report['reviews'].append({'revision': 3})
        self.assertTrue(control_checks(report)['final_review_completed'])

    def test_paid_reply_replay_is_not_new_inference_and_later_turns_are_charged(self):
        from scripts.verify_revision_probe import ReplayInitialRevision
        from langchain_core.messages import AIMessage
        class Live:
            def invoke(self, messages):
                return AIMessage(content='new live response')
        budget = PaidBudget(maximum=8)
        recorded = RecordedModel(Live(), budget)
        replay = ReplayInitialRevision(recorded, 'retained exact paid response')
        self.assertEqual(replay.invoke([]).content, 'retained exact paid response')
        self.assertEqual(budget.used, 0)
        self.assertEqual(recorded.replies, [])
        self.assertEqual(replay.invoke([]).content, 'new live response')
        self.assertEqual(budget.used, 1)
        self.assertEqual(recorded.attempts, 1)

    def test_new_scope_session_plan_stays_free_and_cannot_relabel_previous_session(self):
        from scripts.verify_revision_probe import main as focused_main
        out = io.StringIO()
        with redirect_stdout(out), patch('scripts.verify_revision_probe.build_roles', side_effect=AssertionError('No models')):
            focused_main(['--validation-session', 'scope_conflict_followup'])
        plan = json.loads(out.getvalue())
        self.assertEqual(plan['validation_session'], 'scope_conflict_followup')
        self.assertEqual(plan['maximum_paid_attempts'], 24)
        self.assertEqual(sum(plan['prior_case_paid_attempts'].values()), 0)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.json'
            path.write_text(json.dumps({'plan': plan}), encoding='utf-8')
            with self.assertRaises(ValueError):
                focused_main(['--previous-summary', str(path)])

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
