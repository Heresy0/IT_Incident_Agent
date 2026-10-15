"""Declarative modes and pure budget decisions, using the original run limits.

Evaluation never claims quota. RunContext remains the enforcement authority.
"""
from dataclasses import dataclass, replace
from typing import ClassVar

from runtime.context import Limits
from workflow.review_protection import DEFAULT_REVIEW_PROTECTION, MODES


@dataclass(frozen=True)
class BudgetView:
    limits: Limits
    model_calls: int
    tool_calls: int
    elapsed: float
    supervisor_calls: int
    task_count: int
    reworks: int
    additional_rework_authorized: bool = False


@dataclass(frozen=True)
class WorkflowPolicy:
    investigation_mode: str = 'programmatic'
    knowledge_mode: str = 'programmatic'
    reviewer_repair_mode: str = 'shared'
    review_protection: str = DEFAULT_REVIEW_PROTECTION
    parallel_collection: bool = True
    blind_review: bool = False
    orchestration: str = 'legacy'

    # Existing workflow bounds, not extra budgets or new runtime switches.
    replan_supervisor_limit: ClassVar[int] = 3
    max_parallel_roles: ClassVar[int] = 2
    collector_steps: ClassVar[int] = 4
    recursion_limit: ClassVar[int] = 40

    def __post_init__(self):
        if self.review_protection not in MODES:
            raise ValueError('Unregistered review protection mode')
        if self.investigation_mode not in {'agent', 'programmatic'}:
            raise ValueError('unregistered investigation mode')
        if self.knowledge_mode not in {'agent', 'programmatic'}:
            raise ValueError('unregistered knowledge mode')
        if self.reviewer_repair_mode not in {'shared', 'split'}:
            raise ValueError('unregistered reviewer repair mode')
        if self.orchestration not in {'legacy', 'review_driven'}:
            raise ValueError('unregistered orchestration mode')
        if self.orchestration == 'review_driven' and not self.planning_only:
            raise ValueError('review-driven orchestration requires programmatic collection')

    @property
    def review_driven(self):
        return self.orchestration == 'review_driven'

    def can_revise(self, view: BudgetView, *, revisions):
        judging = 2 + int(self.blind_review)
        return (revisions < 1 and view.model_calls + judging + view.limits.reserve_model_calls
                <= view.limits.model_calls
                and view.elapsed < view.limits.seconds - 30 * judging - view.limits.reserve_seconds)

    @property
    def planning_only(self):
        return self.investigation_mode == self.knowledge_mode == 'programmatic'

    @property
    def role_corrections(self):
        return self.planning_only and self.reviewer_repair_mode == 'shared'

    def supervisor_limit(self, additional=False):
        return 5 if additional else 4

    def task_limit(self, additional=False):
        return 7 if additional else 6

    def rework_limit(self, additional=False):
        return 2 if additional else 1

    def collection_reserve(self, limits, purpose):
        base = limits.reserve_model_calls
        if purpose != 'diagnosis':
            return base
        if self.review_driven:
            judging = 2 + int(self.blind_review)
            required = 2 * judging + 1
            return max(base, required) if limits.model_calls >= required + 1 else max(base, judging)
        required = 2 * (2 + int(self.blind_review)) + 2
        # Protect initial and revised judging stages plus planned collection;
        # tiny budgets retain the original allowance for initial finalization.
        return max(base, required) if limits.model_calls >= required + 3 else base

    def rework_budget(self, view: BudgetView, *, pending_model_calls=0,
                      tool_calls=1, supervisor_completed=False, additional=False, query_model_calls=0):
        limits = view.limits
        remaining = limits.model_calls - view.model_calls
        steps = {'pending_model_calls': pending_model_calls,
                 'supervisor': int(not supervisor_completed), 'interpretation': 1,
                 'finalization_reserve': max(2, limits.reserve_model_calls)}
        extended = additional or view.additional_rework_authorized
        if extended:
            steps['finalization_reserve'] = max(2 + int(self.blind_review), limits.reserve_model_calls)
            steps['correction_reserve'] = limits.reserve_model_calls
        needed = sum(steps.values())
        tools = limits.tool_calls - view.tool_calls
        model_turns = int(not supervisor_completed) + 3 + int(self.blind_review)
        # Retain the existing conservative time estimate for additional rework.
        time_required = 30 * model_turns + limits.reserve_seconds if extended else limits.reserve_seconds
        if self.review_driven:
            judging = 2 + int(self.blind_review)
            steps = {'pending_model_calls': pending_model_calls, 'supervisor': int(not supervisor_completed),
                     'query_models': query_model_calls, 'judging': judging,
                     'correction_reserve': limits.reserve_model_calls}
            needed = sum(steps.values())
            time_required = 30 * (pending_model_calls + int(not supervisor_completed) + judging + query_model_calls) + limits.reserve_seconds
        affordable = (remaining >= needed and tools >= tool_calls
            and view.supervisor_calls < self.supervisor_limit(extended)
            and view.task_count < self.task_limit(extended)
            and view.reworks < self.rework_limit(extended)
            and view.elapsed < limits.seconds - time_required)
        return {'can_collect': affordable, 'model_steps': steps, 'required_model_calls': needed,
                'remaining_model_calls': remaining, 'required_tool_calls': tool_calls, 'remaining_tools': tools}

    def dispatch_capacity(self, view: BudgetView, *, phase, purpose, pending_checks,
                          has_observations, pending_model_call=False):
        available = view.limits.model_calls - view.model_calls - int(pending_model_call)
        tools = view.limits.tool_calls - view.tool_calls
        if phase == 'rework':
            if self.review_driven:
                return int(tools >= pending_checks and available >= 2 + int(self.blind_review) + view.limits.reserve_model_calls)
            return int(tools >= pending_checks and available >= max(2, view.limits.reserve_model_calls) + 1)
        reserve = self.collection_reserve(view.limits, purpose)
        diagnosis = purpose == 'diagnosis'
        keep_tool = diagnosis and reserve > view.limits.reserve_model_calls and has_observations
        if self.review_driven:
            return max(0, min(self.max_parallel_roles, tools - int(keep_tool),
                (5 if diagnosis else 6) - view.task_count)) if available >= reserve else 0
        return max(0, min(self.max_parallel_roles, tools - int(keep_tool),
                         (available - reserve) // 2, (5 if diagnosis else 6) - view.task_count))

    def can_replan(self, view: BudgetView, *, dispatch_capacity):
        return (view.supervisor_calls < self.replan_supervisor_limit
            and view.task_count < self.task_limit() and dispatch_capacity > 0
            and view.elapsed < view.limits.seconds - view.limits.reserve_seconds)


def policy_property(name: str) -> property:
    """Legacy configuration writes replace and validate the immutable snapshot."""
    def read(coordinator):
        return getattr(coordinator.policy, name)

    def write(coordinator, value):
        coordinator.policy = replace(coordinator.policy, **{name: value})

    return property(read, write, doc=f'Alias of WorkflowPolicy.{name}.')
