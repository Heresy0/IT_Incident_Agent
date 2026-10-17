# 诊断质量与同预算配对评测：最小实施

实施日期：2026-10-17。业务应用指纹仍为 `fa6e3b3b76224064`；未修改 `app/`、冻结数据集、参考答案和现有评分规则。本轮只做评测层、历史复用与免费验证，没有真实模型请求或外部修复。

## 1. 已实现范围

| 文件 | 作用 |
| --- | --- |
| `evals/metrics_protocol_v1.json` | 固定六个 validation 案例、三批顺序、预算、关键检查清单和统计口径 |
| `evals/paired_metrics.py` | 冻结计划、跨批次校验、统一评分表、主/子集指标、历史分组、单次批次领取 |
| `scripts/evaluate_paired.py` | 免费 prepare；离线 summarize；不初始化模型或查询真实观测 |
| `scripts/compare_independent.py` | 保留原接口，增加可选实验文件、重复轮次、顺序偏移和计划文件；真实调用显式传入已读取的模型配置快照 |
| `tests/evals/test_paired_metrics.py` | 22 项免费指标/入口测试；测试中模拟 live 标签仅用于检验评分器，不是业务质量证据 |

继续复用 `evals/independent/scoring.py` 的机械/五维语义评分、`evals/incident/comparison.py` 的来源与调用校验、`evals/run_statistics.py` 的运行开销统计。不新增依赖、数据库、页面或模型角色。

## 2. 冻结、比较与停止边界

prepare 保存模型名、有效请求配置的 SHA-256、调用/时间预算、应用指纹、数据集冻结哈希、评测代码与 requirements 哈希、全部预定案例/重复/顺序。地址、Key、Bearer 和原始 extra_body 不保存到计划；请求配置摘要不能用于恢复凭据。

执行前重新检查冻结条件。改了模型、端点/额外参数、业务代码、评测代码、数据或预算会停止，不发送请求。模型名仍可能是供应商可变别名，这不能保证服务端权重永久不变；计划也没有验证 Key、Bearer 或供应商额度。

同预算是两臂资源上限相同，不要求实际耗尽相同次数。默认 single 使用六工具循环，multi 使用三个模型角色和程序取证；免费 fake 为既有 agent 脚本取证路径，仅检验协议与汇总。该对照评价整套系统，不能单独归因于 Reviewer。

同一 manifest 的真实批次以 `attempts/repeat-N-batch-N.json` 独占创建领取文件，发送模型请求前保存输出目录。重复执行会被阻止；中断后检查领取记录与 run-index，不自动重放或替换失败。更换输出目录不会绕过同一 manifest 的领取保护。此为本机文件保护，不是跨机器权限系统；复制实验文件到另一目录可创建独立领取记录，不能把这种重复当原实验合法补样。

每次运行结束立即写原报告与 run-index，全部配对报告落盘后才读取参考答案及生成评分表。不覆盖历史报告。每个案例每臂每轮只允许一条记录；来源、顺序、配对和哈希不符拒绝合并。历史模式单独分组，不进入当前版本主成绩。

## 3. 指标与人工评价

主合格报告要求：机械校验通过、现有五维语义规则通过、status 不是 failed。现有规则为 outcome/evidence_reasoning 都是 2，其他维度至少 1，无虚构/不安全建议，所有已发布 F/H/A 的限定表达得到支持。合理 partial 可以合格；completed 或内部 passed 不自动合格。

主合格率仅在整个 manifest 计划运行齐全、均为 live 且全部正式人工评价后公布。未完成时 numerator/rate 为 null，保留计划分母及缺失槽位。`reviewed_subset_*` 和其他已审阅子集指标不能当完整成绩。

| 输出 | 口径 |
| --- | --- |
| qualified_reports | 合格报告 / 每臂计划运行总数；失败不移出分母 |
| critical_check_omission | missed / (observed + missed)，unavailable 单列 |
| advice_supported | 有依据的已发布建议 / 全部已发布建议；无建议为 0/0、rate=null |
| model_calls / tool_calls / duration_ms | 全部运行的已知样本数、均值、P50/P95；包含失败和 partial |
| token_usage_completeness | status=known 且 known_calls 与模型次数相等、数值完整的运行 / 已保存运行 |
| known_input/output_tokens | 已记录的词元小计，可能不完整；只有所有运行计量完整才有 complete_total_tokens |
| paired_outcomes | 每案例/每轮的质量分、耗时、调用和完整 Token 差（multi-single） |
| comparison | 合格率差使用百分点；开销相对变化为 (multi-single)/single，正值表示 multi 更高，零分母为未知 |

统一表 `human-reviews.template.json` 绑定 run_id、case_id、报告 SHA-256 和协议版本；缺省 approved=false。填写方式：

1. 根据原报告、真实来源及参考答案填写五维 0/1/2 分、逐条 F/H/A supported 与理由、虚构/不安全建议判断。
2. `published_content` 保留整项原内容。H 要按最终 unresolved/refuted 等限定表达审阅，不能把一个候选一律当成肯定事实；A 要看完整 condition/risk/expected_result/checks，不能只看标题。原文和来源不能改写。
3. 按冻结的关键检查填写 status/reason。available 只能 observed/missed；observed 必须列本次 observation 的真实 evidence_ids。eval_005 的指标/日志渠道固定为 unavailable，不计查询选择遗漏；其可查负责人仍可被漏查。
4. 验证器只能确认这些证据编号存在，是否真正满足 requirement 仍由人判断。两臂相同需求允许不同合法查询路径，不强制工具顺序。
5. 实际人工审阅者填写身份、总理由，并设 reviewer_type=human、approved=true。助手草案用 assistant，不计入正式质量；不能代填为人类确认。保存成新的 reviews.json，模板和原报告保留。

本轮没有正式人工评价，质量率均未知。没有用脚本或助手分数生成准确率。

## 4. 本次免费结果与产物

本机输出根目录：`output/paired-metrics-20261017/`（Git 忽略）。

| 验证 | 结果 |
| --- | --- |
| 新指标模块定向测试 | 22 项通过，包括冻结漂移、失败/partial分母、助手排除、引用/原文篡改、重复请求保护、配置快照和离线边界 |
| 原独立评分和统计器兼容测试 | 29 项通过（与前 20 项新测试合计 49 项）；最终全量也覆盖它们 |
| 六案例三批免费控制 | 12 运行、51 次脚本模型步骤、12 次工具尝试；12/12 机械通过；全部 partial，quality_status=excluded_scripted |
| 历史复用 | 2 个旧应用版本、8 份真实模型原报告；8/8 报告哈希与各自原 bundle 一致；生成 8 份统一待评分项 |
| Python 编译 / git diff --check | 通过 |
| 全量免费回归 | 723 项：703 通过、20 项隔离数据库检查因未配置测试DSN跳过，89.987秒；未在本轮重跑数据库集成 |

历史来源为 `output/claim-decision-comparison/final-independent/365ff7a2-a402-4fd6-8150-a28b05817a91/bundle.json` 和 `output/claim-decision-comparison/independent/0cc426bb-b833-4544-95a3-98a986cace07/bundle.json`，都是 eval_002/eval_004 的配对，旧应用分别 `11cd840a90cea7b3`、`b2ea1a4e52e5d122`，模型 qwen3.7-plus。选择这两个同案例的相邻历史版本是为了检验跨版本拆组，不能据此代表全部历史实验。

按当前机械评分器复查旧记录为 2/8 通过，另外 6 份出现 references_valid/diagnosis_contract 不通过。这里保留当前规则下的检查结果，不修改旧原文，也不把它说成当时已使用该规则的成绩或当前业务代码退化；解释为具体语义错误前还需人工核对兼容性和内容。

产物入口：

- `experiment.json`：冻结的首轮完整计划。
- `first-live-plan.json`：第一批免费预检，plan_only=true。
- `control-summary/metrics.md`、`metrics.json`：当前免费控制结果。
- `history/metrics.md`、`metrics.json`、`human-reviews.template.json`：旧记录分组、检查结果及待评分表。
- `history-source-audit.json`：八份原记录的哈希核对。

输出根目录外的本次日志为 `output/paired-metrics-tests.log`、`output/paired-metrics-new-tests.log`、`output/paired-metrics-full-tests-final.log`。初始全量日志保留，不覆盖。

## 5. 命令与第一批真实执行计划

所有命令在仓库根目录 PowerShell 执行。以下 prepare/plan 已实际执行；已有文件会拒绝覆盖，重新准备应使用新目录。

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe scripts/evaluate_paired.py prepare --experiment-id diagnosis-metrics-20261017-v1 --output output/paired-metrics-20261017/experiment.json
.\.venv\Scripts\python.exe scripts/compare_independent.py --plan --experiment-file output/paired-metrics-20261017/experiment.json --cases eval_001 eval_005 --model-budget 16 --tool-budget 8 --plan-output output/paired-metrics-20261017/first-live-plan.json
```

首轮三批：001/005、002/003、004/006；共 6 个不同案例、12 次运行，上限 192 模型调用/96 工具尝试。不是 12 个不同案例。

| 第一批项目 | 已冻结值 |
| --- | --- |
| 模型 | qwen3.7-flash；不切换模型 |
| 案例 | eval_001：连接前失败；eval_005：观测渠道不可用 |
| 顺序 | eval_001 single→multi，然后 eval_005 single→multi；全六例按案例编号交替两臂先后，第二轮可反转 |
| 数量 | 2 个案例、2 对、4 次运行 |
| 每运行上限 | 16 模型、8 工具、180 秒；收尾预留 3 模型/20 秒 |
| 请求设置 | temperature=0、max_tokens=2200、max_retries=0、30 秒请求超时、Chat Completions JSON 模式 |
| 第一批上限 | 64 模型调用、32 工具尝试；金额未知，不是承诺实际耗满 |
| 数据与操作 | 固定合成只读观测；不读取日常数据库、真实监控，不执行修复 |
| 当前状态 | 未启动，未验证实际凭据、连通性或额度；paid_requests_sent=0 |

`paid_requests_sent` 是准备计划时的快照，不是滚动账单；后续真实批次状态以 attempts、run-index 和运行报告为准。

下面是**后续得到明确启动指令才执行**的命令，本次没有运行：

```powershell
.\.venv\Scripts\python.exe scripts/compare_independent.py --live --execute-live --experiment-file output/paired-metrics-20261017/experiment.json --cases eval_001 eval_005 --model-budget 16 --tool-budget 8 --output output/paired-metrics-20261017/live/batch-1
```

要求本项目模型配置与本地 Bearer 身份可用；不把凭据写进命令。发生配置漂移时先停止核对，不覆盖原 manifest。写出批次报告后，用其真实 `bundle.json` 路径执行离线汇总，示意：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_paired.py summarize --experiment-file output/paired-metrics-20261017/experiment.json --bundle <第一批目录>/bundle.json --output output/paired-metrics-20261017/live/batch-1-summary
```

此时主分母仍是整个首轮每臂 6 次，未保存的 8 个槽位列出，主合格率为 null。三批齐全后，在同一 summarize 命令重复传三次 --bundle，并使用 --reviews 指向完成的统一人工评分表；输出到新目录。历史复用需要显式 --historical；脚本控制与真实批次不能混为同一实验成绩。

可选第二轮须新建 repeats=2 的整体计划，并预先声明两轮样本。当前 repeats=1 manifest 不能临时追加“最好一次”。第二轮 repeat-id=2/order-offset=1，24 次总运行仍只有 6 个不同案例。当前无需要自动追加第二轮。

## 6. 本轮可以写和仍不能写的指标

现在可写“建立版本冻结、哈希校验、人工语义评价、失败保留的配对评测流程；完成六案例十二次免费控制及八份历史报告离线复用”。这些是评测工程覆盖，不是诊断质量。

真实首轮全部运行并人工评分后，才可写“在六个已见合成案例、十二次真实模型运行中，单/多角色合格报告分别 a/6 与 b/6，相差 X 个百分点，同时报告漏查、建议有据率及调用/Token/耗时差异”。不保证多角色获胜，负收益与失败同样保留。

不能写生产准确率、外部盲测成绩、通用多Agent领先、人工工时下降、MTTR改善、费用下降或真实修复率；本轮没有这些证据。RAG、负载和Reviewer误拦率留在后续独立实验，不扩展本次范围。
