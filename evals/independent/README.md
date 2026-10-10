# 独立合成测试集 v1

本测试集先于下一轮优化建立，用于检查通用取证、诊断、协作与 Harness 行为。旧 `case_001`–`case_006` 继续作为开发回归集，原评分和报告保留。新增集不针对连接池案例要求固定字段，不预设多 Agent 必须更好。

## 分集与边界

共10个新合成事件：验证集6例、最终保留集4例。不是旧快照的数值或服务名变体；涵盖API、身份网关、导入、缓存、数据库、消息等业务。采用同一份评分规则，简单、复杂、正常和无法诊断的问题都保留。

| 验证案例 | 场景 | 主要考察 |
| --- | --- | --- |
| eval_001 | API请求连接前失败 | 明确故障的基础取证与调用开销 |
| eval_002 | 发布后认证拒绝 | 区分配置不匹配与依赖不可用 |
| eval_003 | 历史CPU经验误导当前导入失败 | 当前证据优先，历史不能替代观测 |
| eval_004 | 健康接口成功但业务接口失败 | 处理观测覆盖差异和表面矛盾 |
| eval_005 | 指标、日志来源不可用 | 有依据地保留缺口、请求信息和升级 |
| eval_006 | 计划维护内暂停处理 | 避免误报和不必要的恢复动作 |

`eval_007`–`eval_010`属于保留集。它们覆盖局部异常、处置前置条件、因果与时间线、投递语义等问题；此处不展开逐例答案。保留集默认不能计划或运行，需要明确选择 `--split holdout --release-holdout`。CLI解锁是防误用措施，不是文件权限或保密系统；源码仓库中仍可主动查看观测与参考答案。

这是一份**内部独立于原开发案例的合成集**，由同一开发者设计和实现，不是外部盲测。保留集的观测已做结构与Provider兼容检查，答案仅校验文件哈希，未用于本次模拟诊断或评分。若读到失败结果并据此调整系统，相关案例应转为回归使用，不能再次宣称未见验收。

## 目录与隔离

```text
fixtures/incident_independent/    工单、观测、手册、历史、固定渠道故障
evals/independent/manifest.json   分集与版本
evals/independent/answers/        仅离线评分读取的参考答案
evals/independent/rubric.json     统一语义评分表
evals/independent/freeze.json     23个数据/答案/规则文件的SHA256
evals/independent/provider.py     复用FixtureProvider筛选、字段白名单和授权
evals/independent/scoring.py      保存报告校验与人工语义评分
scripts/compare_independent.py   免费计划、模拟控制、显式真实对照
scripts/evaluate_independent.py  对保存结果进行离线评分
```

业务 `app/` 不导入新评测包。新入口复用单Agent、LangGraph协作、ToolExecutor和RunContext；旧对照的 `capture` 仅增加可选Provider工厂，默认行为不变。运行中的模型只能取得工单和过滤后的工具结果。所有选定案例的两个运行报告保存后，评分程序才解析参考答案；数据冻结校验读取答案字节计算哈希，不把答案解析或传给模型。

流程：冻结数据检查 → 明确案例与预算 → 两条链路各运行一次 → 保存原报告和哈希 → 离线机械检查 → 生成待确认人工评分表 → 汇总质量及成本。单、多执行顺序按中性案例编号交替。失败/partial不删除，每个案例必须具备两条记录才能汇总。每完成一个运行就保存 `run-index.json`；进程中断后的不完整批次保留记录，不自动重跑或恢复。

## 评分规则

每项0–2分，五项等权，细则在 `rubric.json`：

1. `outcome`：结论方向是否符合本案例可观测范围；合理的无法确定也可以满分。
2. `evidence_reasoning`：当前证据是否支持关键推论，是否正确处理替代解释、反证、历史与时间线。
3. `uncertainty`：是否区分事实、机制、触发原因和未知条件，避免把空结果当健康。
4. `actions`：建议的条件、风险、审批和验证是否适用；无修复依据时允许不提出修复。
5. `handoff`：是否适当交接、明确待补信息及已查到的负责人；无必要升级不扣分。

正式语义通过要求：前两项2分，其余至少1分，全部公开F/H/A按其表述得到支持，无不安全建议或虚构证据。逐项审阅中，“支持”指表述是否合理；有条件的假设不要求已经证明深层根因。每项必须有分数，所有公开结论和建议必须有评价及理由，评分者身份、报告哈希和总体理由不能为空。

机械校验单独检查事件、调用计数、快照、引用、契约和预算；无观测时合法空引用不是伪造。机械通过、工作流completed以及模型Reviewer的passed均不替代外部语义评分。程序不通过关键词匹配自动判定原因正确，不要求固定查询顺序，也不奖励不必要的返工或使用所有角色。

输出保留逐例分数、两条链路差值、调用/Token/耗时、错误/空查询、返工和退出原因。已审阅均分明确标注分母；未审阅为null。模板不包含workflow或内部复核结论，随机运行ID排序以减少提示偏差；原报告仍可识别链路，属于有限遮蔽而非严格双盲。脚本运行始终 `excluded_scripted`，不能批准为模型业务质量。费用未知保持null。

## 免费操作

在项目根目录执行，均无需数据库、Docker或密钥：

```powershell
.\.venv\Scripts\python.exe scripts/compare_independent.py --check
.\.venv\Scripts\python.exe scripts/compare_independent.py --plan
.\.venv\Scripts\python.exe scripts/compare_independent.py --fake --output output/independent-comparison
```

`--check`核对10例的结构、时间与范围，执行80次本地Provider查询；验证集答案检查评分维度，保留集答案不解析。`--fake`默认只跑6例验证集、12个控制运行。控制模型只读取一个可用指标并保留语义缺口，故意不编写案例诊断逻辑；只证明入口、引用、保存和评分隔离可运行，不能展示自主诊断质量或独立Reviewer效果。其输出partial是预期限制。

## 后续真实验证

本次没有执行真实模型调用。建议第一批选择验证集中的一个简单案例与一个有歧义案例，冻结当前模型、提示词和预算后各运行一次。真实入口必须同时提供 `--live --execute-live --cases ... --model-budget ... --tool-budget ...`，单批最多2例。执行前先用同样参数加 `--plan` 查看上限，获得用户明确付费授权后再运行。

两条链路共用模型、温度0、输出2200Token、SDK单次尝试、30秒请求超时，默认每条16模型/8工具/180秒，预留3模型/20秒；最多一次结构修正、一次协作返工。身份复用本项目Bearer认证，不打印凭据。真实模型也只查询合成数据，不读取生产监控或执行修复。

每批输出包含 `execution-plan.json`、原报告、`bundle.json`、`comparison.json`、`review-template.json`。填完人工表后，可离线汇总，无需重新调用模型：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_independent.py --bundle <运行目录>/bundle.json --reviews <运行目录>/review-template.json --output <运行目录>/reviewed-comparison.json
```

保留集在参数最终冻结且另行授权后再释放。新集只有10例，真实单次结果仍存在模型波动；不据此宣称生产准确率或多Agent普遍领先。不在本次完善提示词、增加调度门禁或业务服务。
