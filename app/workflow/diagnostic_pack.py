"""Opt-in, service-scoped baseline reads. Never selects or confirms a cause."""
import hashlib
import json
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator
from agents.contracts import TaskRequest
from runtime.context import ExecutionError
from workflow.task_checks import completion

POLICY = 'service_baseline_reads_experiment_v1'
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
        if state != 'satisfied':
            labels = {'not_executed': '未执行（预算或限次不足）', 'unavailable': '渠道未登记',
                      'failed': '查询失败', 'truncated': '样本截断', 'empty': '所选窗口内无匹配记录',
                      'missing_samples': '对照样本不足'}
            gap = f'基础取证{entry.check_id}：{labels.get(state, state)}。请{pack.handoff_role}{entry.human_check}；保持工单服务与时间范围，不将缺失或空结果当作健康。'
            summary['handoff'].append(gap)
            c.extra_gaps.append(gap)
    if summary['status'] == 'collected' and summary['handoff']:
        summary['status'] = 'collected_with_gaps'
    c.event('diagnostic_pack_completed', pack_id=pack.pack_id, status=summary['status'],
            check_statuses=[r['status'] for r in summary['checks']],
            model_calls=0, tool_calls=summary['tool_calls'], goal_verified=False)
