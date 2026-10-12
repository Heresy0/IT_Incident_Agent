# Investigation 模型调用是否必要：可切换实验

2026-10-12，根据用户授权新增 `investigation_mode='programmatic'` 实验参数；默认仍为 `agent`。源码 `0c82a0fca77dc586`。原 Investigation 角色、流程节点和工具权限保留，实验仅跳过该角色的模型调用；Knowledge、Supervisor、Diagnosis、Reviewer 不删除。

## 实验方案

Supervisor 输出的具体检查继续通过原参数、角色及工单范围校验。程序在既有 ToolExecutor 中执行未尝试检查，逐次计入全局额度；已覆盖、失败或截断检查只复用台账，不自动重试。没有具体 checks 的模糊目标不会被推断为命令；预算不足则保留缺口。

阶段输出不生成候选原因或带自动引用的 findings，而是保存原始证据与真实查询状态。Diagnosis 已可读取这些来源和台账，负责选择引用、解释机制；执行完成不将 N 需求自动改为 supported。服务默认值和环境没有切换，用户可在实验脚本显式选择对照，或通过 collaborate 的参数使用该分支。

```powershell
$env:PYTHONPATH='app'
$env:PYTHONUTF8='1'
# 默认只检查数据哈希并展示计划，无模型请求。
.venv\Scripts\python.exe scripts/compare_investigation_stage.py
# --execute-live 才运行两个场景、两种方案各一次，产生真实调用。
```

两组仍使用相同调度预算计算和预留，程序分支并未额外放宽派发容量。默认脚本/旧接口保持兼容。

## 免费验证

四项新测试证明：明确查询实际执行且叶节点模型调用为 0；失败保留且不重试；没有具体计划或预算不足不执行；越界参数由既有执行器拒绝。执行结果不会自动生成原因或将机制需求判为支持。

完整回归 454 项，447 项通过、7 项数据库集成跳过，61.464 秒；格式检查通过。

## 同批真实对照

Qwen3.7-flash，每例每组各一次，固定顺序为 agent 再 programmatic；预算均为 32 模型/48 工具/300 秒、2 模型/15 秒收尾。新批次 `62aae63b-ac68-48af-801b-9cc68ac5a042`。数据和原参考标准的哈希调用前验证，模型不接触参考答案。失败不覆盖、不重跑。

| 场景 | 原 Agent | 程序执行 | 判断 |
| --- | --- | --- | --- |
| fresh_001：空间耗尽 | 13 模型、4 工具、73.187 秒；partial/needs_information；Supervisor MODEL_OUTPUT_INVALID | 8 模型、4 工具、64.718 秒；partial/needs_information；Supervisor MODEL_OUTPUT_INVALID | 两组都未取得 NO_SPACE 或 disk_usage。程序组取得错误率、变更、依赖日志及查空的配置日志；重复补查计划在后续被拒。执行改善不能代替选对证据，本例机制标准未达到 |
| fresh_003：日志不可用 | 3 模型、0 工具、14.406 秒；partial/not_performed；NO_TOOL_PROGRESS | 8 模型、4 工具、66.265 秒；partial/not_performed；Reviewer MODEL_OUTPUT_INVALID | 程序组确实取得延迟/错误率和变更，依赖/配置日志返回 SOURCE_UNAVAILABLE；来源失败被保留、未确认根因。但 Reviewer 输出及阻塞契约未通过，未形成完整交接 |

原组的 Investigation 模型调用为 3、2 次，程序组两例都为 0。四次运行共 32 次模型请求、12 次合成工具尝试，没有超时；费用未知。两组合计模型调用恰好都是 16 次：原组第二例早停、程序组推进更远，不能只根据汇总调用数判断效率。程序取证并不保证整个流程必然更省调用或耗时。

Supervisor 在各真实运行中给出的计划不同：原组第一例还调用了 Knowledge，程序组没有。这是自然运行波动，限制了严格因果归因；只有一批、两个合成案例，也未经过独立人工评审。不能据此给出可靠正确率或断言简化一定优于原方案。

## 当前结论

实验可验证地消除了“已批准检查还要 Investigation 再选择执行”的环节，并省去其引用格式输出。剩余不足集中在取证方向、Supervisor 补查规划、Diagnosis 建议与 Reviewer 语义/协议遵从，均需单独评价。当前保留可切换方案，不切换服务默认，不继续扩大修改。

[完整运行摘要](investigation-stage-runs.json)保留源码、预算、查询状态与报告哈希。原始报告位于 `output/investigation-stage/62aae63b-ac68-48af-801b-9cc68ac5a042/`。
