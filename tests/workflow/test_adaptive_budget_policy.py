"""Adaptive admission protects closing work without granting additional calls."""
import time
import unittest

from runtime.context import ExecutionError, Limits, RunContext
from workflow.v2.policy import (can_collect, collection_reservation,
                                collect_stop_reason, remaining, stage_quote)


class AdaptiveBudgetPolicyTests(unittest.TestCase):
    def context(self, *, models=12, tools=8, seconds=180):
        return RunContext(limits=Limits(model_calls=models, tool_calls=tools,
                                       seconds=seconds))

    def test_quotes_contain_only_actual_pending_stages(self):
        self.assertEqual(stage_quote().stages, ('draft', 'review'))
        self.assertEqual(stage_quote(phase='rework', enable_challenge=True).stages,
                         ('revision', 'review', 'challenge'))
        self.assertEqual(stage_quote(enable_challenge=True,
                                    correction_needed=True).models, 4)
        with self.assertRaises(ValueError):
            stage_quote(phase='speculative_extra_round')

    def test_larger_caps_keep_a_monotone_legal_read_prefix_and_closing_capacity(self):
        for challenge in (False, True):
            reads = []
            for cap in range(3, 17):
                context = self.context(models=cap, tools=24)
                reserve = collection_reservation(context, enable_challenge=challenge)
                count = 0
                while can_collect(context, reserve_models=reserve):
                    context.reserve('model', terminal=True)
                    context.reserve('tool')
                    count += 1
                reads.append(count)
                self.assertGreaterEqual(count, 1)
                self.assertEqual(remaining(context).models, reserve)
                self.assertEqual(collect_stop_reason(context, reserve_models=reserve),
                                 'COLLECTION_FINALIZATION_RESERVED')
                self.assertEqual(context.counts['model_calls'], count)
            self.assertEqual(reads, sorted(reads))

    def test_rework_protects_all_checks_even_at_a_small_run_cap(self):
        context = self.context(models=3)
        reserve = collection_reservation(context, phase='rework', enable_challenge=True)
        self.assertEqual(reserve, 3)
        self.assertFalse(can_collect(context, reserve_models=reserve))
        self.assertEqual(remaining(context).models, 3)

    def test_rejected_proposals_consume_global_calls_but_not_novel_reads(self):
        context = self.context(models=12)
        reserve = collection_reservation(context, phase='rework', enable_challenge=True)
        for no_progress in (1, 2):
            context.reserve('model', terminal=True)
            self.assertTrue(can_collect(context, reserve_models=reserve,
                                        no_progress=no_progress, max_new_reads=2))
        context.reserve('model', terminal=True)
        self.assertEqual(collect_stop_reason(context, reserve_models=reserve,
                         no_progress=3, max_new_reads=2), 'NO_TOOL_PROGRESS')
        self.assertEqual(context.counts['model_calls'], 3)
        self.assertEqual(context.counts['tool_calls'], 0)

    def test_two_novel_reads_stop_even_if_they_return_no_observations(self):
        context = self.context(models=12)
        for _ in range(2):
            context.reserve('model', terminal=True)
            context.reserve('tool')
        self.assertEqual(collect_stop_reason(context, reserve_models=3,
                         new_reads=2, max_new_reads=2), 'COLLECTION_READ_LIMIT_REACHED')
        self.assertEqual(context.counts['tool_calls'], 2)
        self.assertEqual(remaining(context).models, 10)

    def test_one_empty_read_then_two_duplicates_can_still_choose_a_new_channel(self):
        context = self.context(models=12)
        for _ in range(3):
            context.reserve('model', terminal=True)
        context.reserve('tool')
        self.assertTrue(can_collect(context, reserve_models=3, new_reads=1,
                                    max_new_reads=2, no_progress=2))
        context.reserve('model', terminal=True)
        context.reserve('tool')
        self.assertEqual(collect_stop_reason(context, reserve_models=3, new_reads=2,
                         max_new_reads=2, no_progress=0), 'COLLECTION_READ_LIMIT_REACHED')

    def test_quotes_cannot_expand_global_model_or_tool_budgets(self):
        context = self.context(models=3, tools=1)
        initial = dict(context.counts)
        collection_reservation(context, enable_challenge=True)
        stage_quote(phase='rework', enable_challenge=True)
        self.assertEqual(context.counts, initial)
        context.reserve('tool')
        self.assertEqual(collect_stop_reason(context, reserve_models=2),
                         'TOOL_BUDGET_EXHAUSTED')
        for _ in range(3):
            context.reserve('model', terminal=True)
        self.assertEqual(collect_stop_reason(context, reserve_models=2),
                         'MODEL_BUDGET_EXHAUSTED')
        with self.assertRaises(ExecutionError):
            context.reserve('model', terminal=True)
        self.assertEqual(context.counts['model_calls'], 3)

    def test_time_reservation_preserves_the_existing_hard_deadline(self):
        context = self.context(seconds=30)
        context.started = time.monotonic() - 16
        self.assertEqual(collect_stop_reason(context, reserve_models=2),
                         'COLLECTION_TIME_RESERVED')
        self.assertGreater(remaining(context).seconds, 0)
        context.started = time.monotonic() - 31
        self.assertEqual(collect_stop_reason(context, reserve_models=2),
                         'TIME_BUDGET_EXCEEDED')
        self.assertEqual(context.limits.seconds, 30)


if __name__ == '__main__':
    unittest.main()
