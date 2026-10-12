# 离线分类统计

`scripts/summarize_runs.py` 只读取明确指定的运行 JSON 或比较 bundle，生成 `statistics.json`、`statistics.md` 和 `human-reviews.template.json`。不读取密钥、初始化模型、查询服务或改写历史报告，也不改变现有评测评分器。

```powershell
.\.venv\Scripts\python.exe scripts/summarize_runs.py --bundle output/<批次>/bundle.json --output output/statistics/first
# 可重复 --report 或 --bundle，输入必须具有不同 run_id。
.\.venv\Scripts\python.exe scripts/summarize_runs.py --report output/<批次>/<run_id>.json --output output/statistics/single
```

不同模型、执行模式、源码/数据版本、预算和流程配置分别分组；缺少源码指纹时每条报告单独统计。`prompt_version` 包含服务名、已观测字段等动态工具Schema，逐条保存并在分组内列出，不因此拆分同版本的不同案例；提示词实现变化由源码指纹隔离。失败与 partial 保留。bundle 的报告路径、哈希和配置必须与保存记录一致。此入口是描述统计，不替代已有冻结数据检查、同预算配对评测或诊断质量验收。

## 自动统计

- 协议校验失败尝试：按角色与原因分类，包括随后已纠正的失败；同时报告发生错误的运行比例与纠正次数。
- 缺少事件记录时协议与查询统计为未知，不把缺失记录当成零错误；分组同时列出事件可用运行数。
- 工具查询失败、空结果、截断、预算退出及停止原因。
- 声明检查：使用最终 `task_results.completion.checks`，按角色、工具和参数去重。`not_executed`、`failed`、`truncated`、`missing_samples` 分开记录；它只描述计划执行情况，不能发现计划本身遗漏的关键问题。
- 系统运行耗时：均值、最近秩 P50/P95 和已知样本数；模型/工具调用和已记录 Token。它们不等于人工工时或故障恢复时间。

## 人工质量评价

复制生成的模板到自己的评价文件。每条记录保留原 `run_id`、`case_id`、`report_sha256`，由人类填写 `reviewer`、`rationale`，完成以下字段后设置 `approved: true`。未批准不计分，助手评价设为 `reviewer_type: assistant`，不会进入人类质量比例。脚本运行禁止批准为业务质量成绩。

- `critical_checks`：由评价者独立制定必要检查清单，每项有唯一 `check_id`、`reason` 和 `status`。`observed`/`missed` 进入漏查分母，`unavailable` 和 `not_applicable` 单列；源不可用不算查询选择遗漏。空清单表示未评价，比例为 null。
- `conclusion_correct`：正确为 true，错误为 false，未评价为 null；明确无法确定的合理结论也可以为 true。
- `actions`：逐项评价所有已发布建议的 `supported` 与 `reason`，保留原始建议内容。无建议不自动算正确，建议比例无分母为 null。
- `review_blocks`：逐项评价每个稿次中 `uncertain`/`not_supported` 目标，保留 `revision`、`target_id` 和原 assessment。`unjustified: true` 表示外部评价认为误拦；内部 uncertain 不自动等于误拦。这里覆盖目标级判断，不包括未关联目标的问题级 blocker。
- `handoff`：声明 `required`；需要交接时填写 `actionable` 和 `reason`。有效交接应明确缺什么、下一步做什么，以及接手角色或已取得的负责人；合理 partial 可以有效交接。只在需要交接且已评价的运行中计算比例。
- `human_effort`：可选的同任务实测 `baseline_ms` 和 `assisted_ms`，必须成对填写。差值为前者减后者，负数表示增加工时；不以模型耗时推算人工节省。

```powershell
.\.venv\Scripts\python.exe scripts/summarize_runs.py --bundle output/<批次>/bundle.json --reviews output/statistics/human-reviews.json --output output/statistics/reviewed
```

批准的评价必须覆盖全部原始建议和目标级复核阻塞；错误哈希、修改原文、重复或陌生运行均拒绝。各比例输出独立的 numerator、denominator、rate；没有正式评价时为 null，不能把未评价视为通过或失败。

## 显式价格与费用

可选 `--pricing output/statistics/pricing.json`，价格由使用者根据自己的合同/账单提供，不内置厂商价格。例如以下仅为格式示例：

```json
{"currency":"CNY","models":{"your-model":{"input_per_million":2,"output_per_million":4}}}
```

只有 live 运行、模型名精确匹配、Token 用量为 known 且 known_calls 等于模型调用数时估算。存在任一未知运行时整组 total 为 null，同时保留 known_total 与 known_runs。此估算不处理缓存折扣、阶梯价格、工具或基础设施费用；不要当成实际账单。

输出目录中的文件已存在时拒绝覆盖，重新评价应使用新目录。
