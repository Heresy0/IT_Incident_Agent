# 多 Agent 改进与真实模型验证

本轮针对调度、取证、诊断与复核间的协议失败进行实现和真实调用验证。`partial` 可以是合理结果：缺少业务证据、渠道不可用或关键歧义未消除时必须保留限制；协议错误、重复上下文挤掉有效证据则是需要修复的工程问题。当前图版本为 `incident_graph_v5_blind_review`。

## 实现变化

1. **纠正额度分离。** Supervisor、Investigation、Knowledge、Diagnosis 共享一次结构纠正；Reviewer 单独一次。并行取证仍原子共享取证额度。所有纠正计入原模型调用与时间上限，不是额外免费调用。报告保存 `repair_policy`、分组计数与事件；旧报告沿用旧上限。
2. **模型可见契约更具体。** Schema 只列实际 EV/REF 编号、已有 N/H/A、核心问题合法状态及角色可用工具；初始无观测时不提供虚构证据编号。日志类别说明产生记录的业务边界，过滤候选来自已取得的字段，不把用户描述当作结构化错误码。这些是模型提示约束，执行器仍独立验证。
3. **复核减少先验结论影响。** Reviewer 继续读取原始证据、草稿与程序查询台账，但不再接收 Supervisor 对 N 的支持状态和理由，也不接收取证者的候选机制摘要。同模型复核仍有相关错误风险，不能称为独立专家验收。
4. **建议区分执行方式。** 新增 `manual_check`，表示工具无法完成的人工只读核实，不带工具参数或 repair。非法只读建议在有限纠正后可被单独隔离，合法事实与假设继续进入复核。被拒建议另列，未发布、未执行；引用、身份、审批与修复能力错误仍拒绝。
5. **有界收尾。** 已有真实观测的调度格式或问题清单错误，可以转入一次 Diagnosis/Reviewer 收尾，不执行失败计划、不自动支持问题。权限失败不能走这个兜底。缺少有效建议或关键证据时依然 partial。
6. **上下文去重与语义范围。** 工具参数只在 `ReadOnlyCheck.oneOf` 保留一份，输入目录保存角色、工具名、说明和 Schema 位置。一个三任务回放的调度输入从 28,209 降为 25,364 字符，在原 28,000 上限内完成 13 模型/7 工具；此前会提前停止。工具说明明确查询的是工单服务，不能把本地变更查询当作远端部署核查。提示要求近端机制与未经确认的触发原因分开判断。
7. **Reviewer先盲审观测。** 真实模型工厂启用两阶段复核。第一次仅接收工单、原始证据、查询台账，选择当前观测的实质字段REF；程序展开引用并生成事实文字，不发布模型自拟的基线或因果陈述。未证实原因作为模型判断另列。不读取草稿、N状态或其他角色判断；第二次接收草稿并逐项复核，额外参考盲审说明。这仍是同模型的两次调用，不能保证消除相关错误。每次诊断修订后在预算允许时刷新，报告记录实际阶段；没有两次剩余额度则记录跳过，旧版盲审不会作为本次独立结论。免费脚本默认维持既有流程，新增专门用例验证盲审输入隔离、事实投影与预算上限，免费演示的旧计数不是两阶段真实调用的计数。

Supervisor和最终Reviewer仅需EV编号与字段路径，来源不再重复携带REF候选；Diagnosis和观测盲审仍有完整字段选择列表。完整来源快照与引用目录继续保存在服务端。

没有修改冻结的展示数据或参考答案，没有释放 eval_009/eval_010。保留用户原有工作区修改；没有提交或推送。单 Agent 策略与工具权限未调整，共有输出契约因新增 `manual_check` 和拒绝建议字段而改变。

## 验证方法

真实模型读取合成 Provider 数据，不访问真实 IT 服务，不执行修复。每条运行仍为最多 16 模型/8 工具/180 秒，预留 3 模型/20 秒；温度 0、max_tokens 2200、SDK 单次尝试、请求超时 30 秒。每个批次保存单双两组，包括失败；源码变化后的批次分别保留指纹，模型切换单独标记。迭代案例已经是已见回归数据，不能用最终表现宣称盲测成功。

完整执行投影见 [agent-improvement-runs.json](agent-improvement-runs.json)，包含所有本轮运行、失败原因、调用、Token、原报告 SHA-256、数据哈希与源码指纹。原始报告在本地 `output/agent-improvement-*` 目录，投影不包含凭据、租户身份或完整提示词。脚本 `scripts/summarize_agent_iterations.py` 仅做离线投影，不调用模型、不读答案。

机械检查只验证事件、预算、快照、引用与契约。评分器按新策略核对纠正事件和计数；旧报告仍最多一次纠正。对聚合的缺口列表，旧叶子检查仅使用前八项，完整列表仍通过 DiagnosisDraft 校验，避免把合法的多阶段限制误判成引用无效。这不是语义评分放宽。

正式人工语义评分仍待完成。下列解释是助手辅助审阅，不能填成正式准确率。

## 本轮结果

本轮共36条真实运行、228次模型调用尝试、104次合成工具尝试，全部保留。36条机械检查通过；费用未知，Token缺失的超时尝试不估算。模型调用计数是Harness记录，不声称等于供应商内部HTTP尝试数。

| 批次 | 模型 / 案例 | 单Agent 调用/工具 · 状态 | 多Agent 调用/工具 · 状态 | 多Agent停止原因 |
| --- | --- | --- | --- | --- |
| baseline | qwen-turbo / show_101 | 5/3 · partial | 10/2 · partial | MODEL_OUTPUT_INVALID |
| round1 | qwen-turbo / show_101 | 4/3 · completed | 2/0 · partial | NEEDS_INFORMATION |
| round2 | qwen-turbo / show_101 | 2/1 · partial | 12/3 · partial | REVIEW_INCOMPLETE |
| round3 | qwen-turbo / show_101 | 2/1 · partial | 7/3 · partial | MODEL_OUTPUT_INVALID |
| round4 | qwen-turbo / show_101 | 3/2 · completed | 10/3 · partial | MODEL_OUTPUT_INVALID |
| round5 | qwen-turbo / show_101 | 3/2 · partial | 9/2 · partial | MODEL_OUTPUT_INVALID |
| round6 | qwen-turbo / show_101 | 3/2 · completed | 14/4 · partial | REVIEW_INCOMPLETE |
| round7 | qwen-turbo / show_101 | 3/2 · partial | 12/2 · partial | REVIEW_INCOMPLETE |
| round8 | qwen-turbo / show_101 | 4/2 · partial | 11/3 · partial | REVIEW_INCOMPLETE |
| plus | qwen-plus / show_101 | 4/3 · completed | 7/2 · completed | FINISHED |
| transfer | qwen-turbo / eval_002 | 3/3 · partial | 12/5 · partial | REVIEW_INCOMPLETE |
| transfer | qwen-turbo / eval_005 | 3/4 · partial | 8/4 · partial | NEEDS_INFORMATION |
| plus-final | qwen-plus / show_101 | 4/3 · completed | 8/4 · partial | BUDGET_EXCEEDED |
| plus-compact | qwen-plus / show_101 | 4/3 · completed | 6/2 · completed | FINISHED |
| blind-plus | qwen-plus / show_101 | 4/3 · completed | 7/2 · completed | FINISHED |
| blind-transfer | qwen-plus / eval_002 | 4/5 · completed | 7/4 · partial | BUDGET_EXCEEDED |
| grounded-plus | qwen-plus / show_101 | 5/4 · completed | 10/3 · partial | NEEDS_INFORMATION |
| grounded-transfer | qwen-plus / eval_002 | 4/5 · completed | 12/5 · partial | REVIEW_INCOMPLETE |

最终字段盲审版本的show_101为10模型/3工具、127.094秒，保留远端字段拒绝事实，把“下游协议变更”H1评为uncertain/unresolved，并通过后续本服务变更核查建议；返回partial/NEEDS_INFORMATION。没有强制完成或批准修改。Supervisor关于默认脱敏政策的缺口理由没有直接证据，仍需改进。

同版本eval_002为12模型/5工具、162.062秒，取得AUDIENCE_REJECTED与auth_audience变更；H1的audience不匹配机制获支持，H2为何跨服务未对齐保持unresolved，人工白名单核查建议获支持。N2仍混入触发原因未知而uncertain，最终partial/REVIEW_INCOMPLETE；这个过度阻塞没有被程序强行改成supported。两条最终多Agent无运行错误，盲审陈述只包含来源字段，没有再次发布虚构基线。

最终配对的单Agent均completed，分别5模型/4工具、4模型/5工具；更快且提供变更建议，但认证单Agent的“已确认为根因”“所有登录请求”措辞超出当前证据。不能因此宣称多Agent综合获胜。逐项辅助审阅见[agent-improvement-assistant-review.json](agent-improvement-assistant-review.json)。

最终真实批次源码指纹为4636da06c56bde66。随后仅将盲审事实文字上限从400对齐服务端已有的500字符渲染上限，增加长事实重新校验回归；最终源码指纹55a1e3ed45ebf67d，该边界修正只有免费回归验证，未再次付费重跑。

最终全量402项（395通过、7项数据库集成按条件跳过）。面试三案例、串并行演示及showcase七项控制通过，均为脚本模型，不纳入真实质量计数。

## 尚存限制与简历表述

后续 qwen3.7-plus 的阻塞契约、查询审计和工具/JSON 输出分离见[诊断审计记录](diagnostic-audit.md)；该轮结果另存，不改写上面的历史运行。

模型仍可能选错渠道、跳过有价值检查、过度支持因果、误判建议适用性或给出矛盾缺口。程序能够验证引用真实、参数合法与权限范围，不能仅凭编号证明因果陈述成立。五角色使用同一模型会放大相关错误；每个角色增加的调用也必须证明有价值。当前没有总体准确率、跨模型胜率或生产恢复效果。

可以写“实现基于 LangGraph 的有界故障诊断工作流，支持动态取证、证据字段引用、独立角色复核、一次定向补查和人工审批；通过真实模型失败轨迹修复上下文重复、角色工具错配和纠正额度冲突，保留可审计的降级结果”。不要写“多 Agent 显著提升准确率”或“自动闭环修复生产故障”。
