# IT Incident Agent

基于 FastAPI、PostgreSQL 和 Vue 的故障辅助诊断项目。v2 使用受预算约束的自主调查循环，从工单出发查询指标、日志与变更，形成带证据引用的诊断，审阅后交付报告和人工核查项。原 LangGraph 诊断链路已归档。

本页是统一阅读入口。分支 `codex/autonomous-investigation-v2` 的普通后端、CLI 和 CI 演示已统一到 **`autonomous_v2 / closed_loop`**。本机 8176 使用真实 Flash 和合成只读观测。旧版执行代码退出当前运行目录，完整源码与恢复方式见[旧版归档](archive/legacy/README.md)。准入未通过时明确拒绝启动，不再静默回退旧版。

## 从这里阅读

| 目的 | 入口 |
| --- | --- |
| 了解 v2 架构、当前演示和评估边界 | [当前 v2 导览](examples/current-v2.md) |
| 核验真实前端取证、审阅、返工与报告交付 | [前端验收记录](evals/frontend-live-validation-20261018.json) |
| 核验同模型、同案例、同预算的旧版／v2 对照 | [冻结版本配对验收](evals/frozen-version-validation-20261018.json) |
| 核验跨案例根因定位、补证及剩余错误 | [跨案例评分](evals/rca-cross-case-scorecard-20261018.json)、[实验与证据来源](evals/rca-cross-case-validation-20261018.json) |
| 阅读本地复盘与面试稿 | [本地文档索引](docs/README.md)（`docs/` 继续只保留在本机） |
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
