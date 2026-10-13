from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RepairIntent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['restart_service', 'start_service', 'resume_service', 'reload_config', 'resume_consumer', 'refresh_cache', 'scale_service', 'rollback_release']
    target: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    parameters: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def action_parameters(self):
        if self.action in {'restart_service', 'start_service', 'resume_service', 'reload_config', 'resume_consumer', 'refresh_cache'} and self.parameters:
            raise ValueError('registered maintenance actions accept no caller-controlled commands')
        if self.action == 'scale_service' and (set(self.parameters) != {'replicas'}
                or type(self.parameters['replicas']) is not int or not 1 <= self.parameters['replicas'] <= 10):
            raise ValueError('scale requires replicas 1..10')
        if self.action == 'rollback_release' and (set(self.parameters) != {'version'}
                or not isinstance(self.parameters['version'], str) or not 1 <= len(self.parameters['version']) <= 40):
            raise ValueError('rollback requires a registered version')
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError('duplicate evidence')
        return self


class RepairApprovalDetails(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    condition: str = Field(min_length=1, max_length=1000)
    risk: str = Field(min_length=1, max_length=1000)
    expected_result: str = Field(min_length=1, max_length=1000)

    @field_validator('condition', 'risk', 'expected_result')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('approval details must not be blank')
        return value


class RepairProposal(RepairIntent):
    request_key: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    revision: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=500)
    # Optional for existing API clients; new UI proposals collect all three fields.
    approval_details: RepairApprovalDetails | None = None


class RepairFromRecommendation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_key: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    revision: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=40)
    action_index: int = Field(ge=0, le=3)


class RepairApproval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    decision: Literal['approve', 'reject']


class RepairExecution(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_key: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')


ACTIONS = {
    'start_service': {'risk_level':'medium', 'impact':'启动已停止的登记容器，会恢复业务处理并占用资源；须确认并非计划停用，避免重复任务或写入。',
                      'rollback':'不自动停止容器；验证失败转人工处理。'},
    'resume_service': {'risk_level':'low', 'impact':'恢复已暂停容器中的进程；可能继续处理积压任务或写入，须确认暂停原因已经解除。',
                       'rollback':'不自动再次暂停；验证失败转人工处理。'},
    'reload_config': {'risk_level':'medium', 'impact':'加载已登记且已校验的配置版本；错误配置可能改变流量和业务行为，须人工核实版本。',
                      'rollback':'不自动回滚配置；恢复旧版本须更新登记并创建新审批方案。'},
    'resume_consumer': {'risk_level':'medium', 'impact':'恢复指定消费者，会处理积压任务并可能恢复写入；须确认暂停原因和任务幂等性。',
                        'rollback':'不自动重新暂停；异常时交由人工处理。'},
    'refresh_cache': {'risk_level':'medium', 'impact':'刷新指定缓存组，可能增加后端负载；范围固定，禁止全库清理。',
                      'rollback':'旧缓存内容不由本系统保存；刷新失败转人工处理。'},
    'restart_service': {'impact': '目标服务短暂中断；进程内状态可能丢失。',
                        'rollback': '重启不可撤销；验证失败后转人工处理，不重复重启。'},
    'scale_service': {'impact': '改变副本数量和资源消耗。',
                      'rollback': '记录原副本数，由新审批动作恢复；禁止无限扩容。'},
    'rollback_release': {'impact': '切换服务版本；必须确认数据和配置兼容。',
                         'rollback': '记录原版本，由新审批动作恢复；数据库迁移不自动回退。'},
}
