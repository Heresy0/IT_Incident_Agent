"""Simplified Chinese display labels; JSON keeps stable machine-readable codes."""

LABELS = {
    'connection_wait': '连接等待', 'authentication': '认证异常', 'source_unavailable': '取证数据源不可用',
    'different_mechanism': '故障机制变化', 'renamed_registration': '服务及指标改名',
    'status_only': '仅检查状态', 'small_budget': '小预算', 'empty_business_logs': '业务日志无匹配记录',
    'truncated_business_logs': '业务日志长记录截断', 'dense_business_logs': '密集业务日志',
    'unregistered_service': '服务未登记取证包',
    'satisfied': '已取得所需样本', 'empty': '查询完整但无匹配记录', 'failed': '失败',
    'truncated': '样本截断', 'missing_samples': '缺少所需样本', 'unavailable': '渠道未登记',
    'not_executed': '尚未执行', 'collected': '基础取证已采集', 'collected_with_gaps': '已采集但保留缺口',
    'budget_deferred': '预算不足，暂缓取证', 'channels_unavailable': '全部取证渠道不可用',
    'skipped_status_check': '状态核查，跳过机制取证', 'invalid_configuration': '配置错误',
    'disabled_or_unregistered': '未启用或未登记', 'preflight': '执行前校验',
    'partial': '部分完成', 'complete': '允许完成', 'completed': '已完成',
    'passed': '通过', 'needs_information': '需要补充信息', 'not_performed': '未执行',
    'business-failure': '业务失败记录', 'business-log': '业务日志',
    'pressure-controls': '压力与负载对照', 'controls': '对照指标',
    'recent-changes': '近期变更', 'changes': '变更记录',
    'change-timeline': '故障前后变更时间线', 'symptom-controls': '业务错误率与延迟对照',
    'FINISHED': '流程完成', 'NEEDS_INFORMATION': '需要补充信息',
    'ESCALATED': '已转人工处理', 'BUDGET_EXCEEDED': '达到预算限制',
    'DIAGNOSTIC_PACK_INCOMPLETE': '基础取证覆盖不足', 'REVIEW_INCOMPLETE': '复核条件未满足',
    'DIAGNOSIS_INCOMPLETE': '诊断内容不完整', 'MODEL_OUTPUT_INVALID': '模型输出不符合协议',
    'MODEL_ERROR': '模型服务错误', 'AUTH_ERROR': '模型认证失败', 'NETWORK_ERROR': '网络连接失败',
    'TIMEOUT': '请求超时', 'RATE_LIMIT': '服务限流', 'QUOTA_EXCEEDED': '账户额度不足',
    'SUPERVISOR_LIMIT': '达到调度次数限制', 'NO_TOOL_PROGRESS': '取证未产生进展',
    'STATUS_CHECK_COMPLETED': '状态核查完成', 'INTERNAL_ERROR': '内部执行错误',
    'REWORK_NOT_EXECUTED': '复核补查未执行', 'REWORK_LIMIT': '达到补查轮次限制',
}


def label(value):
    if value is None:
        return '不适用'
    return LABELS.get(value, '未分类状态')


def labels(values):
    return '；'.join(label(value) for value in values) if values else '无'


def control_markdown(result):
    lines = ['# 诊断包受控取证实验', '',
             f"源码：{result['source_version']}；{result['cases']} 个控制场景、{result['runs']} 次图运行；付费模型调用为零。", '',
             '| 场景 | 原流程检查状态 | 诊断包检查状态 | 增加的工具调用次数 | 诊断包采集结果 | 退出判断及阻塞项 | 输入超限次数（原流程／诊断包） |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for pair in result['pairs']:
        baseline, pack = pair['baseline'], pair['pack']
        lines.append(f"| {label(pair['case_id'])} | {labels(baseline['query_statuses'])} | {labels(pack['query_statuses'])} | "
                     f"{pair['extra_tool_calls']} | {label(pack['pack_status'])} | {label(pack.get('pack_exit_decision'))}；"
                     f"阻塞项：{labels(pack.get('pack_blocking_check_ids', []))} | "
                     f"{baseline['input_budget_rejections']}／{pack['input_budget_rejections']} |")
    lines += ['', '## 解释限制', ''] + ['- ' + text for text in result['limitations']]
    return '\n'.join(lines) + '\n'
