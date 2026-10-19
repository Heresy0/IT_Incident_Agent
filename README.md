# IT Incident Agent

基于 FastAPI、PostgreSQL 和 Vue 的故障辅助诊断项目。v2 使用受预算约束的自主调查循环，从工单出发查询指标、日志与变更，形成带证据引用的诊断，审阅后交付报告和人工核查项。当前由 LangGraph 编排 v2 各阶段，旧版诊断链路继续归档。

本页是统一阅读入口。当前分支 `codex/graph-checkpoint-recovery` 的普通后端、CLI 和 CI 演示统一使用 **`autonomous_v2 / closed_loop`**。本机 8176 使用真实 Flash 和合成只读观测。旧版执行代码退出当前运行目录，完整源码与恢复方式见[旧版归档](archive/legacy/README.md)。准入未通过时明确拒绝启动，不再静默回退旧版。

图检查点恢复按运行显式启用，默认关闭；开启方式、预算与未知响应边界见[检查点接入说明](examples/checkpoint-recovery.md)。恢复不会增加 Agent、调用额度或返工轮数。

## 从这里阅读

| 目的 | 入口 |
| --- | --- |
| 按顺序阅读架构、评估和面试口径 | [项目阅读导航](examples/README.md) |
| 核对图编排边界、行为对照和回滚方式 | [LangGraph 迁移说明](examples/langgraph-migration.md) |
| 启用进程中断恢复、核对预算与幂等交付 | [检查点接入说明](examples/checkpoint-recovery.md) |
| 按用途查找诊断、演示、接入与评估命令 | [脚本导航](scripts/README.md) |
| 了解 v2 架构、当前演示和评估边界 | [当前 v2 导览](examples/current-v2.md) |
| 核验剩余旧代码清理、兼容边界和回归结果 | [清理验收](evals/residual-cleanup-validation-20261019.json)、[原文件快照](archive/legacy/residual-20261019/README.md) |
| 核验真实前端取证、审阅、返工与报告交付 | [退役后真实验收](evals/frontend-post-retirement-validation-20261018.json)、[原始前端验收](evals/frontend-live-validation-20261018.json) |
| 核验同模型、同案例、同预算的旧版／v2 对照 | [冻结版本配对验收](evals/frozen-version-validation-20261018.json) |
| 核验跨案例根因定位、补证及剩余错误 | [跨案例评分](evals/rca-cross-case-scorecard-20261018.json)、[实验与证据来源](evals/rca-cross-case-validation-20261018.json) |
| 阅读可随仓库提供的面试稿 | [面试讲述简版](examples/interview.md)；本机扩展复盘索引为 `docs/README.md`，不随克隆提供 |
| 配置普通本地开发环境 | [Windows 启动说明](README_LOCAL.md) |
| 查找历史优化、失败记录和旧版流程 | [历史实验归档索引](archive/experiments/README.md) |
| 查找已退役的研究系统源码 | [研究归档说明](archive/research/README.md) |
| 恢复完整旧版诊断链路 | [旧版源码清单与回滚步骤](archive/legacy/README.md) |

## 快速核验

已有 Python 环境时，可先运行免费、离线的观测数据检查：

```powershell
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m evals.incident.check_fixtures
```

当前 8176 的只读验收命令见 [v2 导览](examples/current-v2.md#只读核验现有演示)。新机器需要按本地启动说明准备依赖；`output/` 中的原始运行记录和本地数据库不会随克隆自动生成。

## 怎样解释结果

`completed` 表示流程完成，内部审阅 `passed` 也不等于根因准确。`partial` 可以保存并交付报告，需结合信息缺口判断用途。前端闭环验收验证运行和交付；跨案例质量分数来自合成开发案例上的非盲 AI 裁决，尚无人工金标或生产准确率。

保留旧版、单 Agent 对照、冻结数据和失败记录，分别用于回滚、对照与复现。历史页中的“当前”“最新”和成绩均属于当时的源码与预算，不覆盖本页标明的运行状态。
