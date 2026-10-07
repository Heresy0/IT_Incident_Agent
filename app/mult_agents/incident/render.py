def render(result):
    if result.get("task_type") == "incident_collaboration":
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
    lines = ["# 故障协作诊断报告", "", f"运行：{result['run_id']}；状态：{result['status']}；复核：{result['review_status']}",
        f"执行模式：{result.get('execution_mode', 'caller_supplied')}；数据源：{result['data_source']}",
        "当前结论未获人工确认，工单未解决；工具只读，操作建议不会自动执行。", ""]
    if result.get("execution_mode") == "scripted_control_only":
        lines.extend(["这是脚本控制演示，复核与调度也是测试替身，不代表真实模型诊断质量。", ""])
    lines.append("已复核观测：")
    for finding in output["findings"]:
        lines.append(f"- {finding['statement']}")
        for ref in finding["refs"]:
            lines.append(f"  - {ref['evidence_id']} / {ref['field_path']} = {ref['value']} {ref['unit']} @ {ref['observed_at']}")
    lines.extend(["", "原因假设（supported 只表示当前证据支持）："])
    for h in output["hypotheses"]:
        lines.append(f"- {h['hypothesis_id']} [{h['status']}] {h['cause']}")
        lines.append(f"  - 支持：{[r['evidence_id'] for r in h['support_refs']]}；反证：{[r['evidence_id'] for r in h['counter_refs']]}")
    lines.extend(["", "处理建议："])
    for action in output["recommended_actions"]:
        lines.append(f"- {action['action']}；条件：{action['condition']}；验证：{action['expected_result']}；风险：{action['risk']}；需批准：{action['requires_approval']}")
    lines.extend(["", "协作过程："])
    for task in result["task_results"]:
        lines.append(f"- {task['role']}: {task['goal']} → {task['status']}（模型 {task['model_calls']} / 工具 {task['tool_calls']}）")
    for item in result["negotiation"]:
        if item["type"] in {"challenge", "request_evidence", "revise"}:
            lines.append(f"- {item['type']} {item['hypothesis_id']}: {item['reason']} {item['requested_check']}")
    lines.extend(["", "信息缺口：", *[f"- {gap}" for gap in output["missing_information"]]])
    if output["escalation_team"]:
        lines.append(f"建议升级团队：{output['escalation_team']}")
    summary = result["run_summary"]
    lines.append(f"\n模型 {summary['model_calls']} / 工具 {summary['tool_calls']}；返工 {result['rework_rounds']} 轮；结构修复 {result['repairs']} 次；{summary['termination_reason']}。未估算费用。")
    return "\n".join(lines) + "\n"
