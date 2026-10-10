def render(result):
    if result.get("task_type") in {"incident_collaboration", "incident_single"}:
        return render_collaboration(result)
    output = result["output"]
    lines = ["# Investigation 取证报告", "", f"运行：{result['run_id']}；状态：{result['status']}",
             f"执行模式：{result.get('execution_mode', 'caller_supplied')}；数据源：{result.get('data_source', 'unknown')}",
             "", "当前仅为单角色取证；假设未经 Diagnosis/Reviewer 复核，故障未确认解决。", ""]
    if result.get("observation_coverage") == "limited_by_truncation":
        lines.extend(["观测不完整：工具结果发生截断，已保留的采样不能代表完整查询窗口。", ""])
    lines.extend(["工具执行记录（empty 只表示所选过滤条件未匹配，truncated 表示结果不完整）：", ""])
    for event in result["events"]:
        if event["type"] == "tool_result":
            profile = event.get("query_profile", {})
            lines.append(f"- {event['name']}: {event['status']}; filters={profile}; truncated={event['truncated']}"
                         + (f"; error={event['error']['code']}" if event.get("error") else ""))
    lines.append("")
    for finding in output["findings"]:
        lines.append(f"- 观测：{finding['statement']}")
        for ref in finding["refs"]:
            lines.append(f"  - {ref['evidence_id']} / {ref['field_path']} = {ref['value']} {ref['unit']} @ {ref['observed_at']} ({ref['data_version']})")
    lines.extend(["", "待验证假设："] + [f"- {h}" for h in output["tentative_hypotheses"]])
    lines.extend(["", "信息缺口："] + [f"- {gap}" for gap in output["missing_information"]])
    if output["escalation_team"]:
        lines.append(f"\n建议交接团队：{output['escalation_team']}")
    summary = result["run_summary"]
    lines.append(f"\n模型 {summary['model_calls']} 次；工具实际执行 {summary['tool_calls']} 次；终止原因 {summary['termination_reason']}。Token 状态：{summary['token_usage']['status']}；未估算费用。")
    return "\n".join(lines) + "\n"


def render_collaboration(result):
    output = result["output"]
    single = result.get("task_type") == "incident_single"
    status_check = result.get('purpose') == 'status_check'
    lines = ["# 服务状态核查报告" if status_check else "# 单 Agent 诊断报告" if single else "# 故障协作诊断报告", "", f"运行：{result['run_id']}；状态：{result['status']}；复核：{result['review_status']}",
        f"执行模式：{result.get('execution_mode', 'caller_supplied')}；数据源：{result['data_source']}",
        "本次核查只复核所列观测，整体健康尚未确认；工具只读，不执行修复。" if status_check
        else "当前结论未获人工确认，工单未解决；工具只读，操作建议不会自动执行。", ""]
    if status_check:
        lines.extend(['核查范围与限制：', *['- ' + note for note in (result.get('status_check_coverage') or {}).get('limitations', [])], ''])
    readiness = result.get('diagnosis_readiness')
    if readiness:
        labels = {'reviewed': '报告已复核', 'reviewed_with_limitations': '报告已复核，保留所列限制',
                  'incomplete': '诊断仍有关键缺口'}
        lines.extend([labels[readiness['report_status']] + '；' + readiness['meaning'], ''])
    if result.get("execution_mode") == "scripted_control_only":
        lines.extend(["这是脚本控制演示，复核与调度也是测试替身，不代表真实模型诊断质量。", ""])
    lines.append("观测（无独立 Reviewer，语义待核查）：" if single else "已复核观测：")
    for finding in output["findings"]:
        lines.append(f"- {finding['statement']}")
        for ref in finding["refs"]:
            lines.append(f"  - {ref['evidence_id']} / {ref['field_path']} = {ref['value']} {ref['unit']} @ {ref['observed_at']}")
    if result.get('unreviewed_observations'):
        lines.extend(['', '已保留的来源观测（引用通过程序校验，尚未获独立语义复核）：'])
        for finding in result['unreviewed_observations']:
            lines.append('- ' + finding['statement'])
            for ref in finding['refs']:
                lines.append(f"  - {ref['evidence_id']} / {ref['field_path']} = {ref['value']} {ref['unit']} @ {ref['observed_at']}")
    if not status_check:
        lines.extend(["", "原因假设（supported 只表示当前证据支持）："])
        if not output['hypotheses']:
            lines.append('- 尚未形成原因假设；已有观测不足以作为完整诊断结论。')
    for h in output["hypotheses"]:
        levels = {'mechanism': '故障机制', 'trigger': '触发原因', 'alternative': '替代解释', 'unspecified': '未分层（兼容旧输出）'}
        lines.append(f"- {h['hypothesis_id']} [{h['status']}] [{levels[h.get('level', 'unspecified')]}] {h['cause']}")
        lines.append(f"  - 支持：{[r['evidence_id'] for r in h['support_refs']]}；反证：{[r['evidence_id'] for r in h['counter_refs']]}")
        if h.get('evidence_explanation'):
            lines.append('  - 证据与因果边界：' + h['evidence_explanation'])
        lines.extend('  - 待验证：' + item for item in h.get('pending_checks', []))
    if not status_check:
        lines.extend(["", "处理建议："])
        if not output['recommended_actions']:
            lines.append('- 尚无经过复核的处理建议。')
    for action in output["recommended_actions"]:
        kinds = {'read_only_check': '只读核查', 'manual_change': '有条件的人工变更',
                 'registered_repair': '登记修复候选', 'unspecified': '未分类（兼容旧输出）'}
        lines.append(f"- [{kinds[action.get('kind', 'unspecified')]}] {action['action']}；条件：{action['condition']}；验证：{action['expected_result']}；风险：{action['risk']}；需批准：{action['requires_approval']}")
        lines.extend('  - 提议只读检查：' + check['tool'] for check in action.get('checks', []))
    if result.get('pending_actions'):
        lines.extend(['', '待确认建议（未通过复核，不能据此审批或执行修复）：'])
        for action in result['pending_actions']:
            lines.append(f"- {action['target_id']} [{action['kind']}] {action['action']}；条件：{action['condition']}；风险：{action['risk']}；验证：{action['expected_result']}")
            lines.append('  - 复核：' + action['review_reason'])
    lines.extend(["", "协作过程："])
    for task in result["task_results"]:
        lines.append(f"- {task['role']}: {task['goal']} → {task['status']}（模型 {task['model_calls']} / 工具 {task['tool_calls']}）")
        coverage = task.get('completion')
        if coverage:
            label = {'checks_satisfied': '声明的检查已取得样本，目标含义仍需复核',
                     'checks_completed_empty': '声明的查询已完成，部分结果为空，目标含义仍需复核',
                     'checks_incomplete': '声明的检查尚未满足',
                     'not_specified': '未声明具体检查，程序未核验目标完成'}[coverage['status']]
            lines.append(f"  执行循环：{task['execution_status']}；任务检查：{label}。")
            lines.extend('  查询限制：' + note for note in coverage.get('limitations', []))
    for item in result["negotiation"]:
        if item["type"] in {"challenge", "request_evidence", "revise"}:
            lines.append(f"- {item['type']} {item['hypothesis_id']}: {item['reason']} {item['requested_check']}")
    if result.get('evidence_needs'):
        labels = {'pending': '待核实', 'supported': '在所列边界内获得支持',
                  'unavailable': '渠道不可用', 'deferred': '暂缓', 'not_required': '说明后无需继续检查'}
        lines.extend(['', '共享证据需求（含义由模型评估，程序校验结构与来源）：'])
        for need in result['evidence_needs']:
            lines.append(f"- {need['need_id']} [{labels[need['status']]}] {need['question']}；{need['reason']}")
            if need['evidence_ids']:
                lines.append(f"  - 来源：{need['evidence_ids']}")
            if need.get('blocking') is False:
                lines.append('  - 作为非阻塞限制保留，含义由模型评估。')
    lines.extend(["", "信息缺口：", *[f"- {gap}" for gap in output["missing_information"]]])
    if output["escalation_team"]:
        lines.append(f"建议升级团队：{output['escalation_team']}")
    summary = result["run_summary"]
    lines.append(f"\n模型 {summary['model_calls']} / 工具 {summary['tool_calls']}；返工 {result['rework_rounds']} 轮；结构修复 {result['repairs']} 次；{summary['termination_reason']}。未估算费用。")
    return "\n".join(lines) + "\n"
