# 当前 v2 导览

状态日期：2026-10-18。本页说明当前分支的代码入口、已验收演示和评估材料；统一入口为[根 README](../README.md)。

## 运行入口与默认值

| 入口 | 实际含义 |
| --- | --- |
| `scripts/demo_frontend_live_acceptance.py --serve` | 隔离的 8176 演示：真实 `qwen3.7-flash`，`autonomous_v2 / closed_loop`，启用 challenge 和最多一次语义返工 |
| `WorkflowService`、普通后端与 `main.py` | 默认 `autonomous_v2 / closed_loop`；显式旧引擎配置报错，准入失败拒绝启动 |
| `scripts/demo_frontend_acceptance.py` | 保存报告的回放与脚本控制验收；与真实演示共用 frontend_demo_support 的身份和环境校验 |
| 归档中的 `scripts/demo_standard_flow.py` | 恢复完整旧版后免费排练；不计自主模型质量 |

本机 8176 已在运行，前端是 `front/agent_front/dist/` 中的 Vue 构建产物，使用隔离 PostgreSQL。真实模型配合合成 fixture 观测，未连接生产故障或真实修复执行器。克隆代码不会自动恢复这些本地资源。

演示配置由 [live 辅助脚本](../scripts/demo_frontend_live_acceptance.py)显式建立，不修改日常配置：每次运行固定最多 16 次模型、8 次工具、180 秒；全局付费请求上限 32 次，记账文件为 `output/frontend-live-acceptance-20261018/server/model-ledger.json`。整理前已用 23 次。这些是本次演示的预算，不是建议修改所有入口的默认预算。

## v2 代码怎样工作

| 环节 | 职责与源码 |
| --- | --- |
| 自主调查 | [engine.py](../app/workflow/v2/engine.py) 中的单步循环选择查询或结束；程序验证权限、参数、去重和剩余预算 |
| 证据与诊断 | [v2 模块](../app/workflow/v2/)生成可追溯的源事实和有限命题；局部契约失败只纠正失败条目 |
| 逐项审阅 | [review.py](../app/workflow/v2/review.py)分别审阅事实性命题与人工核查建议，绑定条目及证据版本 |
| 质疑与有界返工 | [engine.py](../app/workflow/v2/engine.py)按条件质疑已支持结论，必要时调查或修订后重新审阅；共用总预算，语义返工最多一次 |
| 保存与交付 | [WorkflowService](../app/workflow/service.py)、[API](../app/api/)和[前端](../front/agent_front/)保存运行，通过 HTTP／SSE 交付报告 |

模型承担调查、诊断、审阅和条件触发的质疑职责；取证执行、查询台账、权限、预算、发布与终止条件由程序负责。服务登记提供可查询能力和数据语义，具体取证路径由模型选择，不为每个服务预写故障决策树。

旧版 [Coordinator](../archive/legacy/app/workflow/coordinator.py)、单 Agent 基线和关联测试已归档，恢复完整旧版见[回滚说明](../archive/legacy/README.md)。v2 使用独立的 [schema 工具](../app/runtime/schema.py)和[证据引用工具](../app/evidence/references.py)，不再依赖旧版 Agent 或编排器。

## 只读核验现有演示

在 8176 已启动且本地验收记录仍存在时执行：

```powershell
.\.venv\Scripts\python.exe scripts/check_frontend_live_acceptance.py `
  --run-id c6b716dc-58a2-480e-a31c-a2d8e08b1bd3 `
  --run-id 83e2695d-0c63-41d2-88ab-db1b1c08b34b `
  --baseline output/frontend-live-acceptance-20261018/legacy-retirement-before/receipt.json `
  --output output/frontend-live-acceptance-20261018/manual-readonly-check
```

该脚本只 GET 现有健康状态、报告和 SSE，并读取账本；不新建工单或发起模型调用。服务端同时检查现有工单的当前准入配置，不创建运行。`--baseline` 绑定历史报告的原始源码与内容；新运行准入则必须匹配当前源码。输出目录应直接位于 `output/frontend-live-acceptance-20261018/` 下，以便找到相邻的 `server/` 与 `before/` 记录。不要删除账本来重置调用额度，也不要删除正在被服务使用的前端构建目录。

已有两次从前端创建并启动的真实 Flash 运行：一条取证后完成审阅，另一条经历质疑、一次修订及最终审阅；后者返工前已用完工具预算，返工期间没有取得新证据。因此只能据此证明闭环执行与交付，不能宣称该次返工改善了补证或证明根因正确。详见[原始验收摘要](../evals/frontend-live-validation-20261018.json)。

## 评估分开阅读

下列质量数字属于各自冻结时点的源码。本轮旧版退役未新增模型调用或重新评分，只验证共享能力、运行入口、控制流程和历史报告交付。

| 要回答的问题 | 对应材料 | 解释边界 |
| --- | --- | --- |
| 重构是否减少协议中断和延迟 | [旧版／v2 冻结配对](../evals/frozen-version-validation-20261018.json) | 相同 Flash、案例和 12／8／180 预算；包含相关变体和重复，不是独立生产事故 |
| 是否能取得关键证据并定位根因 | [8 类机制跨案例评分](../evals/rca-cross-case-scorecard-20261018.json)、[设计与复现](../evals/rca-cross-case-validation-20261018.json) | 16／8／180 预算；端到端与预置证据分开统计；非盲 AI 裁决，不是人工金标 |
| 调查模型还是审阅模型造成差异 | [角色模型对照](../evals/autonomous-role-validation-20261017.json) | 按角色替换 Flash／Plus；小样本、有特定源码和预算 |
| 调高预算是否有收益 | [四组预算对照](../evals/budget-matrix-validation-20261018.json) | 各组单列，不回写默认预算 |
| 前端是否能交付报告和展示返工 | [真实前端闭环验收](../evals/frontend-live-validation-20261018.json) | 证明执行、存储与展示；不评分根因准确率 |

跨案例端到端严格根因内容正确为 21/24，关键证据取得为 24/24；后者只表示证据进入存储。仍有证据整合与语义审阅问题。两项比例来自开发案例和 AI 裁决，不能外推为生产准确率，也不能与不同预算的旧版实验相减。

原报告、消息、事件、查询结果、运行账本和冻结源码保持原路径。`evals/` 中的已提交摘要可用于定位本机 `output/` 原档；缺少原档时不能声称已重新核验哈希。历史实验见[归档索引](../archive/experiments/README.md)。
