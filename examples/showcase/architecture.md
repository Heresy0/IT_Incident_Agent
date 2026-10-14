# Agent 架构与职责边界

当前默认链路是三个模型角色与两类程序取证：Supervisor 规划查询，Investigation / Knowledge 执行已校验的只读计划，Diagnosis 形成候选诊断，Reviewer 复核并提出证据缺口。查询完成、报告通过与修复批准是不同状态。

## 运行链路

```mermaid
flowchart TD
    A[API / CLI] --> S[WorkflowService 接入与存储]
    S --> G[LangGraph 节点与路由]
    G --> C[Coordinator 流程与状态提交]
    C --> P[Planning 计划校验]
    C --> M[RoleRunner 模型调用与有限纠正]
    C --> L[Collection 取证与分支汇总]
    L --> T[ToolExecutor 只读取证]
    C --> R[Reporting 结果发布]
    M --> K[Protocol 动态契约与解析]
    T --> D[Providers / RAG 观测与知识]
    R --> O[报告 / 待确认项 / 审计]
```

## 模块如何分工

| 模块 | 职责与接口 |
| --- | --- |
| `app/workflow/coordinator.py` | 保留阶段入口与 `Collaboration` / `collaborate()` 兼容入口。负责下一节点、业务补查提醒、失败交接及跨阶段状态更新。 |
| `app/agents/role_runner.py` | `RoleRequest` 描述单次结构化请求；`RoleRunner` 管理本地消息、调用、验证及有限纠正。通过解析、校验、业务提醒和额度耗尽回调与工作流协作，不持有 Coordinator。 |
| `app/agents/protocol.py` | `RoleContractContext` 提供本次可见证据、引用、工具和问题台账。生成动态输出契约、去除重复 Schema 展示、解析兼容响应及提供角色纠正指令。 |
| `app/workflow/planning.py` | `PlanningContext` 提供校验所需的当前范围、台账与模式。`validate_plan()` 返回标准化决定，检查权限、引用、查询覆盖及补查预算；不执行工具或提交问题状态。 |
| `app/workflow/collection.py` | `CollectionRequest` → `CollectionOutcome` 执行程序或兼容 Agent 取证，返回结果、证据与引用台账、查询覆盖和调用统计。`ParallelBatch` / `ParallelOutcome` / `CollectionJoin` 明确分支准备、执行和汇总边界，不持有 Coordinator。 |
| `app/workflow/reporting.py` | `PublicationInput` → `Publication` 发布复核支持的内容；`ReportSnapshot` → 报告。保留暂定原因、待确认建议、未复核观测及缺口，响应与输入审计数据解除可变引用共享。 |
| `app/runtime/context.py` | 原子计数、原始调用与时间预算、纠正额度领取、用量与追踪。所有后续请求仍计入原预算。 |
| `app/tools/executor.py` | 角色工具权限、工单范围、查询去重、查询结果与字段引用台账。 |

动态契约约束模型能选择的编号和参数，程序继续独立校验。有效引用不证明因果成立，Reviewer 的语义判断仍需外部质量评测。

## 状态归属

- Coordinator 保存并提交跨阶段业务状态，LangGraph 状态用于路由投影和并行结果汇总。
- RunContext 管理预算、调用计数及事件发出；事件持久化沿用现有提交顺序。
- ToolExecutor 保存各角色查询台账。Collection 为并行分支创建独立执行器和 BranchContext，分支只更新自身台账；Coordinator 在汇总校验成功后安装分支执行器、保存任务并合并证据与引用。
- RoleRunner 仅管理一次请求的消息历史；业务提醒是否继续由 Coordinator 决定。
- Planning 和 Reporting 使用明确的输入对象。冻结 dataclass 只限制属性赋值，内部映射和模型对象是借用的读取视图，不宣称深层不可变；报告构建会复制公开响应中的可变数据。

两轮拆分保留原复核、诊断包及模式分支。完整 WorkflowState / WorkflowPolicy 和独立 Review 组件尚未迁移，以避免同一轮同时改变所有状态所有者。

## 取证组件的接口与并行处理

1. Coordinator 根据已校验任务建立 objective，决定顺序执行还是并行执行；补查继续顺序执行。
2. Collection 接收规范化参数，包括工具参数默认值。程序模式只执行明确的 `required_checks`，保留重复、失败、空结果和截断的查询状态，不自行解释故障原因。
3. 并行准备阶段按固定角色顺序分配原预算内的额度，复制 objective 并 fork 执行器台账。创建分支不消耗调用次数，也不扩大总预算。
4. 每个分支返回 `ParallelOutcome`。结果对象包含任务结果和该分支执行器；证据、字段引用、查询结果及覆盖来自同一个台账，避免另存一份可独立修改的查询状态。
5. `join_parallel()` 在任何共享状态提交前检查角色集合、批次和请求／执行器归属，按稳定顺序返回 `CollectionJoin`。它不安装执行器，不写入跨阶段状态。
6. Coordinator 提交各分支记录和证据，再根据原规则继续调度、转入诊断或停止。失败分支保留已取得的证据；只有历史知识时仍不能形成当前故障诊断。

内部 Graph 结果通道使用类型化分支结果，未引入 checkpoint 或跨进程序列化。请求和执行器归属校验针对同一次进程内运行；公开报告和持久化事件仍使用原 JSON 格式。

## 兼容与验证

提示词、输出契约、原预算、错误码、Graph 路由及报告字段保持兼容。模型请求失败后的有限纠正与业务补查提醒分开处理，两者均受原预算约束。报告模块不直接改写运行状态，由 Coordinator 显式提交缺口及升级团队。

新增 `tests/architecture/test_workflow_components.py` 验证组件无需协调器即可运行、纠正不能突破原预算、持续无效响应受控退出、计划校验不执行查询、不发布未复核建议，以及响应修改不会影响输入审计快照。

第二轮新增 `tests/architecture/test_collection_component.py`，覆盖无模型程序取证、复用不增加调用、objective 快照隔离、分支台账不提前安装、汇总顺序及批次校验、来源失败与异常脱敏、原始收尾预算，以及执行器／上下文不匹配拒绝。

本次本地另保存了重构前的六个脚本案例及一个默认程序取证案例。采用顺序取证，排除随机 UUID、采集时间与耗时后，比较输出、事件次序、动态契约审计、调用计数和停止原因。完整免费回归继续覆盖并行、补查、状态核查和诊断包路径。脚本响应仅验证程序行为，不作为真实模型质量成绩。

第二轮另外保存四个并行／失败基线：兼容 Agent 并行、默认程序并行、Investigation 失败、Knowledge 失败。比较时排除随机编号和时间，并按内容比较并发事件集合；真实并发、事件汇总顺序和角色调用归属由既有屏障测试验证。

```powershell
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m unittest tests.architecture.test_workflow_components
.\.venv\Scripts\python.exe -m unittest tests.architecture.test_collection_component
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py'
```

## 面试讲解

“我把模型协议、执行约束和业务流程分开：Supervisor 提出查询，Planning 校验计划，Collection 组织受限取证，ToolExecutor 校验并执行工具；RoleRunner 负责结构化调用、有限纠正和审计，Coordinator 决定是否补查及何时结束；Reporting 依据复核结果发布内容。并行分支使用独立台账和共享总预算，汇总节点统一提交状态。独立复核是职责和上下文分离，同基础模型仍可能出现相关错误，因此我保留单 Agent 对照及外部评测。”
