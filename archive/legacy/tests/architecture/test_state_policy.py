"""Run isolation, compatibility ownership and original policy boundaries."""
from dataclasses import FrozenInstanceError, replace
import unittest

from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from runtime.context import Limits, RunContext
from tests.tools.test_incident_tools import P
from workflow.coordinator import Collaboration
from workflow.policy import BudgetView, WorkflowPolicy
from workflow.state import WorkflowState


def coordinator(**options):
    return Collaboration(scripted_models(), FixtureProvider('case_001', P), P, **options)


class WorkflowStateTests(unittest.TestCase):
    def test_runs_do_not_share_mutable_business_or_audit_state(self):
        first, second = coordinator(), coordinator()
        first.phase = 'rework'
        first.pending = {'challenge_id': 'C1', 'checks': []}
        first.tasks.append({'task_id': 'T1'})
        first.references['R1'] = object()
        first.parallel_plans['knowledge'] = object()
        first.extra_gaps.append('Source unavailable')
        first.repairs['repairs'] += 1
        first.event('test_event')
        self.assertEqual(second.phase, 'collect')
        self.assertIsNone(second.pending)
        self.assertEqual(second.tasks, [])
        self.assertEqual(second.references, {})
        self.assertEqual(second.parallel_plans, {})
        self.assertEqual(second.extra_gaps, [])
        self.assertEqual(second.repairs, {'repairs': 0})
        self.assertEqual(second.events, [])
        self.assertIsNot(first.evidence_needs['N1'], second.evidence_needs['N1'])

    def test_legacy_reads_replacements_and_mutation_use_one_owner(self):
        c = coordinator()
        tasks, pending, events = [], {'checks': []}, []
        c.tasks, c.pending, c.events = tasks, pending, events
        self.assertIs(c.state.collection.tasks, tasks)
        self.assertIs(c.state.control.pending, pending)
        self.assertIs(c.state.audit.events, events)
        c.state.control.reworks = 1
        self.assertEqual(c.reworks, 1)
        c.pending['checks'].append('check')
        self.assertEqual(c.state.control.pending['checks'], ['check'])
        for name in ('tasks', 'pending', 'reworks', 'events', 'draft', 'repairs'):
            self.assertNotIn(name, vars(c))

    def test_shared_runtime_retains_event_log_identity(self):
        provider = FixtureProvider('case_001', P)
        events = [{'type': 'existing', 'seq': 1}]
        context = RunContext(scope=provider.ticket().scope, emit=events.append)
        c = Collaboration(scripted_models(), provider, P, context=context, event_log=events)
        self.assertIs(c.context, context)
        self.assertIs(c.state.audit.events, events)
        c.event('next_event')
        self.assertEqual([e['type'] for e in events], ['existing', 'next_event'])
        self.assertIs(c.executors['investigation'].context, context)
        self.assertNotIn('counts', vars(c.state))

    def test_graph_projection_is_detached_and_stop_routes_to_finish(self):
        state = WorkflowState()
        state.collection.evidence['EV1'] = object()
        state.collection.tasks.append({'task_id': 'T1'})
        state.diagnosis.drafts.append({'revision': 1})
        state.control.reworks = 1
        counts = {'model_calls': 3, 'tool_calls': 2}
        snapshot = state.graph_projection(counts)
        self.assertEqual(snapshot['task_count'], 1)
        self.assertEqual(snapshot['draft_revision'], 1)
        self.assertEqual(snapshot['rework_rounds'], 1)
        snapshot['evidence_ids'].clear()
        snapshot['counts']['tool_calls'] = 99
        self.assertEqual(list(state.collection.evidence), ['EV1'])
        self.assertEqual(counts['tool_calls'], 2)
        state.control.stop = 'BUDGET_EXCEEDED'
        self.assertEqual(state.graph_projection(counts)['next_node'], 'finish')
        self.assertEqual(state.control.next_node, 'supervisor')

    def test_budget_view_reads_live_runtime_instead_of_stale_state(self):
        c = coordinator()
        initial = c.budget_view()
        with c.context.lock:
            c.context.counts['model_calls'] = 4
        updated = c.budget_view()
        self.assertEqual(initial.model_calls, 0)
        self.assertEqual(updated.model_calls, 4)
        self.assertEqual(c.graph_snapshot()['counts']['model_calls'], 4)
        self.assertEqual(c.context.counts['model_calls'], 4)


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = WorkflowPolicy()
        self.view = BudgetView(Limits(model_calls=32, tool_calls=8, seconds=300),
                               0, 0, 10, 0, 0, 0)

    def test_default_resolution_keeps_existing_modes_and_blind_review(self):
        c = coordinator(investigation_mode='agent')
        self.assertEqual(c.knowledge_mode, 'agent')
        self.assertFalse(c.planning_only)
        self.assertFalse(c.role_corrections)
        self.assertEqual(c.blind_review, getattr(c.models['reviewer'], 'blind_review', False))
        c = coordinator(investigation_mode='agent', knowledge_mode='programmatic', blind_review=True)
        self.assertTrue(c.blind_review)
        self.assertFalse(c.planning_only)
        self.assertTrue(coordinator().role_corrections)
        self.assertFalse(coordinator(reviewer_repair_mode='split').role_corrections)

    def test_compatibility_update_replaces_frozen_policy_and_recomputes_modes(self):
        c = coordinator()
        previous = c.policy
        with self.assertRaises(FrozenInstanceError):
            previous.blind_review = True
        c.blind_review = True
        self.assertTrue(c.policy.blind_review)
        self.assertFalse(previous.blind_review)
        c.knowledge_mode = 'agent'
        self.assertFalse(c.planning_only)
        self.assertFalse(c.role_corrections)
        current = c.policy
        with self.assertRaises(ValueError):
            c.review_protection = 'unknown_mode'
        self.assertIs(c.policy, current)

    def test_unregistered_modes_are_rejected_before_collection(self):
        for name in ('investigation_mode', 'knowledge_mode', 'reviewer_repair_mode', 'review_protection'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                WorkflowPolicy(**{name: 'unknown_mode'})

    def test_basic_rework_preserves_finalization_and_pending_call_reserve(self):
        view = replace(self.view, model_calls=28)
        result = self.policy.rework_budget(view)
        self.assertTrue(result['can_collect'])
        self.assertEqual(result['required_model_calls'], 4)
        self.assertFalse(self.policy.rework_budget(view, pending_model_calls=1)['can_collect'])
        completed = self.policy.rework_budget(view, supervisor_completed=True)
        self.assertEqual(completed['required_model_calls'], 3)
        self.assertEqual(completed['model_steps']['supervisor'], 0)

    def test_each_original_budget_dimension_can_deny_rework(self):
        self.assertTrue(self.policy.rework_budget(self.view)['can_collect'])
        for changes in ({'model_calls': 29}, {'tool_calls': 8}, {'supervisor_calls': 4},
                        {'task_count': 6}, {'reworks': 1}, {'elapsed': 285}):
            with self.subTest(changes=changes):
                result = self.policy.rework_budget(replace(self.view, **changes))
                self.assertFalse(result['can_collect'])
        self.assertTrue(self.policy.rework_budget(replace(self.view, elapsed=284.9))['can_collect'])

    def test_additional_rework_keeps_original_cap_and_blind_time_reserve(self):
        policy = replace(self.policy, blind_review=True)
        view = replace(self.view, supervisor_calls=4, task_count=6, reworks=1,
                       additional_rework_authorized=True, model_calls=25)
        result = policy.rework_budget(view)
        self.assertTrue(result['can_collect'])
        self.assertEqual(result['required_model_calls'], 7)
        self.assertEqual(result['remaining_model_calls'], 7)
        self.assertFalse(policy.rework_budget(replace(view, model_calls=26))['can_collect'])
        self.assertFalse(policy.rework_budget(replace(view, elapsed=135))['can_collect'])
        for changes in ({'supervisor_calls': 5}, {'task_count': 7}, {'reworks': 2}):
            with self.subTest(changes=changes):
                self.assertFalse(policy.rework_budget(replace(view, **changes))['can_collect'])
        self.assertEqual(view.limits.model_calls, 32)

    def test_collection_reserve_preserves_tiny_and_status_check_budgets(self):
        self.assertEqual(self.policy.collection_reserve(Limits(model_calls=8), 'diagnosis'), 2)
        self.assertEqual(self.policy.collection_reserve(Limits(model_calls=9), 'diagnosis'), 6)
        blind = replace(self.policy, blind_review=True)
        self.assertEqual(blind.collection_reserve(Limits(model_calls=10), 'diagnosis'), 2)
        self.assertEqual(blind.collection_reserve(Limits(model_calls=11), 'diagnosis'), 8)
        self.assertEqual(blind.collection_reserve(Limits(model_calls=16), 'status_check'), 2)

    def test_dispatch_protects_finalization_and_one_future_read(self):
        view = replace(self.view, limits=Limits(model_calls=16, tool_calls=2))
        options = dict(phase='collect', purpose='diagnosis', pending_checks=0)
        self.assertEqual(self.policy.dispatch_capacity(view, has_observations=False, **options), 2)
        self.assertEqual(self.policy.dispatch_capacity(view, has_observations=True, **options), 1)
        self.assertEqual(self.policy.dispatch_capacity(replace(view, task_count=5),
            has_observations=False, **options), 0)
        self.assertEqual(self.policy.dispatch_capacity(view, phase='collect', purpose='status_check',
            pending_checks=0, has_observations=True), 2)
        self.assertEqual(self.policy.dispatch_capacity(view, phase='rework', purpose='diagnosis',
            pending_checks=3, has_observations=True), 0)

    def test_policy_decisions_do_not_consume_runtime_quota(self):
        c = coordinator()
        counts = dict(c.context.counts)
        for _ in range(3):
            c.rework_budget()
            c.dispatch_capacity()
            c.can_replan()
        self.assertEqual(c.context.counts, counts)
        self.assertEqual(c.events, [])


if __name__ == '__main__':
    unittest.main()
