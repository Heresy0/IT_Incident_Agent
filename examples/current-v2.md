# 当前 v2 导览

状态日期：2026-10-19。本页说明当前分支的代码入口、已验收演示和评估材料；统一入口为[根 README](../README.md)。

首次阅读可先看[项目阅读导航](README.md)、[架构导览](architecture.md)和[评估口径](evaluation-guide.md)；按用途找命令见[脚本导航](../scripts/README.md)。本页保留本机环境和历史验收的具体状态。

## 运行入口与默认值

| 入口 | 实际含义 |
| --- | --- |
| `scripts/demo_frontend_live_acceptance.py --serve --maximum-model-calls 55` | 本机现有账本的 8176 演示：真实 `qwen3.7-flash`，`autonomous_v2 / closed_loop`，启用 challenge 和最多一次语义返工 |
| `WorkflowService`、普通后端与 `main.py` | 默认 `autonomous_v2 / closed_loop`；显式旧引擎配置报错，准入失败拒绝启动 |
| `scripts/demo_frontend_acceptance.py` | 保存报告的回放与脚本控制验收；与真实演示共用 frontend_demo_support 的身份和环境校验 |
| 归档中的 `scripts/demo_standard_flow.py` | 恢复完整旧版后免费排练；不计自主模型质量 |

本机 8176 已在运行，前端是 `front/agent_front/dist/` 中的 Vue 构建产物，使用隔离 PostgreSQL。真实模型配合合成 fixture 观测，未连接生产故障或真实修复执行器。克隆代码不会自动恢复这些本地资源。

演示配置由 [live 辅助脚本](../scripts/demo_frontend_live_acceptance.py)显式建立，不修改日常配置：每次运行固定最多 16 次模型、8 次工具、180 秒。记账文件为 `output/frontend-live-acceptance-20261018/server/model-ledger.json`，本次获用户授权后将总上限从 32 增至 55，保留原有 23 次调用并追加扩额记录；补做两次诊断后累计 47 次、剩余 8 次。这些是本次演示的预算，不是所有入口的默认预算。

重启本机现有演示需沿用原数据库、原 output 目录及 `--maximum-model-calls 55`。首次扩额时显式使用 `--extend-ledger`，先停止旧服务；账本会记录旧上限、新上限、既有调用数和既有调用内容摘要。后续相同上限的重启不再次扩额，不允许静默重置或降低上限。默认新账本仍为 32 次；不要删除账本来重置额度。

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

2026-10-19 已继续清理无调用者的任务分支、证据适配、日志补查和历史评估辅助；原文件快照见[剩余旧版归档](../archive/legacy/residual-20261019/README.md)。当前应用指纹为 `0d58da1f8b5285f4`，8176 已按新指纹重新绑定原闭环配置。655 项免费测试通过（含 PostgreSQL / pgvector）；9 个既有工单、报告、SSE 与首页内容哈希保持一致，原调用账本仍为 47/55。提示词、v2 引擎与审阅代码保持原字节；本轮未新增真实模型运行，历史报告继续绑定其原源码。详见[清理验收](../evals/residual-cleanup-validation-20261019.json)。

## 只读核验现有演示

在 8176 已启动且本地验收记录仍存在时执行：

```powershell
.\.venv\Scripts\python.exe scripts/check_frontend_live_acceptance.py `
  --run-id 749a58b3-a2be-4102-a76f-cd87d0791646 `
  --run-id a2634656-9003-41cf-8f18-1fea535e0756 `
  --baseline output/frontend-live-acceptance-20261018/post-retirement-after/receipt.json `
  --output output/frontend-live-acceptance-20261018/manual-readonly-check
```

该脚本只 GET 现有健康状态、报告和 SSE，并读取账本；不新建工单或发起模型调用。服务端同时检查现有工单的当前准入配置，不创建运行。`--baseline` 绑定历史报告的原始源码与内容；新运行准入则必须匹配当前源码。输出目录应直接位于 `output/frontend-live-acceptance-20261018/` 下，以便找到相邻的 `server/` 与 `before/` 记录。不要删除账本来重置调用额度，也不要删除正在被服务使用的前端构建目录。

退役后已用相同 Flash、案例和单次预算从前端新增两次真实运行，应用指纹为 `748b2a02ac1b54a3`：case_001 用 14 次模型、5 次工具、43.750 秒；case_003 用 10 次模型、6 次工具、27.828 秒。两次都完成取证、逐项审阅、语义挑战和报告交付，刷新页面重新登录后可回读，不增加调用；联合检查退役前后报告、历史记录、SSE 和账本的 76 项检查全部通过。

这两次因 `NO_CURRENT_REVIEW_OBJECTION` 均未实际返工，本轮不声称已验证真实返工执行；对应控制路径由免费测试覆盖。退役前的 case_003 曾经历一次修订和最终审阅，但返工前已用完工具预算，未取得新证据。详见[退役后验收摘要](../evals/frontend-post-retirement-validation-20261018.json)及[原始验收摘要](../evals/frontend-live-validation-20261018.json)。

完成交付不等于根因准确。case_001 本次有 6 次重复查询决策；case_003 的挑战模型直接比较请求速率与连接数并据此支持容量不足，存在量纲和因果论证不足的问题。后者如实记录为 AI 复核发现、待人工核验，不作为人工金标。本次未修改业务代码或扩大质量结论。

## 评估分开阅读

下列质量数字属于各自冻结时点的源码。旧版退役提交本身未新增模型调用；后续补做的前端验收新增 24 次真实调用，验证退役后执行与交付，没有重新评分根因准确率。

| 要回答的问题 | 对应材料 | 解释边界 |
| --- | --- | --- |
| 重构是否减少协议中断和延迟 | [旧版／v2 冻结配对](../evals/frozen-version-validation-20261018.json) | 相同 Flash、案例和 12／8／180 预算；包含相关变体和重复，不是独立生产事故 |
| 是否能取得关键证据并定位根因 | [8 类机制跨案例评分](../evals/rca-cross-case-scorecard-20261018.json)、[设计与复现](../evals/rca-cross-case-validation-20261018.json) | 16／8／180 预算；端到端与预置证据分开统计；非盲 AI 裁决，不是人工金标 |
| 调查模型还是审阅模型造成差异 | [角色模型对照](../evals/autonomous-role-validation-20261017.json) | 按角色替换 Flash／Plus；小样本、有特定源码和预算 |
| 调高预算是否有收益 | [四组预算对照](../evals/budget-matrix-validation-20261018.json) | 各组单列，不回写默认预算 |
| 前端是否能交付报告和展示返工 | [真实前端闭环验收](../evals/frontend-live-validation-20261018.json) | 证明执行、存储与展示；不评分根因准确率 |

跨案例端到端严格根因内容正确为 21/24，关键证据取得为 24/24；后者只表示证据进入存储。仍有证据整合与语义审阅问题。两项比例来自开发案例和 AI 裁决，不能外推为生产准确率，也不能与不同预算的旧版实验相减。

原报告、消息、事件、查询结果、运行账本和冻结源码保持原路径。`evals/` 中的已提交摘要可用于定位本机 `output/` 原档；缺少原档时不能声称已重新核验哈希。历史实验见[归档索引](../archive/experiments/README.md)。
