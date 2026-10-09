# Harness 约束证据

本轮复用现有测试，运行七项，覆盖五类边界；2026-10-10 全部通过。输入是合成观测和脚本模型，证明程序约束，不证明模型在所有场景都能正确判断。

| 类别 | 程序行为 | 现有测试定位 |
| --- | --- | --- |
| 权限与范围 | 未开放工具、额外身份参数、扩张时间窗口或服务范围被拒绝 | `tests/tools/test_incident_tools.py`：`test_role_unknown_tools_and_extra_fields`、`test_window_timezone_service_and_scope_expansion` |
| 证据引用 | 假引用最多纠正一次，仍错误则 partial，不进入可信结论 | `tests/workflow/test_diagnosis_references.py`：`test_persistent_invalid_reference_stops_after_one_repair_with_public_error_location` |
| 结构输出 | 叶角色与 Reviewer 共用结构修复额度，不能逐角色无限修复 | `tests/workflow/test_incident_collaboration.py`：`test_schema_repair_allowance_is_shared_across_leaf_and_reviewer` |
| 重试与去重 | 工具超时最多两次尝试，每次计费预算；相同查询拒绝重复，快照不受外部修改 | `tests/tools/test_incident_tools.py`：`test_retry_count_error_sanitization_and_limit`、`test_duplicate_no_double_count_and_snapshot_immutability` |
| 调用预算 | 保留终结额度完成复核，补查预算不足即停止，不增加上限 | `tests/workflow/test_incident_collaboration.py`：`test_small_budget_keeps_review_and_refuses_unaffordable_rework` |

完整测试路径和逐项本轮结果由 `scripts/showcase_incident.py` 写入本地 `summary.json` 的 `harness_controls`。脚本测试失败会返回非零状态。

三条演示还校验事件序列、调用计数、证据快照、引用、诊断契约与预算，并验证补查实际执行。模拟修复复用 `scripts/demo_remediation.py`：审批和执行分开，同一请求只执行一次，真实写操作为零。

这类证据适合回答“模型胡乱调用怎么办”“如何避免无穷重试”“为什么格式通过仍可能错误”。不要将七项测试称为完整生产安全审计，或把模型拒绝事件与 Provider 尝试混为一谈。

本轮不新增真实数据库、Docker 或付费模型验收；它们已有各自历史检查，不能自动计入当前全量结果。没有新增相关生产功能，不重复外部操作来证明材料整理。
