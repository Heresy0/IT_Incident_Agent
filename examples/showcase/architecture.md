# Agent 架构与职责边界

当前默认链路是三个模型角色与两类程序取证：Supervisor 规划查询，Investigation / Knowledge 执行已校验的只读计划，Diagnosis 形成候选诊断，Reviewer 复核并提出证据缺口。查询完成、报告通过与修复批准是不同状态。

## 运行链路

```mermaid
flowchart TD
    A[API / CLI] --> S[WorkflowService 接入与存储]
    S --> G[LangGraph 节点与路由]
    G --> C[Coordinator 流程与状态提交]
    C --> W[WorkflowState 运行状态]
    C --> B[WorkflowPolicy 模式与额度判断]
    C --> P[Planning 计划校验]
    C --> M[RoleRunner 模型调用与有限纠正]
    C --> L[Collection 取证与分支汇总]
    L --> T[ToolExecutor 只读取证]
    C --> V[Review 校验与补查交接决策]
    C --> R[Reporting 结果发布]
    M --> K[Protocol 动态契约与解析]
    T --> D[Providers / RAG 观测与知识]
    R --> O[报告 / 待确认项 / 审计]
```

## 模块如何分工

| 模块 | 职责与接口 |
| --- | --- |
| `app/workflow/coordinator.py` | 保留阶段入口与 `Collaboration` / `collaborate()` 兼容入口。负责下一节点、业务补查提醒、失败交接及跨阶段状态更新。 |
| `app/workflow/state.py` | `WorkflowState` 按控制流、取证、诊断复核和审计分组保存单次运行状态；`graph_projection()` 返回独立的路由摘要，不保存第二份预算或查询台账。 |
| `app/workflow/policy.py` | 冻结的 `WorkflowPolicy` 集中模式、派生能力、原有限次及取证／补查额度判断；`BudgetView` 提供本次决策所需的计数和耗时快照。只判断准入，不领取额度、调用模型或执行查询。 |
| `app/agents/role_runner.py` | `RoleRequest` 描述单次结构化请求；`RoleRunner` 管理本地消息、调用、验证及有限纠正。通过解析、校验、业务提醒和额度耗尽回调与工作流协作，不持有 Coordinator。 |
| `app/agents/protocol.py` | `RoleContractContext` 提供本次可见证据、引用、工具和问题台账。生成动态输出契约、去除重复 Schema 展示、解析兼容响应及提供角色纠正指令。 |
| `app/workflow/planning.py` | `PlanningContext` 提供校验所需的当前范围、台账与模式。`validate_plan()` 返回标准化决定，检查权限、引用、查询覆盖及补查预算；不执行工具或提交问题状态。 |
| `app/workflow/collection.py` | `CollectionRequest` → `CollectionOutcome` 执行程序或兼容 Agent 取证，返回结果、证据与引用台账、查询覆盖和调用统计。`ParallelBatch` / `ParallelOutcome` / `CollectionJoin` 明确分支准备、执行和汇总边界，不持有 Coordinator。 |
| `app/workflow/review.py` | `ReviewContext` 提供草稿目标、证据、问题及查询台账的读取视图。校验目标覆盖、当前支持、反证、缺口归属和查询提示；`ReviewRoutingContext` → `ReviewHandoff` / `ReworkDecision` 决定交接或补查资格，完成门禁返回停止原因。 |
| `app/workflow/reporting.py` | `PublicationInput` → `Publication` 发布复核支持的内容；`ReportSnapshot` → 报告。保留暂定原因、待确认建议、未复核观测及缺口，响应与输入审计数据解除可变引用共享。 |
| `app/runtime/context.py` | 原子计数、原始调用与时间预算、纠正额度领取、用量与追踪。所有后续请求仍计入原预算。 |
| `app/tools/executor.py` | 角色工具权限、工单范围、查询去重、查询结果与字段引用台账。 |

动态契约约束模型能选择的编号和参数，程序继续独立校验。有效引用不证明因果成立，Reviewer 的语义判断仍需外部质量评测。

## 状态归属

- Coordinator 持有一份 `WorkflowState` 并提交跨阶段业务状态，LangGraph 状态用于路由投影和并行结果汇总。
- RunContext 管理预算、调用计数及事件发出；事件持久化沿用现有提交顺序。
- ToolExecutor 保存各角色查询台账。Collection 为并行分支创建独立执行器和 BranchContext，分支只更新自身台账；Coordinator 在汇总校验成功后安装分支执行器、保存任务并合并证据与引用。
- RoleRunner 仅管理一次请求的消息历史；业务提醒是否继续由 Coordinator 决定。
- Review 读取候选与台账，不改写问题状态或执行工具。`ReviewAudit` 保存当前校验的提示拒绝与范围限制；Coordinator 即使在校验异常时也保存这些诊断记录，语义判定和问题台账仅在有效结果返回后更新。
- Planning 和 Reporting 使用明确的输入对象。冻结 dataclass 只限制属性赋值，内部映射和模型对象是借用的读取视图，不宣称深层不可变；报告构建会复制公开响应中的可变数据。

四轮拆分保留原诊断包及模式分支。状态聚合不等于支持持久化恢复；运行中断仍沿用现有 RunStore 失败处理。

## 状态与策略如何配合

| 状态分组 | 保存内容 |
| --- | --- |
| `state.control` | 当前阶段与下一节点、停止原因、调度／补查次数、待补查挑战、有限提醒和协议收尾标志。 |
| `state.collection` | 任务记录、复用记录、聚合证据和引用、派发队列、并行批次与计划、取证退出记录和诊断包状态。角色查询台账仍由 ToolExecutor 保存。 |
| `state.diagnosis` | 当前草稿和复核、证据问题台账、各版历史、独立观测复核、提示审计、交接请求、报告缺口及升级团队。 |
| `state.audit` | 原事件列表、协商记录、调度决定、结构纠正统计和工作流 span。共享 RunContext 时继续使用调用方提供的同一事件列表。 |

每次运行独立创建这些容器。Coordinator 的 `phase`、`tasks`、`draft` 等旧属性通过显式声明的 property 映射到聚合对象，读取、替换和原地修改使用同一份数据；没有 `__getattr__` 猜测映射或影子副本。现有阶段方法、诊断包及直接调用脚本继续兼容。

WorkflowPolicy 只保存原有取证模式、复核保护、纠正模式、并行与盲复核开关。`planning_only` 和 `role_corrections` 根据模式计算。兼容配置赋值会校验并替换冻结策略快照，旧快照保持原值，未登记模式不会部分生效。

Coordinator 从 RunContext 在锁内读取当前计数，构造短期 `BudgetView`；Policy 计算是否可以派发或补查，RunContext 在真实调用时继续原子领取额度。准入结果不会预扣预算，也不替代运行时执行门禁。普通补查保持最多 1 轮／4 次 Supervisor／6 个任务；满足原追加补查资格后最多 2 轮／5 次 Supervisor／7 个任务，总调用和时间上限不增加。两类取证最多并行 2 个角色，补查仍顺序执行。

Graph 投影仅包含路由、数量、证据编号和运行计数摘要；复制列表及计数映射，修改投影不会改写实际状态。并行结果仍通过原有类型化通道返回并在 join 后提交，未把执行器和整个业务状态移入 Graph 存储。

## 取证组件的接口与并行处理

1. Coordinator 根据已校验任务建立 objective，决定顺序执行还是并行执行；补查继续顺序执行。
2. Collection 接收规范化参数，包括工具参数默认值。程序模式只执行明确的 `required_checks`，保留重复、失败、空结果和截断的查询状态，不自行解释故障原因。
3. 并行准备阶段按固定角色顺序分配原预算内的额度，复制 objective 并 fork 执行器台账。创建分支不消耗调用次数，也不扩大总预算。
4. 每个分支返回 `ParallelOutcome`。结果对象包含任务结果和该分支执行器；证据、字段引用、查询结果及覆盖来自同一个台账，避免另存一份可独立修改的查询状态。
5. `join_parallel()` 在任何共享状态提交前检查角色集合、批次和请求／执行器归属，按稳定顺序返回 `CollectionJoin`。它不安装执行器，不写入跨阶段状态。
6. Coordinator 提交各分支记录和证据，再根据原规则继续调度、转入诊断或停止。失败分支保留已取得的证据；只有历史知识时仍不能形成当前故障诊断。

内部 Graph 结果通道使用类型化分支结果，未引入 checkpoint 或跨进程序列化。请求和执行器归属校验针对同一次进程内运行；公开报告和持久化事件仍使用原 JSON 格式。

## 复核组件的接口与决策

1. Coordinator 组织原 Reviewer 输入，通过 RoleRunner 取得输出，将当前证据、目标、问题与查询台账作为 `ReviewContext` 交给校验函数。
2. 校验函数返回规范化的 `ReviewDecision`，保留不确定性、原错误码及审计顺序。核心问题的阻塞属性仍由程序固定；历史知识不能单独建立当前原因，弱反证继续保留为未知。旧 blocker 推导的补查请求须重新通过同一校验。
3. 未登记、越界或已覆盖的查询提示记录在 `ReviewAudit.hint_status`，不执行查询，也不丢弃合法证据缺口。后续校验失败时，已产生的审计诊断仍由 Coordinator 保存。
4. Coordinator 根据有效复核结果更新问题台账。`resolve_handoff()` 只有在当前机制、有效下一步及必要覆盖无关键缺口时才返回非关键触发原因的人工交接；原请求与未确认原因继续保留。
5. 对仍需补查的请求，`decide_rework()` 按协议收尾、补查轮次、追加补查资格、原预算的顺序返回决定。额度计算由只读回调提供，组件不领取预算或派发任务。
6. Coordinator 保存交接／补查事件及下一节点。完成门禁仍区分诊断不完整、复核不完整、状态核查完成与普通完成，修复审批链路保持独立。

复核组件不持有 Coordinator；审计回调只发布原有事件，显式审计对象收集诊断效果。冻结视图中的映射和模型是借用的读取对象，不宣称深层不可变。程序保护不证明自然语言因果判断正确。

## 兼容与验证

提示词、输出契约、原预算、错误码、Graph 路由及报告字段保持兼容。模型请求失败后的有限纠正与业务补查提醒分开处理，两者均受原预算约束。报告模块不直接改写运行状态，由 Coordinator 显式提交缺口及升级团队。

新增 `tests/architecture/test_workflow_components.py` 验证组件无需协调器即可运行、纠正不能突破原预算、持续无效响应受控退出、计划校验不执行查询、不发布未复核建议，以及响应修改不会影响输入审计快照。

第二轮新增 `tests/architecture/test_collection_component.py`，覆盖无模型程序取证、复用不增加调用、objective 快照隔离、分支台账不提前安装、汇总顺序及批次校验、来源失败与异常脱敏、原始收尾预算，以及执行器／上下文不匹配拒绝。

第三轮新增 `tests/architecture/test_review_component.py`，验证复核不修改候选、问题或调用计数，未知引用与目标缺失拒绝，核心阻塞保留，失败校验保留提示审计，范围解绑不提升判定，以及关键缺口交接、追加补查和完成门禁。

第四轮新增 `tests/architecture/test_state_policy.py`，覆盖跨运行可变状态隔离、兼容属性的唯一数据归属、共享事件列表、Graph 投影隔离、实时预算读取、冻结策略替换及派生模式一致性，以及各个调用／工具／时间／补查限次门槛和收尾保留。

本次本地另保存了重构前的六个脚本案例及一个默认程序取证案例。采用顺序取证，排除随机 UUID、采集时间与耗时后，比较输出、事件次序、动态契约审计、调用计数和停止原因。完整免费回归继续覆盖并行、补查、状态核查和诊断包路径。脚本响应仅验证程序行为，不作为真实模型质量成绩。

第二轮另外保存四个并行／失败基线：兼容 Agent 并行、默认程序并行、Investigation 失败、Knowledge 失败。比较时排除随机编号和时间，并按内容比较并发事件集合；真实并发、事件汇总顺序和角色调用归属由既有屏障测试验证。

第三轮另保存四个复核／交接基线：非关键 trigger 交接、关键机制未确认、低预算退出与无效引用。排除随机编号和时间后，比较发布结果、审计事件顺序、调用计数和停止原因，同时继续核对前两轮的顺序及并行基线。

2026-10-15 第三轮验证：45 项针对性回归和 10 项新增边界测试通过；全量 668 项，648 通过、20 项数据库集成检查跳过，0 失败。15 个固定案例通过上述基线对照。首次全量测试停在 Windows asyncio 的本地 socketpair 初始化；不加载项目代码的独立探针在沙箱内同样阻塞，允许本机回环后成功，完整回归随后在允许回环的环境通过。没有为此修改项目或测试逻辑；数据库跳过项不计为通过，控制成绩不代表真实模型诊断质量。

2026-10-15 第四轮验证：14 项新增状态／策略边界测试和 31 项针对性回归通过；全量 682 项，662 通过、20 项数据库集成检查跳过，0 失败。前述 15 个固定案例继续通过基线对照。完整回归使用允许本机回环的环境，没有调用付费模型或真实修复服务。本轮验证程序行为兼容及状态归属，不证明真实模型质量或断点恢复能力。

```powershell
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m unittest tests.architecture.test_workflow_components
.\.venv\Scripts\python.exe -m unittest tests.architecture.test_collection_component
.\.venv\Scripts\python.exe -m unittest tests.architecture.test_review_component
.\.venv\Scripts\python.exe -m unittest tests.architecture.test_state_policy
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py'
```

## 面试讲解

“我把模型协议、执行约束和业务流程分开：Supervisor 提出查询，Planning 校验计划，Collection 组织受限取证，ToolExecutor 校验并执行工具；RoleRunner 负责结构化调用、有限纠正和审计，Review 检查复核结果、缺口和补查资格。Coordinator 根据 WorkflowPolicy 做额度判断，将有效结果提交到 WorkflowState 并推进阶段；RunContext 负责真实调用时的原子预算执行，Reporting 依据复核结果发布内容。并行分支使用独立台账和共享总预算，汇总节点统一提交状态。独立复核是职责和上下文分离，同基础模型仍可能出现相关错误，因此我保留单 Agent 对照及外部评测。”
