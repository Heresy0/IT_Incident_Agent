# Supervisor 协议容错与证据保留（2026-10-10）

本轮修复调度协议出错后整条链路提前终止的问题，复用原五角色 LangGraph、工具执行器、RunContext、诊断和复核门禁。没有增加角色、工具、预算或重试轮数，没有改动单 Agent 实现或冻结案例。

## 问题与改动

上次真实对照中，两个多 Agent 运行都在取证后出现 `REQUIRED_EVIDENCE_NEED`，一次纠正后又出现 `UNEXPECTED_TASKS`，Diagnosis 和 Reviewer 都没有运行。第一条错误原来没有字段详情，无法判断是核心问题被标为不需要，还是阻塞标志被关闭，不能凭错误码断言具体字段。

1. **缩小 Supervisor 输出契约。** 现在用 `need_updates` 提交已有 N 编号的局部评估，省略的项由程序保留。问题文字、用途及核心阻塞规则由程序维护。`additional_needs` 只声明新增触发原因或操作条件问题，程序以 pending 初始化，总数仍不超过六。旧完整清单仅保留输入兼容，不再出现在模型输出 Schema 中。
2. **安全处理冗余字段。** 明确的 diagnose/request_info/escalate/finish 动作携带 tasks 时，丢弃 tasks 并记录 `supervisor_fields_ignored`，即使冗余任务结构错误也不会执行或消耗纠正机会。保持实际动作：request_info 不会改成 diagnose，未复核 finish 仍被拒绝。dispatch 的权限、参数、引用和范围继续严格校验。
3. **提供具体纠正信息。** 同一回复的多个核心状态、阻塞、用途错误一起返回 N 编号、字段、错误码及允许值。旧清单删项仍被拒绝。无效引用、越界任务或检查优先校验，不能借另一个可恢复错误隐藏它们。全运行仍最多一次结构纠正。
4. **一次预算内收尾。** 收集阶段 Supervisor 的可恢复协议错误耗尽纠正机会，且已有当前观测、至少剩两次模型调用、剩余时间大于收尾预留时，图转入 Diagnosis → Reviewer。仅允许列明的协议错误；无效引用、越界检查、身份、账户或网络错误不走此分支。失败决定中的任务不执行，也不增加预算。
5. **保留失败报告中的观测。** `unreviewed_observations` 单独保存 Investigation 已通过来源字段校验的观测，最多八条，标注“引用通过程序校验，尚未获独立语义复核”。不混入已复核观测，不增加已批准的建议或修复权限。

## 执行流程与边界

正常流程仍为 Supervisor → Investigation/Knowledge → Supervisor → Diagnosis → Reviewer，按原规则最多一次复核补查。局部问题更新不调用工具，next_check 仍只是提案。

容错流程：校验失败 → 原有一次纠正 → 仍失败则判断错误类别、当前观测及剩余预算 → 满足条件后只做 Diagnosis 与 Reviewer 收尾。复核若提出新的补查，保存请求和缺口，以 partial / REVIEW_INCOMPLETE 结束，不重新进入已失败的调度者。预算不足、没有观测或严重权限/引用错误则结束并保留来源观测。

收尾不自动把 pending 改成 supported，也不自动宣告完成。只有原诊断完整性、F/H/A 独立复核、证据问题、查询覆盖及观测门禁全部满足，才发布 completed 报告。报告通过仍不等于工单解决、人工确认、修复批准或执行成功。协议错误和容错事件留在运行记录中。

## 源码

| 文件 | 本轮职责 |
| --- | --- |
| `app/agents/contracts.py` | SupervisorRequest、局部问题更新和新增问题契约，保留旧输入兼容 |
| `app/agents/prompts.py` | 简体中文调度提示与新字段说明 |
| `app/workflow/evidence_needs.py` | 合并清单、核心规则和字段详情，校验提案但不执行 |
| `app/workflow/coordinator.py` | 冗余字段审计、协议分类、一次收尾及未复核观测投影 |
| `app/workflow/graph.py` | Supervisor 失败时选择收尾或原终止路径 |
| `app/evidence/render.py` | 区分已独立复核观测与来源观测 |
| `tests/workflow/test_supervisor_resilience.py` | 九项免费协议回放及安全边界检查 |
| `tests/workflow/test_live_protocol_regressions.py` | 更新冗余字段规则，验证实际动作得到保留 |

## 验证结果和未完成项

- 新增九项回放通过：局部更新保留核心问题；实际失败序列的一次纠正；纠正耗尽后的诊断与复核；未知机制和待确认操作不能通过；预算不足保留事实；越界与伪造引用拒绝；无观测不收尾；复核补查保持待执行；新问题初始化且提案不执行。
- 全量 343 项，336 通过、7 项隔离 PostgreSQL 集成按既有条件跳过，耗时 40.247 秒，无未解决失败。首次测试启动缺少项目根目录参数，仅导致模块导入错误，已修正启动方式，无业务补丁。
- 原错误序列回放共七次脚本模型步骤、两次合成工具调用，在一次结构纠正后进入诊断与复核；持续协议错误的收尾回放也为七次/两次。不是付费模型结果，也不代表诊断准确率。
- 本轮付费调用为零，没有真实业务写操作、Git 提交或推送。原真实对照结果保留，不重算成修复后结果。真实效果尚未验证，未证明多 Agent 优势。

后续真实验证需另行授权小批次，使用既有冻结观测和评分规则，保留所有结果，不遇到 partial 就追加重跑。版本与免费证据见 [验证记录](supervisor-protocol-validation.json)。
