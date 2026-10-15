"""Validate proposed plans before dispatch; never change workflow state or run tools."""
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agents.contracts import EvidenceNeed, SupervisorDecision, SupervisorSelection
from evidence.contracts import Evidence, EvidenceRef
from evidence.diagnosis import ProtocolError
from evidence.ownership import resolve_owner
from runtime.context import ExecutionError
from workflow.evidence_needs import update_needs, apply_need_updates
from workflow.task_checks import completion, adds_information


@dataclass(frozen=True)
class PlanningContext:
    """Read-only-by-contract inputs; query validation may inspect executor ledgers."""
    phase: str
    purpose: str
    evidence: Mapping[str, Evidence]
    references: Mapping[str, EvidenceRef]
    evidence_needs: Mapping[str, EvidenceNeed]
    executors: Mapping[str, Any]
    investigation_mode: str
    knowledge_mode: str
    planning_only: bool
    reworks: int
    additional_rework_authorized: bool
    pending: Mapping[str, Any] | None
    selector_diagnostics: bool = False


def validate_plan(selection: SupervisorSelection, view: PlanningContext, *,
                  emit: Callable[..., None], can_collect: Callable[[int], bool]) -> SupervisorDecision:
    # Explicit non-dispatch actions never execute attached tasks.
    if selection.action != 'dispatch' and selection.tasks:
        emit('supervisor_fields_ignored', code='UNEXPECTED_TASKS',
                   action=selection.action, path='tasks', ignored_count=len(selection.tasks))
        selection = selection.model_copy(update={'tasks': []})
    if selection.action == 'dispatch' and not selection.tasks:
        raise ProtocolError('TASK_LIMIT', [{'path': 'tasks',
            'instruction': 'dispatch须提供一到两个有效任务；没有新检查时选择适用的收尾动作并使用tasks=[]。'}])
    if view.phase == 'rework' and selection.action == 'dispatch' and len(selection.tasks) != 1:
        raise ProtocolError('INVALID_REWORK_DISPATCH', [{'path': 'tasks', 'max_items': 1,
            'instruction': '补查只允许一个任务；选择最有区分价值的角色和检查，无法补查则request_info/escalate。'}])
    if selection.evidence_needs and (selection.need_updates or selection.additional_needs):
        raise ProtocolError('CONFLICTING_NEED_FORMATS', [{'path': 'need_updates',
            'instruction': '使用need_updates局部更新；不要同时提供旧版evidence_needs清单。'}])
    possible_needs = set(view.evidence_needs) | {n.need_id for n in
        [*selection.evidence_needs, *selection.additional_needs]}
    tasks, redundant_checks = [], []
    for task_index, task in enumerate(selection.tasks):
        if (len(task.evidence_ids) != len(set(task.evidence_ids))
                or any(eid not in view.evidence for eid in task.evidence_ids)):
            raise ProtocolError("UNKNOWN_OR_DUPLICATE_EVIDENCE")
        if len(task.need_ids) != len(set(task.need_ids)) or not set(task.need_ids) <= possible_needs:
            raise ProtocolError('UNKNOWN_OR_DUPLICATE_NEED')
        mode = view.investigation_mode if task.role == 'investigation' else view.knowledge_mode
        if mode == 'programmatic' and not task.checks:
            raise ProtocolError('INVALID_TASK_CHECK', [{'path': 'tasks.checks',
                'instruction': '程序取证需要明确的checks；给出登记工具与精确参数，无法规划则request_info或escalate。'}])
        normalized, seen = [], set()
        for check_index, check in enumerate(task.checks):
            try:
                args = view.executors[task.role].validate_args(check.tool, check.args)
            except ExecutionError as exc:
                raise ProtocolError('INVALID_TASK_CHECK', [{'code': exc.code,
                    'path': f'tasks.{task_index}.checks.{check_index}.args', 'tool': check.tool,
                    'role': task.role,
                    'allowed_tools': [s['function']['name'] for s in view.executors[task.role].schemas()],
                    'instruction': '按read_only_tools[role]选工具；start/end复制ticket.scope窗口或在其内缩小，不能为对照外扩。无法查询则保留缺口。',
                    **({'argument_errors': view.executors[task.role].argument_errors(check.tool, check.args)}
                       if view.selector_diagnostics else {})}]) from None
            key = (check.tool, args.model_dump_json())
            if key in seen:
                raise ProtocolError('DUPLICATE_TASK_CHECK')
            seen.add(key)
            normalized.append(check.model_copy(update={'args': args.model_dump(mode='json')}))
        if view.phase == 'rework':
            if not normalized or not task.expected_value:
                raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'READ_ONLY_CHECK_REQUIRED',
                    'instruction': 'Supervisor须规划checks与expected_value；不可补查时request_info或escalate。'}])
            if view.investigation_mode == 'programmatic':
                fresh = []
                for check in normalized:
                    state = completion([check.model_dump(mode='json')], view.executors[task.role])['checks'][0]
                    if state['status'] != 'not_executed' or not adds_information(
                            [check.model_dump(mode='json')], view.executors[task.role]):
                        redundant_checks.append({'role': task.role, 'tool': check.tool, 'status': state['status']})
                    else:
                        fresh.append(check)
                normalized = fresh
                if not normalized:
                    continue
            if any(not adds_information([c.model_dump(mode='json')], view.executors[task.role]) for c in normalized):
                raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'DUPLICATE_TOOL',
                    'instruction': '补查没有新增覆盖；换有价值的新查询，或保留缺口收尾。'}])
            if view.reworks == 1 and view.additional_rework_authorized:
                if (view.pending.get('need_id') not in task.need_ids or
                        any(completion([c.model_dump(mode='json')], view.executors[task.role])['checks'][0]['status']
                            != 'not_executed' for c in normalized)):
                    raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'ADDITIONAL_CHECK_NOT_NEW',
                        'instruction': '追加补查须关联被质疑的问题，且仅使用未尝试的新检查；不重试失败、截断或已覆盖查询。'}])
            if not can_collect(len(normalized)):
                raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'BUDGET_EXCEEDED',
                    'instruction': '预算不足以完整执行补查并收尾；减少检查或保留限制。'}])
        tasks.append(task.model_copy(update={'checks': normalized}))
    # Validate executable proposals first; a recoverable ledger mistake
    # must not hide an unknown source or an out-of-scope tool proposal.
    needs = (update_needs(view.evidence_needs, selection.evidence_needs,
                view.evidence, view.executors, view.purpose) if selection.evidence_needs
             else apply_need_updates(view.evidence_needs, selection.need_updates, selection.additional_needs,
                view.evidence, view.executors, view.purpose))
    if view.planning_only:
        if selection.need_updates or selection.evidence_needs:
            emit('supervisor_assessments_ignored', count=len(selection.need_updates) + len(selection.evidence_needs),
                       reason='planning_only', verdicts_changed=False)
        # Compatibility fields were validated above but cannot replace the
        # existing review-owned assessments. New questions start pending.
        needs = {key: view.evidence_needs.get(key, value.model_copy(update={
            'status': 'pending', 'evidence_ids': [], 'blocking': None})) for key, value in needs.items()}
    for update in selection.need_updates:
        if update.blocking is False and needs[update.need_id].purpose in {'symptom', 'mechanism'}:
            emit('supervisor_fields_ignored', code='CORE_BLOCKING_RETAINED',
                       need_id=update.need_id, path='blocking')
    if redundant_checks:
        emit('planned_checks_reused', checks=redundant_checks, executed=False)
        if selection.action == 'dispatch' and not tasks:
            # An exhausted plan can hand off uncertainty, never approve a draft.
            selection = selection.model_copy(update={'action': 'request_info',
                'reason': '程序台账：补查计划全部已尝试或已覆盖，没有新查询；保留证据缺口供人工核实。',
                'missing_information': list(dict.fromkeys([*selection.missing_information,
                    view.pending['missing_observation']]))[:8]})
    return SupervisorDecision(**selection.model_dump(exclude={'escalation_ref', 'tasks', 'evidence_needs',
                                                             'need_updates', 'additional_needs'}),
                              tasks=tasks, evidence_needs=list(needs.values()),
                              escalation_team=resolve_owner(selection.escalation_ref, view))
