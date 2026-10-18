# 历史实验归档索引

整理日期：2026-10-18。项目统一入口为[根 README](../../README.md)，当前代码及演示说明见[v2 导览](../../examples/current-v2.md)。

旧版专属代码已另存[源码归档与恢复](../legacy/README.md)。本页继续保留历史实验材料的原址索引。

本目录把历史实验从当前阅读主线中归档。冻结材料绑定了原始路径、内容哈希和版本；脚本之间也存在复用。因此原实验文件、测试、评估数据及 `output/` 原档保持原位，通过本索引按阶段归档。旧页中的“当前”“最新”和预算只适用于原实验时点；本次没有改写旧成绩或把失败记录合并掉。

## 重构前

| 阶段 | 阅读入口 | 保留用途 |
| --- | --- | --- |
| 退役研究系统 | [研究源码归档](../research/README.md) | 仅追溯；其文件和清单受现有哈希测试保护 |
| 旧版默认链路 | [整理前 README](legacy-default-README.md)、[旧系统架构](../../examples/showcase/system-architecture.md) | 解释 legacy 入口与原始设计；实现继续保留用于回滚 |
| 原展示、独立集与局部提示优化 | [原 showcase 索引](../../examples/showcase/README.md)、[原评估说明](../../evals/README.md) | 保留迭代、失败和当时版本的对照，不视为 v2 最新成绩 |
| 审阅、返工与编排尝试 | [编排对照](../../examples/showcase/orchestration-autonomous-comparison.md)、[冻结审阅](../../examples/showcase/frozen-review.md) | 说明增加约束后仍遇到的语义和协议瓶颈 |
| 旧版收尾与停止优化 | [收尾验证](../../examples/showcase/validation-closeout.md)、[最小修复](../../examples/showcase/standard-flow-repairs.md) | 保留失败、局部收益和撤回理由 |

## v2 实施与对照沿革

| 阶段 | 冻结验收入口 | 怎样阅读 |
| --- | --- | --- |
| 阶段 0–3 | [初次 v2 实施](../../evals/autonomous-v2-validation-20261017.json) | 当时的冻结源码、保留改动、协议与自主调查验证 |
| 阶段 4–5 | [有界闭环验收](../../evals/autonomous-v45-validation-20261017.json) | 质疑与返工的收益和回退，不能单看完成率 |
| 完成契约、预算和调查问题修复 | [后续控制修复](../../evals/autonomous-controls-validation-20261017.json) | 不增加 Agent，保留各次真实模型与工程结果 |
| 角色模型替换 | [Flash／Plus 对照](../../evals/autonomous-role-validation-20261017.json) | 同代码按角色替换，不汇总为某模型普遍胜出 |
| 重构前后配对 | [冻结版本验收](../../evals/frozen-version-validation-20261018.json) | 同模型、案例和预算，分别读中断、补证、语义、成本与延迟 |
| 四组预算 | [预算矩阵](../../evals/budget-matrix-validation-20261018.json) | 判断追加预算能否带来有效进展，默认值未因此调整 |
| 跨机制根因评估 | [实验设计与复现](../../evals/rca-cross-case-validation-20261018.json)、[AI 评分](../../evals/rca-cross-case-scorecard-20261018.json) | 原始 48 报告、端到端与预置证据两种条件分别保留 |
| 当前前端演示 | [真实 Flash 闭环验收](../../evals/frontend-live-validation-20261018.json) | 当前阅读主线的一部分；归档本条用于定位其固定验收时点 |

## 本地材料与清理边界

[本地文档索引](../../docs/README.md)把最新复盘、面试稿和旧文档分开，`docs/` 继续由 Git 忽略。本机原始实验主要位于 `output/`，版本快照、原始响应、报告、事件、裁决来源和成本账本均保留。

重复内容不必然是冗余：不同案例的源文件快照、数据集版本以及失败日志可能分别被冻结清单引用，不能按名称、日期或哈希相同就删除。本次核对的重复日志因冻结引用或位于活动演示目录而全部保留；实际清理范围为源码／测试／评估目录中的 Python 字节码缓存。正在运行的 8176、前端 `dist/`、虚拟环境、数据库和服务账本不在删除范围内。

本次整理的删除数量、保留指纹、检查结果和本地审计文件定位见[整理验收记录](repository-tidy-20261018.json)。该记录只核验整理边界，不增加模型质量成绩。
