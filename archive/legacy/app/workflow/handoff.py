"""Multi-role, evidence-bound closure helpers; no model calls or direct queries."""
from dataclasses import dataclass
import time

from evidence.ownership import observed_owner_team
from providers.fixtures import ProviderError

FATAL_CODES = frozenset({'AUTH_ERROR', 'QUOTA_EXCEEDED', 'SCOPE_DENIED', 'TOOL_DENIED', 'WINDOW_DENIED'})


@dataclass(frozen=True)
class OwnerChoice:
    status: str
    team: str | None = None


def owner_choice(evidence, alias, queries=()):
    if any(row['tool'] == 'get_service_owner' and row['args'].get('alias') == alias
           and row.get('truncated') for row in queries):
        return OwnerChoice('incomplete')
    matches = [item for item in evidence.values() if observed_owner_team(item)
               and (item.payload.get('service') == alias or alias in item.payload.get('aliases', []))]
    teams = {observed_owner_team(item) for item in matches}
    if len(teams) > 1:
        return OwnerChoice('ambiguous')
    return OwnerChoice('observed', next(iter(teams))) if teams else OwnerChoice('missing')


def has_business_observation(evidence):
    return any(item.kind == 'observation' and not item.locator.startswith('get_service_owner/')
               for item in evidence.values())


def fatal_collection_code(collection):
    return next((item['error']['code'] for item in collection
                 if item.get('error') and item['error']['code'] in FATAL_CODES), None)


def can_finalize(context):
    with context.lock:
        return (context.limits.model_calls - context.counts['model_calls'] >= 2
                and time.monotonic() - context.started < context.limits.seconds - context.limits.reserve_seconds)


def handoff_notes(ticket, choice):
    recipient = choice.team or f'工单提交人及{ticket.scope.service}服务负责人（登记团队尚未确认）'
    return [
        f'人工只读核实：请{recipient}补充受影响功能、实际请求结果或错误消息、复现步骤及发生时间；'
        f'核对其是否落在{ticket.scope.start.isoformat()}至{ticket.scope.end.isoformat()}的授权窗口内。',
        '后续验证：观测渠道可用后核对同一窗口的业务指标和日志；未取得当前业务观测，不能确认故障机制、服务正常或已经恢复。',
        ('负责人查询结果截断，不能确认唯一接手团队，需人工确认。' if choice.status == 'incomplete' else
         '负责人信息存在冲突，未选择任何登记团队，需人工确认接手人。' if choice.status == 'ambiguous' else
         f'交接对象来自本次登记观测：{choice.team}；只建议接手，未实际联系。' if choice.team else
         '未取得可用登记负责人；请工单提交人确认接手角色，不编造团队。'),
    ]


class SingleAttemptOwnerProvider:
    """Disable the shared executor's transient retry only for this closure read."""
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def query(self, name, *args, **kwargs):
        try:
            return self.provider.query(name, *args, **kwargs)
        except ProviderError as exc:
            if name == 'get_service_owner':
                raise ProviderError(exc.code, retryable=False) from None
            raise
