"""Mechanical channel coverage and recovery; no scenario answers or verdicts.

Local fallback is offered only for a material reviewed gap, bounded to two
registered log boundaries. No remote state is inferred from local observations.
"""
from workflow.task_checks import completion
from runtime.context import ExecutionError
from agents.contracts import TaskRequest
from typing import ClassVar

POLICY = 'local_channel_recovery_v1'
# Channel-wide registration/availability failures, not empty queries, timeouts,
# result truncation, category-specific failures or model errors.
CHANNEL_FAILURES = frozenset({'SOURCE_UNAVAILABLE', 'SOURCE_NOT_CONFIGURED', 'AUTH_ERROR', 'QUOTA_EXCEEDED'})
LOCAL_CATEGORIES = ('resource', 'configuration')


class LocalBoundaryTask(TaskRequest):
    """Internal provenance, deliberately absent from the model contract."""
    planning_source: ClassVar[str] = 'program_registered_boundaries'


def boundary_task(checks, needs):
    return LocalBoundaryTask(role='investigation',
        goal='核查工单窗口的本地资源与配置日志边界，供后续模型独立判断。',
        need_ids=[n.need_id for n in needs.values() if n.purpose == 'mechanism'], checks=checks,
        expected_value='取得本地边界实际记录，与依赖交互观测对照；空结果或失败保留限制，不推定健康。')


def channel_failure(executor, tool):
    latest = next((row for row in reversed(list(executor.check_results.values())) if row['tool'] == tool), None)
    return latest if latest and latest['status'] == 'error' and latest.get('error') in CHANNEL_FAILURES else None


def local_checks(executor, scope):
    """Validated unfiltered local boundaries not already attempted/covered."""
    if channel_failure(executor, 'get_service_logs'):
        return []
    checks = []
    for category in LOCAL_CATEGORIES:
        try:
            args = executor.validate_args('get_service_logs',
                {'start': scope.start.isoformat(), 'end': scope.end.isoformat(),
                 'category': category}).model_dump(mode='json')
        except ExecutionError:
            continue
        check = {'tool': 'get_service_logs', 'args': args}
        if completion([check], executor)['checks'][0]['status'] == 'not_executed':
            checks.append(check)
    return checks


def channel_inventory(executors, scope):
    channels = []
    for role, executor in executors.items():
        for schema in executor.schemas():
            name = schema['function']['name']
            rows = [row for row in executor.check_results.values() if row['tool'] == name]
            failure = channel_failure(executor, name)
            channels.append({'role': role, 'tool': name,
                'availability': 'failed_channel' if failure else 'attempted' if rows else 'not_queried',
                'failure_code': failure['error'] if failure else None})
    local = []
    executor = executors['investigation']
    for category in LOCAL_CATEGORIES:
        try:
            args = executor.validate_args('get_service_logs', {'start': scope.start.isoformat(),
                'end': scope.end.isoformat(), 'category': category}).model_dump(mode='json')
        except ExecutionError as exc:
            local.append({'category': category, 'status': 'not_registered_or_denied', 'code': exc.code})
            continue
        state = completion([{'tool': 'get_service_logs', 'args': args}], executor)['checks'][0]
        local.append({'category': category, 'status': state['status'], 'error': state['error']})
    return {'policy': POLICY, 'channels': channels, 'local_boundaries': local,
        'meaning': 'not_queried不是失败；空结果仅覆盖查询过滤；failed_channel不能认定健康。范围外信息仍需人工取得。'}


def failed_request_channel(review, executors):
    request = review.request_evidence
    source = getattr(review, 'request_source', None)
    if not request:
        return None
    executor = executors[request.target_role]
    # A fresh explicit hint prevents a broad failed-channel shortcut from
    # concealing an alternative path offered by the valid review.
    if any(not channel_failure(executor, check.tool) and
           completion([check.model_dump(mode='json')], executor)['checks'][0]['status'] == 'not_executed'
           for check in request.checks):
        return None
    if source:
        return channel_failure(executor, source.tool)
    # Compatibility reviews may give precise checks without a channel hint.
    if request.checks and all(channel_failure(executor, c.tool) for c in request.checks):
        return channel_failure(executor, request.checks[0].tool)
    return None


RECOVERY_GUIDANCE = """
channel_inventory为本轮程序台账，不是健康结论。not_queried不能写成查询失败，
failed_channel不能写成空结果或健康。不要将指标证据ID当作日志失败记录。
依赖探测不覆盖本服务resource/configuration边界；关键机制仍缺观测时，先考虑未查的登记本地边界，
不要仅因远端内部状态不可读就提前放弃本地取证。已有近端机制支持时不强制遍历其它渠道。
非重试来源失败不能靠改过滤或相同读取恢复。下一步可为manual_check：人工核查观测渠道状态、
获取授权范围内的替代观测或确认负责人；说明所需记录与核实条件，不预设原因、恢复或查询成功。
Reviewer在request_evidence寻求本服务渠道时给request_source.tool及日志category（若明确）；
范围外远端内部信息用null。这只是渠道标识，不规划参数、不授权执行。
"""
