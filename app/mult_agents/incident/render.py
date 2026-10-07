def render(result):
    output = result["output"]
    lines = ["# Investigation 取证报告", "", f"运行：{result['run_id']}；状态：{result['status']}",
             f"执行模式：{result.get('execution_mode', 'caller_supplied')}；数据源：{result.get('data_source', 'unknown')}",
             "", "当前仅为单角色取证；假设未经 Diagnosis/Reviewer 复核，故障未确认解决。", ""]
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
