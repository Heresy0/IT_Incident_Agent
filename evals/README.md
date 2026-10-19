# 评估入口

当前运行代码为 `autonomous_v2 / closed_loop`；执行与交付、根因质量、历史版本比较分别核验。结果阅读入口为[当前 v2 导览](../examples/current-v2.md#评估分开阅读)。脚本控制、真实调用和 AI 裁决须分开统计；流程完成或内部审阅通过不代表根因正确。未知指标保留为 null。

首次阅读先看[评估阅读指南](../examples/evaluation-guide.md)，其中集中解释配对收益、跨案例成绩和完成／正确／交付的区别。各脚本的用途与执行边界见[脚本导航](../scripts/README.md)。

## 当前可用工具

| 目的 | 入口 | 边界 |
| --- | --- | --- |
| 免费检查开发观测数据 | `python -m evals.incident.check_fixtures` | 不评分模型判断 |
| v2 调查、审阅与闭环评估 | `scripts/evaluate_autonomous.py`、`scripts/evaluate_autonomous_closed_loop.py` | 按源码、输入阶段和预算分别统计 |
| 跨机制根因定位评估 | `scripts/evaluate_rca.py`、`scripts/summarize_rca_cross_case.py` | 端到端与预置证据分开；金标与裁决来源必须如实登记 |
| 证据整合与审阅对照 | `scripts/evaluate_rca_integration.py`、`scripts/summarize_rca_integration.py` | 使用固定任务和冻结源码 |
| 同模型、案例、预算的旧版／v2 对照 | `scripts/evaluate_frozen_versions.py` | 旧版在独立冻结源码中执行；不从归档混装导入 |
| 只读核验已有前端报告、SSE 和调用账本 | `scripts/check_frontend_live_acceptance.py` | 仅 GET；历史报告用 `--baseline` 绑定原始源码 |
| 已保存报告分类统计 | `scripts/summarize_runs.py` | 不调用模型；详见[统计口径](run-statistics.md) |

各评估脚本的参数以 `--help` 为准。计划、脚本控制和正式真实调用使用不同入口；本轮清理未发起真实调用。历史质量数字仍属于当时的源码、数据、预算及裁决来源。

## 历史材料

[独立合成集](independent/README.md)、[专项集](targeted/README.md)、[定向展示集](showcase_live/README.md)和工单修订集的原始数据、答案、冻结哈希及记录继续保留。它们对应的旧版比较器已退役；文档中的历史命令需要恢复完整旧版。剩余的 `provider.py` / `suite.py` 及失效的 `evaluate_showcase_live.py` 入口已移至[剩余旧版归档](../archive/legacy/residual-20261019/README.md)。

所有历史实验的统一入口是[归档索引](../archive/experiments/README.md)。旧研究系统的评估代码在 `archive/research/evals/`；其分数不计入当前 IT 诊断系统成绩。原始失败、partial、待审核项和调用证据保持原路径，不因代码清理重写。
