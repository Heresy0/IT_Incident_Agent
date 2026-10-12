# Reviewer 分阶段纠正额度试验

2026-10-12，按用户要求增加 Reviewer 额度试试。新增可切换 `reviewer_repair_mode='split'`；默认仍为 shared，不修改服务环境、模型或总预算。应用指纹 `2a7590991f8bab73`。

## 分配

- shared：盲审 ObservationReview 与最终复核 ReviewDecision 全运行共享一次纠正。
- split：盲审和最终复核分别全运行最多纠正一次，Reviewer 合计最多两次。每个诊断版本不会重新获得额度。
- 收集阶段仍全运行共享一次纠正，Knowledge/Supervisor/Diagnosis 不增加机会。所有请求继续原子计入全局模型预算；授权纠正不能使调用或时间上限增加。

严格引用、核心需求阻塞、目标覆盖等校验保持不变。单阶段连续失败后仍停止，网络或超时不因此获得隐藏重试。历史修复政策和报告保持原解释；新增政策 `collection_one_review_stages_one_v2` 根据实际 validation_repair 的 review_stage 逐阶段核对计数，伪造或超额记录不能通过机械评分。

免费测试证明两阶段依次发生格式错误后可以分别纠正、同阶段不能第二次纠正、收集仍只有一次、全局预算不足时会停止；额外计数与事件一致性也经过检查。完整回归 457 项：450 项通过、7 项数据库集成跳过，61.722 秒。

## 真实对照

同一源码、Qwen3.7-flash、fresh_003、程序取证模式，总预算 32 模型/48 工具/300 秒，2 模型/15 秒收尾。shared 与 split 各一次，固定先 shared 后 split，保存原数据和标准哈希，不重跑或覆盖结果。

| Reviewer 额度 | 消耗 | 结果 | 实际纠正 |
| --- | --- | --- | --- |
| shared | 15 模型、6 工具、98.156 秒 | partial / needs_information，REWORK_LIMIT，无执行错误 | 收集1、Reviewer1；Reviewer 为盲审空引用纠正 |
| split | 11 模型、5 工具、65.937 秒 | partial / needs_information，MODEL_OUTPUT_INVALID，失败节点 Supervisor | 收集1、Reviewer1；仍只有盲审空引用纠正，最终复核无需纠正 |

这次 split 组没有用到第二份 Reviewer 额度；最后失败于重复补查计划，不能据此证明扩大 Reviewer 额度改善了真实业务质量。shared 组也完成了复核，因此不能把这两次不同规划路径下的调用差异归因于纠正额度。只有一个合成场景每组一次，未独立人工评审，不能计算可靠通过率。

两次共 26 次模型请求、11 次合成工具尝试；无请求超时，费用未知。所有中间失败、停止原因和哈希见[运行摘要](reviewer-stage-budget-runs.json)，原始报告保存在 `output/investigation-stage/4c4ea694-edab-4e7c-845f-87d3b9633b3a/` 与 `640910be-ecdb-445c-977e-6d411808378c/`。

免费查看实验计划：

```powershell
$env:PYTHONPATH='app'
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe scripts/compare_investigation_stage.py --cases fresh_003 --arms programmatic --reviewer-repair-mode split
```

显式加 `--execute-live` 才产生真实请求。也可直接向 collaborate 传入 reviewer_repair_mode='split'；未指定时保留旧模式。当前保留实验，等待真实双阶段错误样本验证收益，不追加其他流程优化。
