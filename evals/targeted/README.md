# 多 Agent 专项能力测试集 v1

本集用于验证多 Agent 是否在历史误导、证据冲突、区分性取证上获得收益；不预设它获胜。保留单 Agent 全部六个工具、原提示词和原执行能力。应用代码没有为新案例增加规则、角色或查询顺序。

采用人工设计的合成事件，不读取真实服务、任务或知识库数据。由同一开发者编写，作者知道参考答案，因此不是外部盲测。旧开发集、独立集及历史报告继续保留。专项集是有目的的压力测试，不能用它替代代表性业务测试或生产准确率。

## 案例与分集

总计8例：6个专项事件（三个类别各两个不同业务事件），另有2个普通对照。

| 开发案例 | 场景 | 预期考察 |
| --- | --- | --- |
| probe_001 | 推送积压，历史扩容有效，但当前请求被schema拒绝 | 当前观测与历史经验交叉核验，避免照搬旧方案 |
| probe_002 | 健康面板绿色，当前导出写入失败，同时存在旧实例报警 | 区分读写接口、身份、实例和时间，识别表面矛盾 |
| probe_003 | 请求超时，被怀疑下游慢，实际需区分本地排队机制 | 选择有区分力的当前证据，据此取舍候选机制 |
| probe_007 | 未报告失败的队列/worker状态核查 | 简单任务的调用开销、范围诚实及避免不必要修复 |
| probe_008 | 失败指标异常，日志和变更来源不可用 | 合理保留未知、准确区分来源错误和空结果 |

`probe_004`–`probe_006`是三个类别的保留验证事件，涉及不同服务和不同故障机制，不是原快照改名或调整几个数值。默认不运行、不解析答案；必须显式选择 `--split holdout --release-holdout`。这只是防误用开关，仓库文件仍可阅读。结构检查会访问保留观测并计算答案哈希，不代表作者未见；一旦用其输出调整系统，应标为已见回归案例。

同一个事件的单/多链路使用同一模型、温度0、2200输出Token上限、SDK单次尝试、30秒请求超时、身份、数据、工具权限和总预算。每条默认16模型/8工具/180秒，预留3模型/20秒。单批真实执行最多2例，两条链路顺序按编号交替，每条只运行一次。失败和partial保留，禁止自动重跑或只挑最好的一次。

## 数据、答案与运行隔离

```text
fixtures/incident_targeted/       Agent可见的中性工单、观测、手册和历史
evals/targeted/manifest.json      分集、专项/普通类别和版本，仅评测入口读取
evals/targeted/answers/           仅保存报告后的离线评分读取
evals/targeted/rubric.json        预先声明的评分与优势判据
evals/targeted/freeze.json        19个数据/答案/规则文件的SHA256
evals/targeted/provider.py        复用FixtureProvider过滤与授权
evals/targeted/scoring.py         保留完整语义的逐项评分和跨批汇总
scripts/compare_targeted.py       复用既有同预算对照执行器
scripts/evaluate_targeted.py      无模型调用的离线评分入口
```

不向Agent提供案例类别、参考答案、能力评分或必查清单。运行期Provider只读工单与过滤后的工具数据。所有选定案例的两条报告保存之后，才生成参考答案评分表。历史记录具备适用版本并可正常检索，但历史事件不能代替当前观测；不强制读取历史或使用全部角色。

合成检索沿用原Provider的词元匹配，没有引入新的RAG能力。可用症状中的服务/业务词检索，例如 `delivery`、`export`、`pricing`；中文任意子串不保证召回。两条链路条件相同，检索失败应如实保留。

## 怎样判断优势

主评分仍是五项0–2分：结论、证据推理、不确定性、建议、交接。必须人工确认，不通过关键词自动判定原因正确。`completed`、内部`passed`、取证数量和返工次数都不直接计作质量收益。

专项评分表另有逐例能力项0–2分，必须附报告或轨迹依据：历史适用性、跨来源机制、范围冲突、建议适用性、区分性证据、依据证据取舍机制等。先取对证据且不需返工也可满分；不得为体现Reviewer而故意让单Agent出错。

F/H/A逐项评分保留完整原表述，包括假设的状态/层级、证据解释，以及建议的条件、风险、审批和修复登记。待确认建议另用P编号，明确不可执行。`supported=true`表示“该表述及其状态合理”：H的`refuted`评的是排除判断是否合理，而不是要求被排除原因成立；pending评的是待确认的表达和边界，不当成已获批准的修复。

预先规定一个**描述性优势信号**：完整覆盖三个专项类别，至少两个类别出现多Agent总分高出至少2分，多Agent外部语义和机械检查通过、结论分不低于单Agent。少于三个类别或尚未评分时，`threshold_met=null`。该信号仅解释本批小样本，不表示统计显著或普遍领先。普通对照单独报告，不混入专项胜率；额外模型调用、Token与耗时一起披露，费用未知保持null。

共享程序对越权或审批的阻断属于Harness成果，不计为多Agent独有收益。若要归因Reviewer的收益，人工检查原报告中的初稿、复核意见、补查及最终结论，确认它是否纠正了具体问题；单/多对照本身不足以隔离Reviewer的因果贡献。

## 免费操作

在项目根目录执行，无需密钥、数据库或Docker：

```powershell
.\.venv\Scripts\python.exe scripts/compare_targeted.py --check
.\.venv\Scripts\python.exe scripts/compare_targeted.py --plan
.\.venv\Scripts\python.exe scripts/compare_targeted.py --fake --output output/targeted-comparison
```

结构检查覆盖8例和64次本地Provider查询，保留答案不解析。免费模拟只运行5个开发/普通案例的10条链路，使用既有通用SmokeModel，只读一个指标并保留原因缺口。它不根据案例答案诊断；因此partial是预期限制，所有模型质量始终`excluded_scripted`。模拟中的模型步数是脚本响应次数，付费调用为0。

## 有限真实对照与离线汇总

本次没有授权或执行付费模型调用。建议第一批先选历史误导与范围冲突两例，先看免费计划：

```powershell
.\.venv\Scripts\python.exe scripts/compare_targeted.py --plan --cases probe_001 probe_002 --model-budget 16 --tool-budget 8
```

这对应4条运行，最多64次模型请求、32次合成工具尝试；实际调用可能较少，费用尚不能估算。**得到用户针对该批的明确付费授权后**才执行：

```powershell
.\.venv\Scripts\python.exe scripts/compare_targeted.py --live --execute-live --cases probe_001 probe_002 --model-budget 16 --tool-budget 8
```

第二批可在另行授权后选择 `probe_003 probe_007`，第三批选择 `probe_008`。保持模型、代码和预算冻结，不根据第一批结果改代码后又把后续结果混成同版本成绩。每批保留原报告、哈希、执行计划、`bundle.json`、待填`review-template.json`和`comparison.json`。此方案不是一次性授权三批，保留验证也需要另行授权。

填完人工表后，仅评分保存结果，无需再调用模型：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_targeted.py --bundle <批次>/bundle.json --reviews <批次>/review-template.json --output <批次>/reviewed-comparison.json
```

多个有限批次可合并人工表的`runs`数组，再离线汇总：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_targeted.py --bundles <批次1>/bundle.json <批次2>/bundle.json <批次3>/bundle.json --reviews <汇总人工表>.json --output <汇总结果>.json
```

汇总会拒绝重复案例、变更后的模型/代码/预算/分集、报告篡改和未知已批准评分，避免挑选最好结果。开发与保留分集分别汇总。每个报告可以只评分一次或修正原评分，但不因此重新调用模型。全部失败、平局和单Agent胜出都应展示。

当前交付是案例和可执行评测链路；是否存在明显多Agent优势，仍需真实对照及人工审阅才能回答。

## 本次离线验收（2026-10-10）

新增10项边界和评分控制测试通过。完整免费回归334项，327通过、7项隔离PostgreSQL集成测试跳过；最终针对本轮评分和计划调整的检查通过。8例数据执行64次本地Provider预检；开发/普通5例的10条模拟报告全部通过6项机械检查。旧独立集23个冻结文件校验通过。应用源码指纹保持`11e895e2f331bf28`。

模拟共44次脚本模型响应、10次合成工具尝试，真实付费调用0、真实服务写入0，保留案例诊断运行0。结果全部标为`excluded_scripted`，语义质量与优势判据均未取得成绩。验证记录见[专项集离线验收](../../examples/showcase/targeted-suite-validation.json)。

## 首批真实对照（2026-10-10）

随后经用户授权，`probe_001`、`probe_002`各single/multi一次，qwen-turbo、每条16模型/8工具上限。共22次模型请求、11次合成工具尝试，已知输入116657/output7521 Token，无追加重跑、真实服务写入或保留集诊断。本批未收到Provider额度、认证或网络错误；不据此推断剩余额度。

| 案例 | 链路 | 模型/工具 | 耗时 | 结果 |
| --- | --- | --- | --- | --- |
| probe_001 | single | 4/3 | 12.687秒 | partial，DIAGNOSIS_INCOMPLETE |
| probe_001 | multi | 7/2 | 28.406秒 | partial，MODEL_OUTPUT_INVALID |
| probe_002 | multi | 9/5 | 39.687秒 | partial，MODEL_OUTPUT_INVALID |
| probe_002 | single | 2/1 | 5.859秒 | partial，DIAGNOSIS_INCOMPLETE |

两条单Agent的H/A为空，未取得关键dependency日志。两条多Agent在Supervisor均先触发`REQUIRED_EVIDENCE_NEED`，一次纠正后触发`UNEXPECTED_TASKS`，未进入Diagnosis/Reviewer；核心证据需求的第一项失败日志缺少字段详情，不能区分是`not_required`还是`blocking=false`。范围冲突案例多Agent取得身份日志及配置变更，比仅查指标的single有更多相关观测，但未形成正式诊断。两条链路都没有知识检索，无法据此验证历史检索后的交叉复核收益。

四份报告的六项机械检查通过，语义评分仍待人工确认，尚未证明多Agent优势。本批主要暴露协议推进稳定性和提前收尾，不能直接评价未执行的Reviewer效果。源码/评测实现指纹及数据冻结在批次内保持一致，没有为结果改题、改代码或追加调用。[原始统计](../../examples/showcase/targeted-first-comparison.json)、[助手定性审阅（非正式人工评分）](../../examples/showcase/targeted-first-assistant-review.json)保留失败记录；普通对照与第三专项类别尚未运行。
