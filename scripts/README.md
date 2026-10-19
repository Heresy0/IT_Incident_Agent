# 脚本导航

本页按用途索引当前 36 个 Python 文件。脚本保留原路径，避免破坏互相导入、仓库根目录定位、测试和历史复现。项目入口见[根 README](../README.md)，架构见[代码导览](../examples/architecture.md)。

## 先运行哪些命令

在仓库根目录、已有项目虚拟环境时，可执行以下免费检查。`--fake` 使用脚本模型与合成观测，不计模型质量。

```powershell
$env:PYTHONPATH='app;.'
.\.venv\Scripts\python.exe -m evals.incident.check_fixtures
.\.venv\Scripts\python.exe scripts/probe_incident_tools.py
.\.venv\Scripts\python.exe main.py --fake --case case_001 --output output/v2-demo
```

首次安装和普通开发环境见[本地启动说明](../README_LOCAL.md)。8176 的既有真实演示、账本及只读检查命令见[当前 v2 导览](../examples/current-v2.md#只读核验现有演示)。回读已有报告不会重新运行模型。

## 诊断与前端演示

| 文件 | 用途与执行边界 |
| --- | --- |
| [investigate_incident.py](investigate_incident.py) | `main.py` 委托的统一 v2 CLI；`--fake` 免费，`--live` 要求身份和显式调用预算 |
| [demo_frontend_live_acceptance.py](demo_frontend_live_acceptance.py) | 真实 Flash 的隔离前端演示；不带 `--serve` 只输出计划，启动服务后由用户发起诊断，沿用持久调用账本 |
| [check_frontend_live_acceptance.py](check_frontend_live_acceptance.py) | GET 核验已有真实运行、SSE、源码准入与账本；跨源码读取历史报告需提供 `--baseline` |
| [demo_frontend_acceptance.py](demo_frontend_acceptance.py) | 已保存报告回放与脚本控制演示；不是新增真实模型质量成绩 |
| [check_frontend_acceptance.py](check_frontend_acceptance.py) | 核验回放／脚本演示的 HTTP 与报告交付 |
| [frontend_demo_support.py](frontend_demo_support.py) | 演示身份、回环地址和隔离数据库校验的共享辅助模块；没有独立 CLI |

## 服务接入与工程核验

| 文件 | 用途与执行边界 |
| --- | --- |
| [setup_demo_auth.py](setup_demo_auth.py) | 初始化本机演示身份文件；不覆盖已有身份，不调用诊断模型 |
| [register_service.py](register_service.py) | 生成服务登记模板、免费校验配置；登记的是能力和语义，不是故障决策树 |
| [probe_registered_service.py](probe_registered_service.py) | 查询登记服务的实际观测，核对范围与证据；会连接登记的数据源 |
| [probe_incident_tools.py](probe_incident_tools.py) | 本地协议、工具绑定与计数探针；使用替身，不发网络请求 |
| [verify_checkpoint_equivalence.py](verify_checkpoint_equivalence.py) | 检查点关闭／开启的 42 组固定响应对照；仅允许隔离 `_test` 数据库，不调用真实模型 |
| [ingest_runbooks.py](ingest_runbooks.py) | 准备或导入排障手册；执行导入会写知识库，并可能调用向量接口 |
| [verify_runbook_rag.py](verify_runbook_rag.py) | pgvector 冒烟核验；默认计划，`--execute` 仅用于显式配置的隔离测试库 |
| [verify_postgres.py](verify_postgres.py) | PostgreSQL 集成核验；`--start` 启动专用临时测试库，按脚本说明准备环境 |
| [verify_docker_executor.py](verify_docker_executor.py) | 对专用验收容器实际执行重启与验证；属于修复执行器测试，不是只读检查 |

## 实验计划、运行与机制复现

| 文件 | 用途 |
| --- | --- |
| [evaluate_autonomous.py](evaluate_autonomous.py) | v2 自主调查与固定审阅实验 |
| [evaluate_autonomous_closed_loop.py](evaluate_autonomous_closed_loop.py) | 挑战、语义返工及闭环控制实验 |
| [evaluate_autonomous_roles.py](evaluate_autonomous_roles.py) | 固定其他条件，只替换指定角色模型 |
| [evaluate_budget_matrix.py](evaluate_budget_matrix.py) | 固定模型、案例与提示词的预算对照 |
| [evaluate_frozen_versions.py](evaluate_frozen_versions.py) | 独立冻结源码中的旧版／v2 配对；旧版不从当前归档混装导入 |
| [evaluate_rca.py](evaluate_rca.py) | 根因定位实验；端到端、充分证据和固定审阅阶段分别运行 |
| [evaluate_rca_integration.py](evaluate_rca_integration.py) | 固定任务的证据整合、审阅和引用条件对照 |
| [compare_autonomous_controls.py](compare_autonomous_controls.py) | 控制行为和各阶段结果的可比性对照 |
| [prepare_rca_integration_tasks.py](prepare_rca_integration_tasks.py) | 准备绑定观测、问题措辞与版本的整合任务 |
| [reproduce_rca.py](reproduce_rca.py) | 代表故障机制的本地可复现实验 |
| [reproduce_rca_independent.py](reproduce_rca_independent.py) | 独立机制案例的本地可复现实验 |

计划、脚本控制和真实模型模式须按各脚本 `--help` 区分。真实实验使用自己的显式总预算与调用账本；没有因为文档整理重新运行实验。故障机制复现不等于模型已经正确定位根因。

## 裁决与离线统计

| 文件 | 用途 |
| --- | --- |
| [review_rca.py](review_rca.py) | 准备、导入及核验根因评审材料；评审来源须如实登记 |
| [score_rca_ai_adjudication.py](score_rca_ai_adjudication.py) | 对绑定报告的 AI 裁决评分；不生成或冒充人工金标 |
| [summarize_rca.py](summarize_rca.py) | 根因实验汇总 |
| [summarize_rca_cross_case.py](summarize_rca_cross_case.py) | 跨案例、跨重复的根因与交付统计 |
| [summarize_rca_integration.py](summarize_rca_integration.py) | 整合与审阅任务汇总 |
| [summarize_frozen_versions.py](summarize_frozen_versions.py) | 冻结旧版／v2 配对结果汇总 |
| [summarize_autonomous_roles.py](summarize_autonomous_roles.py) | 角色模型对照汇总 |
| [summarize_budget_matrix.py](summarize_budget_matrix.py) | 预算对照汇总 |
| [summarize_runs.py](summarize_runs.py) | 已保存报告的执行、资源及显式评审统计 |
| [summarize_agent_iterations.py](summarize_agent_iterations.py) | 归纳运行中的角色活动；角色活动次数不代表根因判断正确 |

这些入口读取已保存材料，不重新调用诊断模型。正式质量数字需核对报告哈希、裁决来源、源码、案例、阶段和预算，具体口径见[评估阅读指南](../examples/evaluation-guide.md)。旧版已退役命令见[归档说明](../archive/legacy/README.md)。
