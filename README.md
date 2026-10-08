# IT Incident Agent

面向内部服务故障工单的诊断与处置辅助系统：读取工单范围内的指标、日志和变更，形成有引用的原因假设，独立复核，输出处理建议，最后由运维人员确认处理结果。

当前后端已整理为独立 IT 系统：**研究执行与启用开关已移除，五个 Agent 通过真实 LangGraph 节点运行**。模块按 API、Agent、工作流、工具、Provider、证据、运行控制、监控和存储组织；前端留到后端稳定后整理。

## 当前后端架构

```mermaid
flowchart TD
    API[FastAPI 工单与运行接口] --> SERVICE[统一执行池 / RunContext / RunStore]
    SERVICE --> S[Supervisor]
    S --> I[Investigation]
    S --> K[Knowledge]
    I --> S
    K --> S
    S --> D[Diagnosis]
    D --> R[Reviewer]
    R -->|最多一次只读补查| S
    R --> F[Finish / 持久化结果与事件]
    I --> TOOLS[指标 / 日志 / 变更 / 负责人]
    K --> KNOWLEDGE[运行手册 / 已确认历史工单]
    F --> HUMAN[人工处理与 operator 确认]
    HUMAN --> HISTORY[私有已确认经验]
    HISTORY --> KNOWLEDGE
```

- Supervisor 选择角色与任务；Investigation 负责四种当前观测工具；Knowledge 使用两个知识工具；Diagnosis 形成假设与建议；Reviewer 逐项复核。
- 图负责节点切换，原有 Harness 负责全流程预算、调用计数、超时和审计；原有引用校验负责从已登记的来源字段展开引用。
- 默认最多 16 次模型请求、24 次工具尝试、180 秒，预留 3 次模型/20 秒收尾；真实执行必须显式选择 live 并提供预算。最多 4 次调度、6 个任务、1 次补查和1次结构修复。
- 工单、运行、事件和人工确认复用 PostgreSQL 存储。SSE 支持续读和回放；进程中断会标记失败，目前不恢复图执行。

## 已实现和边界

已支持创建/修订工单、六个开发案例、有界诊断、证据回放、人工确认解决、私有经验检索，以及同预算单/多 Agent 对照。六个工具都只读，执行前校验身份、角色、服务和时间范围。

自由工单可通过服务端ServiceRegistry绑定Prometheus/Loki观测Provider，查询模板、目标、租户和用户范围由管理员登记。开发案例继续使用合成观测。真实来源诊断必须显式live和预算，未登记来源返回信息缺口。

新增独立修复流程：Agent结构化候选或人工提案 → 绑定证据、工单版本和服务配置 → operator审批 → 单次执行 → 有界验证。Docker HTTP执行器已实现精确登记容器的restart_service，要求有效HEALTHCHECK、启动时间改变且健康恢复；不会开启Docker TCP或操作现有容器。扩缩容/回滚目前只有动作契约及平台扩展机制，默认执行器明确拒绝。真实重启尚未实测。诊断completed或修复verified都不会自动解决工单。

目录迁移后123项 IT 测试：118通过、5项 PostgreSQL 集成因未配置测试 DSN 跳过；再对新增归档与监控检查定向验证，当前共127项、122通过、5项跳过。六案例单/多共12次免费流程检查通过，结果不计入业务质量评分。本轮没有付费调用。历史真实复测的语义质量缺口仍保留，M5尚未整体验收。

通用接入与修复扩展做一次144项全量回归和后续定向验证，累计151项：145通过、6项数据库集成跳过。离线修复演示通过，模拟动作一次，重复请求不再次执行；未调用付费模型、数据库迁移或真实容器。配置、API和验证边界见[通用接入与受控修复](docs/通用接入与受控修复.md)，登记结构参考examples/services.example.json。

## 后端入口

2026-10-09进一步完善：真实修复同时验证容器健康与原症状指标，阶段事件逐步持久化，新增详情/事件续读、固定标签修复指标及Docker Desktop原生CLI执行器。独立临时PostgreSQL的7项集成和临时容器重启已实测通过；当前166项由一次全量加定向验证覆盖，未解决失败0。无付费调用，真实业务系统全链路尚未验收。详见[后端完善与持久化验收](docs/后端完善与持久化验收-20261009.md)。

```powershell
cd D:\code\it_incident_agent
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe main.py --fake --case case_003 --output output/incident
.\.venv\Scripts\python.exe scripts/demo_remediation.py
.\.venv\Scripts\python.exe local_run.py backend
```

`main.py` 默认运行 IT 协作图；单角色取证可显式选择 `--workflow investigation`。后端运行结果与事件使用 `/api/v1/runs/{run_id}` 和 `/api/v1/runs/{run_id}/events`。旧 `/api/v1/research/runs/...` 地址保留只读兼容，供现有页面与历史链接使用。

默认 IT 后端只需要 PostgreSQL；免费 CLI 不需要数据库、模型 Key、Milvus 或搜索 Key。新环境使用 `requirements.txt` 安装67个锁定的 IT 运行依赖；`requirements-incident.txt` 仅作为兼容入口。现有环境继续可用。详细启动步骤见 [Windows 本地启动](README_LOCAL.md)。

研究专用代码、原测试及覆盖/审计成果移入 `archive/research/`，只供历史追溯，不提供重新启用入口。52个归档文件保存内容哈希，保护已提交成果；历史验收报告不改写。现有 `config.json` 与本地凭据原样保留，运行只读取 IT 字段。

数据库现有表名及旧只读运行 URL 保留兼容，避免破坏数据和现有页面。日志改为 `logs/incident.log`，指标使用 `it_incident_*`。Compose只声明本项目的 PostgreSQL，旧 Milvus 容器/卷及其他项目未操作。

## 交付与后续

目录、职责与调用关系见 [项目结构与模块说明](docs/项目结构与模块说明.md)；本轮清理与验证见 [研究退役与目录整理记录](docs/研究退役与目录整理记录.md)。前一轮图迁移记录见 [后端收敛与 LangGraph 实施记录](docs/后端收敛与LangGraph实施记录.md)。下一步优先接入真实日志/指标来源并完成一条真实工单后端闭环，接口稳定后再收敛前端。

设计材料见 [完整实施与交接方案](docs/IT_Incident_Agent-完整实施与新对话交接方案.md)，实施状态以源码及交付记录为准。历史阶段记录保留：

- [M0–M2 实施](docs/M0-M2-实施记录.md)、[M2 验收](docs/M2-验收报告.md)。
- [M3 实施](docs/M3-实施记录.md)、[M3 验收](docs/M3-验收报告.md)。
- [M4 工单闭环](docs/M4-实施与验收记录.md)、[M5 对照准备](docs/M5-开发集与对照准备.md)。
- [最近一次真实复测与缺口](docs/M5-升级对象修复后真实复测记录.md)、[历史研究底座](README_RESEARCH.md)。

Git 提交信息使用简体中文；不提交密钥、令牌、运行原始数据或环境。本仓库基于已提交研究源码建立独立历史，保留已有成果。
