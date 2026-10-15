"""Bounded collection with isolated branch ledgers and an explicit join result.

The runner owns task execution only. Installing branch ledgers, committing task
records and choosing the next workflow node remain Coordinator responsibilities.
"""
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from api.auth import Principal
from agents.contracts import TaskRequest
from agents.investigation import investigate
from agents.prompts import INVESTIGATION_TASK
from evidence.contracts import Evidence, EvidenceRef, InvestigationOutput
from evidence.diagnosis import ProtocolError
from runtime.branch import BranchContext
from runtime.context import RunContext, ExecutionError, activate
from tools.executor import ToolExecutor
from workflow.task_checks import completion

ExecutionMode = Literal['programmatic', 'agent']


@dataclass(frozen=True)
class CollectionRequest:
    task: TaskRequest
    objective: dict[str, Any]
    mode: ExecutionMode
    steps: int


@dataclass(frozen=True)
class CollectionOutcome:
    """Result and owned executor ledger; no writes to shared workflow state."""
    request: CollectionRequest
    result: dict[str, Any]
    executor: ToolExecutor

    @property
    def evidence(self) -> Mapping[str, Evidence]:
        return self.executor.evidence

    @property
    def references(self) -> Mapping[str, EvidenceRef]:
        return self.executor.references

    @property
    def query_results(self) -> Mapping:
        return self.executor.check_results

    @property
    def counts(self) -> Mapping[str, int]:
        return self.result['task_counts']

    @property
    def coverage(self) -> dict:
        return completion(self.request.objective['required_checks'], self.executor)


@dataclass(frozen=True)
class ParallelPlan:
    request: CollectionRequest
    context: BranchContext
    executor: ToolExecutor


@dataclass(frozen=True)
class ParallelBatch:
    batch_id: str
    plans: Mapping[str, ParallelPlan]


@dataclass(frozen=True)
class ParallelOutcome:
    batch_id: str
    collection: CollectionOutcome


@dataclass(frozen=True)
class CollectionJoin:
    collections: tuple[CollectionOutcome, ...]
    stop_codes: tuple[str, ...]

    @property
    def incomplete(self) -> bool:
        return bool(self.stop_codes)

    @property
    def fatal_code(self) -> str | None:
        return next((code for code in self.stop_codes if code in {
            'AUTH_ERROR', 'QUOTA_EXCEEDED', 'SCOPE_DENIED', 'TOOL_DENIED', 'WINDOW_DENIED'}), None)


class CollectionRunner:
    def __init__(self, models: Mapping[str, Any], provider, principal: Principal,
                 repairs: dict, *, modes: Mapping[str, ExecutionMode]):
        self.models, self.provider, self.principal = models, provider, principal
        self.repairs, self.modes = repairs, modes

    def collect(self, request: CollectionRequest, *, executor: ToolExecutor,
                context: RunContext, event_log: list[dict]) -> CollectionOutcome:
        if executor.context is not context or executor.role != request.task.role:
            raise ValueError('shared executor mismatch')
        before = dict(context.counts)
        role, objective = request.task.role, request.objective
        if request.mode == 'programmatic':
            result = execute_planned_reads(objective, executor, context, role=role)
        elif request.mode == 'agent':
            result = investigate(self.models[role], self.provider, self.principal, context=context,
                event_log=event_log, executor=executor, objective=objective,
                role=role, repair_state=self.repairs, max_steps=max(1, request.steps),
                approved_checks=objective['approved_checks'],
                system_prompt=INVESTIGATION_TASK if role == 'investigation' else None)
        else:
            raise ValueError('unregistered collection mode')
        result['task_counts'] = {key: context.counts[key] - before[key] for key in before}
        return CollectionOutcome(request, result, executor)

    def prepare_parallel(self, tasks: Sequence[TaskRequest], *, context: RunContext,
                         executors: Mapping[str, ToolExecutor], model_reserve: int,
                         objective_for: Callable[[TaskRequest], dict], model_cost_for=None) -> ParallelBatch:
        if len(tasks) != 2 or {task.role for task in tasks} != {'investigation', 'knowledge'}:
            raise ProtocolError('INVALID_TASK_ROUTE')
        with context.lock:
            available_models = context.limits.model_calls - model_reserve - context.counts['model_calls']
            available_tools = context.limits.tool_calls - context.counts['tool_calls']
        required_models = sum(model_cost_for(task) for task in tasks) if model_cost_for else 4
        required_tools = sum(len(task.checks) for task in tasks) if model_cost_for else 2
        if available_models < required_models or available_tools < required_tools or not context.can_research():
            raise ExecutionError('BUDGET_EXCEEDED')
        batch_id, plans = str(uuid4()), {}
        # Stable allocations/publication order; unused slices remain available later.
        ordered = sorted(tasks, key=lambda task: task.role != 'investigation')
        for index, task in enumerate(ordered):
            model_cap = model_cost_for(task) if model_cost_for else min(4, available_models // 2 + (available_models % 2 if index == 0 else 0))
            tool_cap = available_tools // 2 + (available_tools % 2 if index == 0 else 0)
            child = BranchContext(context, model_calls=model_cap, tool_calls=tool_cap,
                                  tool_only=bool(model_cost_for and not model_cap))
            request = CollectionRequest(task, deepcopy(objective_for(task)), self.modes[task.role], model_cap)
            plans[task.role] = ParallelPlan(request, child, executors[task.role].fork(child))
        return ParallelBatch(batch_id, plans)

    def collect_parallel(self, plan: ParallelPlan, batch_id: str) -> ParallelOutcome:
        role = plan.request.task.role
        child, executor = plan.context, plan.executor
        try:
            result = self.collect(plan.request, executor=executor, context=child, event_log=child.events).result
        except Exception as exc:
            code = exc.code if isinstance(exc, (ExecutionError, ProtocolError)) else 'INTERNAL_ERROR'
            child.error(ExecutionError(code), role)
            task_id = next((e['task_id'] for e in child.events if e['type'] == 'task_started'), str(uuid4()))
            result = {'task_id': task_id, 'status': 'failed', 'prompt_version': 'parallel_branch_failure',
                'output': InvestigationOutput(missing_information=[f'{role}任务未完成（{code}）；保留已取得证据。']).model_dump(mode='json'),
                'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
                'task_counts': dict(child.counts), 'run_summary': {**child.summary(), 'termination_reason': code}}
            child.emit({'type': 'task_completed', 'role': role, 'task_id': task_id, 'status': 'failed', 'reason': code})
        return ParallelOutcome(batch_id, CollectionOutcome(plan.request, result, executor))


def join_parallel(batch_id: str, plans: Mapping[str, ParallelPlan],
                  outcomes: Mapping[str, ParallelOutcome]) -> CollectionJoin:
    """Validate every branch before any shared state commit; retain stable role order."""
    if set(outcomes) != set(plans) or any(
            not isinstance(outcome, ParallelOutcome) or outcome.batch_id != batch_id
            for outcome in outcomes.values()):
        raise ProtocolError('INVALID_TASK_ROUTE')
    collections, stops = [], []
    for role, plan in plans.items():
        collected = outcomes[role].collection
        if collected.request is not plan.request or collected.executor is not plan.executor:
            raise ProtocolError('INVALID_TASK_ROUTE')
        collections.append(collected)
        if collected.result['status'] != 'completed':
            stops.append(collected.result['run_summary']['termination_reason'])
    return CollectionJoin(tuple(collections), tuple(stops))


def task_record(outcome: CollectionOutcome, challenge_id: str | None) -> dict:
    task, objective, result = outcome.request.task, outcome.request.objective, outcome.result
    return {"task_id": result["task_id"], "role": task.role, "goal": task.goal,
        'need_ids': objective['need_ids'],
        "expected_value": objective['expected_value'],
        "challenge_id": challenge_id,
        "status": result["status"], "execution_status": result['status'], "output": result["output"],
        "required_checks": objective['required_checks'],
        "model_calls": result['task_counts']['model_calls'],
        "tool_calls": result['task_counts']['tool_calls'],
        "prompt_version": result["prompt_version"], "termination_reason": result["run_summary"]["termination_reason"],
        "evidence_ids": [e["evidence_id"] for e in result["evidence"]]}


def execute_planned_reads(objective: dict, executor: ToolExecutor, context: RunContext, *,
                          role: str = 'investigation') -> dict:
    """Execute validated planned reads; Diagnosis owns evidence interpretation."""
    task_id, stop = str(uuid4()), ''
    checks = objective['required_checks']
    with activate(context), context.span('task', role) as span:
        def event(kind, **fields):
            context.emit({'type': kind, 'role': role, 'task_id': task_id,
                          'span_id': span['span_id'], 'parent_span_id': span['parent_span_id'], **fields})
        event('task_created', execution_mode='programmatic')
        event('task_started')
        for check in checks:
            if objective.get('channel_recovery_policy'):
                from workflow.collection_recovery import channel_failure
                failure = channel_failure(executor, check['tool'])
                if failure:
                    event('planned_check_channel_blocked', name=check['tool'],
                          query_profile=check['args'], error_code=failure['error'],
                          status='not_executed', source_args=failure['args'])
                    continue
            # Do not automatically retry failures/truncation or re-read a superset.
            if completion([check], executor)['checks'][0]['status'] != 'not_executed':
                event('planned_check_reused', name=check['tool'])
                continue
            if not context.can_research():
                stop = 'BUDGET_EXCEEDED'
                break
            event('tool_selected', name=check['tool'], execution_source=objective.get('execution_source', 'validated_supervisor_plan'))
            result = executor.execute(check['tool'], check['args'])
            event('tool_result', name=check['tool'], status=result.status,
                  evidence_ids=[e.evidence_id for e in result.evidence],
                  query_profile=dict(executor.last_query_profile), sample_order=result.sample_order,
                  truncated=result.truncated, error=result.error.model_dump() if result.error else None,
                  execution_source=objective.get('execution_source', 'validated_supervisor_plan'))
            if result.error and result.error.code == 'BUDGET_EXCEEDED':
                stop = 'BUDGET_EXCEEDED'
                break
        coverage = completion(checks, executor)
        gaps = [*coverage['missing_information'], *coverage['limitations']]
        if not checks:
            gaps.append('No validated concrete checks were supplied; the program cannot execute a goal alone.')
            stop = 'NO_TOOL_PROGRESS'
        status = 'partial' if stop or coverage['missing_information'] or not checks else 'completed'
        event('task_completed', status=status, cause_confirmation='not_performed')
    return {'task_id': task_id, 'status': status, 'prompt_version': 'programmatic_reads_v1',
            'output': InvestigationOutput(findings=[], tentative_hypotheses=[],
                missing_information=gaps[:8], escalation_team=None).model_dump(mode='json'),
            'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
            'run_summary': {**context.summary(), 'termination_reason': stop or 'FINISHED'}}
