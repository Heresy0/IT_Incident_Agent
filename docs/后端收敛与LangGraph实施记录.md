# 后端收敛与 LangGraph 实施记录

日期：2026-10-08。基于 `codex/incident-foundation` 的 `ce92a57` 继续开发；这里的收敛是项目架构调整，没有进行 Git 分支合并。按用户要求先完善后端，前端本轮未修改、未构建。

## 一、主链路收敛

`main.py` 默认改为 IT 协作 CLI；仍须显式选择 `--fake` 或 `--live`，live 仍须提供原有小预算，空参数不会调用模型。

FastAPI 默认只开放健康检查、身份、工单和统一运行接口。研究创建和研究记忆 API 默认关闭；历史代码及覆盖/审计成果保留。需要兼容时设置 `ENABLE_LEGACY_RESEARCH=true` 或显式使用 `local_run.py backend --legacy-research`。已有完整依赖环境仍可用。

新增 `backend/config/incident.py`，默认 IT 运行只读取模型与 PostgreSQL 配置；免费模式不要求模型 Key。`WorkflowService` 对研究图、Agent、记忆和研究专用校验改为按需导入，默认只注册 IncidentAdapter。执行池、计数、数据库事务和运行回放继续复用。

新增 `requirements-incident.txt`，使用原锁定版本约束 IT 直接依赖。默认后端启动和初始化只需 PostgreSQL；Milvus、搜索和研究记忆不在默认执行路径。该依赖清单本轮未新建虚拟环境安装；验证使用现有独立 `.venv`，另以禁止研究/Milvus 模块导入的独立进程验证默认 IT 链路。

## 二、真正的 LangGraph 编排

新增 `app/mult_agents/incident/graph.py`，使用 `StateGraph(IncidentState)`，注册六个节点：

| 节点 | 工作与工具权限 |
| --- | --- |
| Supervisor | 选择任务和角色，校验调度；无工具 |
| Investigation | 执行指标、日志、最近变更、负责人四种只读工具 |
| Knowledge | 检索运行手册、已确认历史工单两种只读工具 |
| Diagnosis | 根据登记来源形成事实、原因假设和处置建议；无工具 |
| Reviewer | 逐项判断支持程度，提出有范围的只读补查；无工具 |
| Finish | 记录终止原因及结果状态，不调用模型 |

`Collaboration` 保留原有角色操作和校验，拆分调度准备、单任务执行、诊断、复核，通过条件边切换角色。不是将原 while 循环包在一个图节点里。图状态记录阶段、下一节点、证据编号、任务/修订/复核数量、补查轮数和预算计数；同一运行中的证据与引用登记仍由原 coordinator 和共享 RunContext 管理。

一次流程为：接收工单与输入快照 → Supervisor 选择取证任务 → Investigation/Knowledge 串行执行 → Diagnosis → Reviewer → 最多一次补查与修订 → Finish → 原运行存储事务提交终态与 final 事件。

全局预算、预留收尾、最多四次调度、六个任务、一次补查和一次结构修复仍生效。图本身使用有限 recursion_limit。事件新增实际节点 phase，报告和运行 metadata 标记 `workflow_engine=langgraph`、`workflow_version=incident_graph_v1`，源码指纹包含新图文件。

运行和事件持久化继续支持回放。图没有增加 checkpoint 恢复功能，后端重启仍把中断运行标为失败；不会从初始输入再次执行旧运行。

## 三、两个已知缺口的修复

### 日志覆盖顺序

ToolExecutor 保存成功且未截断的、不指定 level 的日志查询覆盖。后续同类 ERROR 空查询先判断已有查询的时间范围和 error_code 是否真正覆盖；覆盖时不再制造 WARN/INFO 缺口。较窄窗口、不同类别、不适用 error_code、错误或截断查询仍不能消除缺口。

记录新增校验后的 start/end，便于今后判断具体窗口。仍不在 query_profile 中输出身份、自由文本或 error_code。重复查询拒绝仍不增加工具实际执行计数；不新增证据、不扩大工单范围。旧真实报告不改写，不能用新记录反推旧报告未保存的查询窗口。

### 获准补查没有实际执行

Reviewer 的结构化检查先经过原 ToolExecutor 的角色、身份、服务、时间范围与重复检查，再由 Supervisor 派发。进入对应角色节点后，程序通过同一 Executor 执行已批准检查，按实际尝试计数和登记来源，再让模型解释结果。

补查模型直接返回 final 也不能跳过实际执行。模型换用其他检查仍被 CHECK_NOT_APPROVED 拒绝；失败和截断检查继续保留未完成缺口。每条补查事件标记 `execution_source=approved_rework`。这只执行原有六种只读能力，人工扩容、回滚等操作仍不能通过补查执行。

## 四、统一 API 与存储兼容

新增 `backend/router/run_router.py`：

- `GET /api/v1/runs/{run_id}`：按当前身份读取运行。
- `GET /api/v1/runs/{run_id}/events`：SSE 回放，支持 after_seq / Last-Event-ID。
- 新诊断响应的 result_url / event_url 指向统一路径；SSE 增加 X-Run-ID，保留旧响应头。

旧 `/api/v1/research/runs/{run_id}` 与 events 地址保留只读兼容，默认不出现在 OpenAPI 主接口列表中。现有页面无需先改造即可继续读取工单运行；研究导航仍在旧页面中，默认后端不会执行它的研究请求，前端之后再统一整理。

没有删除数据或改动现有数据库表名。原 research_runs 表、身份配置兼容变量和监控指标名保留，避免破坏历史记录与已有部署。工单归属、唯一启动、输入修订、终态事务、operator 人工确认和私有经验检索机制保持。

## 五、验证结果

全部为免费控制或本地检查，没有新增真实模型、搜索或嵌入请求。

- 免费 IT 回归及原工程关键约束合计涉及 182 项：176 项首先通过，5 项 PostgreSQL 集成测试因未配置 RESEARCH_TEST_POSTGRES_DSN 跳过；唯一失败是新增时间窗口导致旧 query_profile 断言不匹配，更新为等价 UTC 时间和安全字段校验后，该项定向复测通过，累计177项通过。未再次无差别重跑整套。
- 新增的独立图节点、默认 IT API、兼容读取、SSE 续读、身份隔离和禁止研究/Milvus 导入检查通过。独立进程完成免费 IT 诊断。
- 已验证日志先广后窄与逆序处理、窄窗口不能覆盖整段窗口、重复拒绝不计实际工具调用、final-only 模型不能跳过补查，以及模型不能替换批准检查。
- 六个开发案例，SingleAgent 与多 Agent 共 12 次免费对照，机械检查 12/12、必要观测覆盖均为1.0，无 failed 运行。单 Agent 脚本调用合计13模型/37工具，多 Agent 合计44模型/21工具；这些为脚本计数，不代表付费调用或模型质量。
- case_003 协作控制路径完成3个任务、2轮诊断/复核、1次补查，使用12模型/5工具，较旧脚本少1次模型请求。
- 合成资料完整性检查和 Python 编译检查通过。CI 增加新后端收敛及负责人选择回归，仍不调用付费模型。

本地免费对照路径：`output/incident-backend-convergence/ccb9be8a-6e72-469f-8494-7830ff9e012a/`。公开安全摘要：`evals/incident/backend_convergence_20261008.json`。对照所用 implementation 指纹为 `bcbfb148e7ed74f9`；运行时 HEAD 仍为 ce92a57，working_tree_dirty=true 如实保存。

Windows 普通沙箱的首次 API 测试临时文件操作挂起，已停止该测试进程；使用正常临时目录权限完成免费 API 和汇总回归。不存在付费重跑。

## 六、未完成项与下一步

1. 接入至少一种真实日志/指标 Provider，把自由工单绑定到可信观测和权限范围；当前自由工单仍返回 OBSERVATION_PROVIDER_UNAVAILABLE。
2. 新修复的真实模型效果尚未复测，历史 M2/M3/M5 业务验收状态不改写。后续必须显式授权小预算执行并人工判断结论与处置建议。
3. 本轮未连接真实测试 PostgreSQL，数据库集成在既有 CI 中保留；没有新增数据库迁移。
4. 前端本轮不处理；后端接口稳定后统一去除研究导航并整理工单操作流程。
5. 自动执行修复、生产身份体系和图断点恢复不在当前交付范围。现阶段是诊断与处置辅助系统，实际处置与结果确认仍由运维完成。

本轮完成后端架构收敛与已知工程缺口修复，不宣布生产化或完整 M5 业务验收通过。
