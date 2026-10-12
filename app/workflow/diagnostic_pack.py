"""Opt-in, service-scoped baseline reads. Never selects or confirms a cause."""
import hashlib
import json
import time
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, model_validator
from agents.contracts import TaskRequest
from runtime.context import ExecutionError
from workflow.task_checks import completion

POLICY = 'service_baseline_reads_experiment_v1'
STATE_POLICY = 'diagnostic_pack_read_ledger_v2'
EXIT_POLICY = 'diagnostic_pack_exit_v1'
UNAVAILABLE = {'SOURCE_NOT_CONFIGURED', 'LOG_CATEGORY_NOT_REGISTERED', 'METRIC_NOT_REGISTERED'}


class PackCheck(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    check_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    tool: str
    args: dict
    human_check: str = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def baseline_parameters(self):
        allowed = {'get_service_metrics': {'metrics', 'granularity'},
                   'get_service_logs': {'category'}, 'get_recent_changes': {'category'}}
        if self.tool not in allowed or set(self.args) - allowed[self.tool]:
            raise ValueError('Only baseline metrics/logs/changes, without caller-supplied window or filters')
        if self.tool == 'get_service_metrics':
            metrics = self.args.get('metrics')
            if not isinstance(metrics, list) or not 1 <= len(metrics) <= 3:
                raise ValueError('A baseline metric check requires 1..3 registered metrics')
        return self


class DiagnosticPack(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    pack_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    service: str = Field(min_length=1, max_length=80)
    environment: str
    service_version: str = Field(min_length=1, max_length=40)
    handoff_role: str = Field(default='服务值班人员', min_length=1, max_length=80)
    checks: list[PackCheck] = Field(min_length=1, max_length=2)

    @model_validator(mode='after')
    def distinct_checks(self):
        if len({c.check_id for c in self.checks}) != len(self.checks):
            raise ValueError('Pack check IDs must be unique')
        if len({json.dumps([c.tool, c.args], sort_keys=True) for c in self.checks}) != len(self.checks):
            raise ValueError('Pack reads must be distinct')
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()

    def assert_scope(self, ticket):
        scope = ticket.scope
        if (self.service, self.environment, self.service_version) != (
                scope.service, scope.environment, scope.service_version):
            raise ValueError('Diagnostic pack does not match the authorized service/environment/version')


def collect(c):
    """Use the original executor, ledger, task cap and global budgets exactly once."""
    pack = c.diagnostic_pack
    if c.diagnostic_pack_result is not None:
        return  # No automatic replay, including failed or deferred reads.
    summary = {'policy': POLICY, 'pack_id': pack.pack_id, 'fingerprint': pack.fingerprint,
               'status': 'preflight',
               'state_policy': STATE_POLICY, 'revision': 0,
               'goal_verified': False, 'mechanism_confirmed': False,
               'checks': [], 'handoff': [], 'model_calls': 0, 'tool_calls': 0}
    c.diagnostic_pack_result = summary
    if c.ticket.purpose != 'diagnosis':
        summary['status'] = 'skipped_status_check'
        c.event('diagnostic_pack_skipped', reason='status_check', pack_id=pack.pack_id)
        return
    executor = c.executors['investigation']
    window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
    available, metadata = [], []
    # Preflight all queries before any external read; scope/argument errors never
    # get hidden behind an earlier successful query. Missing channels are gaps.
    for entry in pack.checks:
        check = {'tool': entry.tool, 'args': {**window, **entry.args}}
        try:
            args = executor.validate_args(check['tool'], check['args']).model_dump(mode='json')
            check['args'] = args
            available.append(check)
            metadata.append((entry, check, None))
        except ExecutionError as exc:
            if exc.code not in UNAVAILABLE:
                summary['status'] = 'invalid_configuration'
                summary['error'] = exc.code
                raise
            metadata.append((entry, check, exc.code))
    new_reads = [check for check in available
                 if completion([check], executor)['checks'][0]['status'] == 'not_executed']
    with c.context.lock:
        counts = dict(c.context.counts)
        remaining_tools = c.context.limits.tool_calls - counts['tool_calls']
        remaining_models = c.context.limits.model_calls - counts['model_calls']
    # Reserve the original finalization allowance and at least the three model
    # stages. This is admission only; retries still consume the normal tool cap.
    affordable = (len(new_reads) <= remaining_tools
                  and remaining_models >= max(3, c.context.limits.reserve_model_calls)
                  and (not new_reads or remaining_models > c.context.limits.reserve_model_calls)
                  and time.monotonic() - c.context.started
                      < c.context.limits.seconds - c.context.limits.reserve_seconds
                  and len(c.tasks) < 6)
    if available and not affordable:
        summary['status'] = 'budget_deferred'
        c.stop = 'BUDGET_EXCEEDED'
        c.event('diagnostic_pack_deferred', pack_id=pack.pack_id, code='BUDGET_EXCEEDED',
                planned_reads=len(new_reads), remaining_tools=remaining_tools)
    elif available:
        task = TaskRequest.model_validate_json(json.dumps({
            'role': 'investigation', 'goal': '执行服务登记的基础取证；查询结果不代表原因确认。',
            'checks': available, 'need_ids': [],
            'expected_value': '取得当前业务失败记录及对照，供诊断与复核判断。'}))
        objective = c.task_objective(task, False)
        objective['execution_source'] = POLICY
        result = c.collect_task(task, objective, executor, c.context, c.events, 0)
        c.record_task(task, objective, result)
        summary['tool_calls'] = c.context.counts['tool_calls'] - counts['tool_calls']
        summary['status'] = 'collected'
        if result['run_summary']['termination_reason'] == 'BUDGET_EXCEEDED':
            c.stop = 'BUDGET_EXCEEDED'
    else:
        summary['status'] = 'channels_unavailable'
        c.stop = 'NEEDS_INFORMATION'
    for entry, check, unavailable in metadata:
        if unavailable:
            state, error, evidence_ids = 'unavailable', unavailable, []
        else:
            row = completion([check], executor)['checks'][0]
            state, error, evidence_ids = row['status'], row['error'], row['evidence_ids']
        summary['checks'].append({'check_id': entry.check_id, **check, 'status': state,
                                  'error': error, 'evidence_ids': evidence_ids})
    refresh(c)
    c.event('diagnostic_pack_completed', pack_id=pack.pack_id, status=summary['status'],
            check_statuses=[r['status'] for r in summary['checks']],
            model_calls=0, tool_calls=summary['tool_calls'], goal_verified=False)


def refresh(c):
    """Derive current coverage from scoped executor reads, never model verdicts.

    A later complete covering read can discharge a previous failure. Narrower,
    filtered, differently sampled or unrelated reads cannot erase its gap.
    Recomputing this view never executes a read or grants a retry.
    """
    summary = c.diagnostic_pack_result
    if summary is None or summary['status'] in {'preflight', 'invalid_configuration', 'skipped_status_check'}:
        return summary
    pack, executor = c.diagnostic_pack, c.executors['investigation']
    rows, handoff, blocking_gaps = [], [], []
    for entry, previous in zip(pack.checks, summary['checks'], strict=True):
        check = {key: previous[key] for key in ('tool', 'args')}
        if previous['status'] == 'unavailable':
            row = {**check, 'status': 'unavailable', 'error': previous['error'],
                   'evidence_ids': [], 'query_complete': False}
        else:
            row = completion([check], executor)['checks'][0]
        # Empty event queries are complete within their scope. Empty metric
        # queries lack the registered samples; neither state proves health.
        blocking = row['status'] not in {'satisfied', 'empty'} or (
            row['status'] == 'empty' and row['tool'] == 'get_service_metrics')
        rows.append({**row, 'check_id': entry.check_id, 'blocking': blocking})
        if row['status'] != 'satisfied':
            labels = {'not_executed': '尚未执行', 'unavailable': '渠道未登记',
                      'failed': '查询失败', 'truncated': '样本截断', 'empty': '所选窗口内无匹配记录',
                      'missing_samples': '对照样本不足'}
            handoff.append(f'基础取证{entry.check_id}：{labels.get(row["status"], row["status"])}。'
                f'请{pack.handoff_role}{entry.human_check}；保持工单服务与时间范围，不将缺失或空结果当作健康。')
            if blocking:
                blocking_gaps.append(handoff[-1])
    blocking_ids = [r['check_id'] for r in rows if r['blocking']]
    coverage = 'checks_incomplete' if blocking_ids else 'checks_completed_empty' if handoff else 'checks_satisfied'
    status = summary['status']
    if status in {'collected', 'collected_with_gaps'}:
        status = 'collected_with_gaps' if handoff else 'collected'
    current = {'checks': rows, 'handoff': handoff, 'blocking_check_ids': blocking_ids, 'blocking_gaps': blocking_gaps,
               'coverage_status': coverage, 'status': status}
    if any(summary.get(key) != value for key, value in current.items()):
        old_handoff = set(summary['handoff'])
        c.extra_gaps[:] = [gap for gap in c.extra_gaps if gap not in old_handoff]
        c.extra_gaps.extend(handoff)
        summary.update(current)
        summary['revision'] += 1
        c.event('diagnostic_pack_state_updated', pack_id=pack.pack_id, revision=summary['revision'],
                check_statuses=[r['status'] for r in rows], blocking_check_ids=blocking_ids)
    return summary


def state_input(c):
    """A small mechanical view for planning and independent interpretation."""
    summary = refresh(c)
    if summary is None:
        return {}
    view = {key: deepcopy(summary.get(key)) for key in ('revision', 'handoff')}
    # Scoped args, evidence IDs and payloads already appear in task_results /
    # collection / evidence. Avoid repeating these catalogs in role inputs.
    view['checks'] = [{key: deepcopy(row.get(key)) for key in
                      ('check_id', 'tool', 'status', 'blocking')}
                     for row in summary['checks']]
    return {'diagnostic_pack_state': view,
        'diagnostic_pack_state_meaning': '查询覆盖不等于机制确认；完整空查询只说明该范围无匹配，不能证明健康。'}


def check_exit(c, stage):
    """Record and enforce exit conditions separately from evidence state.

    Incomplete collection still permits interpretation of existing observations;
    it cannot become a completed report because a model says it is supported.
    Never promotes a failed/partial workflow, authorizes reads or changes scope.
    """
    if c.diagnostic_pack is None:
        return None
    if stage not in {'diagnosis_entry', 'review_completion', 'finalization'}:
        raise ValueError('Unregistered diagnostic pack exit stage')
    summary = refresh(c)
    applicable = c.ticket.purpose == 'diagnosis'
    rows = summary['checks'] if summary else []
    blocking_ids = ([r['check_id'] for r in rows if r.get('blocking', True)]
                    if rows else [entry.check_id for entry in c.diagnostic_pack.checks]) if applicable else []
    configuration_valid = bool(summary and summary['status'] not in {'preflight', 'invalid_configuration'})
    observations = any(e.kind == 'observation' for e in c.evidence.values())
    conditions = {'configuration_valid': configuration_valid,
                  'baseline_collection_complete': not blocking_ids,
                  'observations_available': observations}
    can_enter = not applicable or (configuration_valid and observations)
    if stage == 'diagnosis_entry':
        allowed = can_enter
        decision = ('not_applicable' if not applicable else 'handoff_required' if not allowed else
                    'continue_with_limitations' if (blocking_ids or summary['handoff']) else 'ready_for_interpretation')
        if applicable and not allowed and not c.stop:
            c.stop = 'NEEDS_INFORMATION'
    else:
        conditions.update(draft_present=c.draft is not None, independent_review_present=c.review is not None,
                          diagnosis_sections_complete=bool(c.draft) and not c.diagnosis_completion_gaps(),
                          reviewed_gaps_clear=bool(c.draft and c.review) and not c.reviewed_gaps(),
                          workflow_allows_completion=c.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'})
        allowed = all(conditions.values()) if applicable else c.status() == 'completed'
        if applicable and not allowed and c.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'}:
            c.stop = 'DIAGNOSTIC_PACK_INCOMPLETE' if blocking_ids or not configuration_valid else 'REVIEW_INCOMPLETE'
            conditions['workflow_allows_completion'] = False
        decision = 'complete' if allowed else c.status()
    snapshot = {'stage': stage, 'applicable': applicable,
                'evidence_state_revision': summary.get('revision', 0) if summary else 0,
                'draft_revision': len(c.drafts), 'review_count': len(c.reviews),
                'conditions': conditions, 'blocking_check_ids': blocking_ids,
                'limitations': deepcopy(summary['handoff']) if summary else [],
                'can_enter_diagnosis': can_enter, 'can_mark_completed': allowed if stage != 'diagnosis_entry' else None,
                'allowed': allowed, 'decision': decision,
                'termination_reason': c.stop}
    if c.diagnostic_pack_exit is None:
        c.diagnostic_pack_exit = {'policy': EXIT_POLICY, 'latest': None, 'history': []}
    ledger = c.diagnostic_pack_exit
    if ledger['latest'] != snapshot:
        ledger['latest'] = deepcopy(snapshot)
        ledger['history'].append(deepcopy(snapshot))
        c.event('diagnostic_pack_exit_checked', **deepcopy(snapshot))
    return snapshot
