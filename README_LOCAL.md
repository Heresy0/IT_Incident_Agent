# Windows 本地启动

当前后端只运行 IT 工单链路。研究源码在 archive/research 中保留历史快照，不能通过旧开关重新启用。前端已提供轻量 IT 调试页面。最新试点结果、免费检查和边界见[真实联调验收与稳定版收尾](docs/真实联调验收与稳定版收尾-20261010.md)。

## 首次准备

使用本项目自己的 Python 3.12 环境和 PostgreSQL，不复制其他项目的密钥、令牌或数据。已有环境和配置无需重建。

```powershell
cd D:\code\it_incident_agent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env.local)) { Copy-Item .env.local.example .env.local }
.\.venv\Scripts\python.exe scripts/setup_demo_auth.py
```

requirements.txt 为67个锁定的 IT 运行包，不包含 Milvus、Redis 检查点或研究搜索依赖。requirements-incident.txt 是兼容入口。本轮使用现有独立环境完成验证，未强制卸载额外包或重新创建环境。

免费脚本模式无需模型 Key。显式执行真实模型时，在本项目 .env.local 配置 DASHSCOPE_API_KEY、MODEL=qwen-turbo 和 INCIDENT_BEARER_TOKEN。身份初始化脚本不会覆盖现有 .auth.local.json 和 .demo-credentials.local.json；不要把它们提交到 Git。

config.json 和真实 .env.local 原样保留；运行配置仅使用模型、Key和 PostgreSQL DSN。旧研究开关、记忆字段和向量配置不会恢复研究功能。

## 免费 CLI

无需 Docker、数据库或 Key：

```powershell
cd D:\code\it_incident_agent
$env:PYTHONPATH='app'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe main.py --fake --case case_003 --output output/incident
.\.venv\Scripts\python.exe scripts/probe_incident_tools.py
.\.venv\Scripts\python.exe -m evals.incident.check_fixtures
```

main.py 默认运行五角色 LangGraph 协作；只排查取证可加 --workflow investigation。--fake 是脚本控制演示，不是自主决策成绩，completed 也不会把工单变为已解决。原始输出写入 output，已被 Git 忽略。

## 持久化后端

安装并启动 Docker Desktop，只启动本项目自己的 PostgreSQL：

```powershell
cd D:\code\it_incident_agent
docker compose -f docker-compose.local.yml up -d --wait --wait-timeout 300 postgres
.\.venv\Scripts\python.exe local_run.py check
.\.venv\Scripts\python.exe local_run.py init
.\.venv\Scripts\python.exe local_run.py backend
```

接口文档：http://127.0.0.1:8003/docs。健康检查：/health。运行结果：/api/v1/runs/{run_id}；SSE事件：/api/v1/runs/{run_id}/events，支持 after_seq / Last-Event-ID。

业务接口需要 Authorization: Bearer。服务端解析身份，工单与运行按租户/用户隔离；只有本工单 owner 的 operator 能人工确认解决。POST diagnoses 只启动一次，刷新和 SSE 重连不再次发起模型调用。

默认端口 PostgreSQL 5434、后端8003；Compose项目 it-incident-local，数据卷独立。现有数据库名和账号保留以兼容既有数据，不在目录清理时迁移；旧 Milvus 容器/卷没有删除。

后端初始化执行运行表、工单迁移和新增002修复表迁移，不需要 Milvus、搜索 Key、个人记忆或研究 Agent。自由工单可通过INCIDENT_SERVICES_FILE绑定真实观测源；未绑定时返回信息缺口，不制造案例证据。后台仅支持单进程，诊断中断标记失败，修复中断转人工处理，不恢复外部写操作。

## 接入知识库服务的免费只读联调

本地 `services.local.json` 通过 `INCIDENT_SERVICES_FILE` 加载。知识库事件日志支持登记 `log_mapping`，公开骨架见 `examples/services.enterprise.example.json`。指标、日志地址及选择器由管理员固定，凭据不传给模型。

```powershell
.\.venv\Scripts\python.exe scripts/probe_registered_service.py --service enterprise-indexing --minutes 1440
```

该命令只查询已登记观测并校验证据，不调用模型、不创建工单或执行修复。结果写入忽略提交的 `output/registered-service-probe.json`；退出码0表示检查通过，2表示有空结果/截断等缺口，1表示无法开始。指标仅检查最近最多5分钟的小样本，日志使用指定窗口。详细操作与边界见 [知识库服务只读接入与联调](docs/知识库服务只读接入与联调-20261009.md)。配置或代码修改后，HTTP 后端需要重新启动。

## 日志和指标

真实诊断出现 `REVIEW_TARGET_COVERAGE` 或未登记日志重复查询时，修复、指标采样行为及重新加载步骤见 [真实诊断复核与观测修复](docs/真实诊断复核与观测修复-20261009.md)。长窗口指标采用按证据容量调整的采样，`granularity` 为实际间隔；未截断不表示完整连续历史。

JSON日志写入控制台和 logs/incident.log，单文件约2 MB，保留3个轮转备份；可通过 INCIDENT_LOG_DIR 修改位置。已有 research.log 不删除。

GET /metrics 返回 Prometheus 格式的 it_incident_* 指标，需要 operator 身份。指标为进程内统计，重启清零；事件和运行记录保存到 PostgreSQL。本项目自身尚未部署 Prometheus/Grafana 或告警；知识库试点使用外部已部署的 Prometheus/Loki 作为只读观测源。

## 免费回归

```powershell
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py'
.\.venv\Scripts\python.exe scripts/compare_incident.py --fake --output output/incident-comparison
```

数据库集成只接受明确隔离的 *_test 库；配置 INCIDENT_TEST_POSTGRES_DSN 后，运行 tests.storage.test_incident_postgres。不能把日常使用的业务库作为测试库。测试只清理本次随机租户的合成记录。

目录、导入、脚本和 CI 已按新模块同步；研究专用测试退出当前 CI，在历史归档中保留。过往研究分数和免费控制成绩都不代表 IT 业务准确率。

## 显式真实模型调用

下面的命令仅供用户明确决定执行时使用，本轮没有执行：

```powershell
# 单角色探针始终保持最多3模型请求/6工具尝试。
.\.venv\Scripts\python.exe scripts/investigate_incident.py --workflow investigation --live --probe --case case_001 --output output/incident-live
# 协作需显式提供预算，不自动批量复测。
.\.venv\Scripts\python.exe main.py --live --case case_003 --model-budget 16 --tool-budget 8 --output output/incident-live
```

Key、身份或预算不满足时在请求前停止。未知 Token/费用继续记为 unknown，不估造价格。六个Agent工具仍只读；修复使用单独的审批与执行API，写操作不进入自由工具循环。

## 通用接入和修复

最新完善与实测见[后端完善与持久化验收](docs/后端完善与持久化验收-20261009.md)。Docker Desktop可用时，可显式运行scripts/verify_postgres.py --start，使用独立临时测试库5435并自动清理；不能用业务库。真实修复现在必须登记症状规则及对应指标freshness_query。支持docker_cli执行器，登记固定context和容器ID，无需开放Docker TCP。数据库7项和临时容器重启已实测，真实业务故障闭环仍待接入。

配置结构见examples/services.example.json，具体步骤、权限、API和未完成项见[通用接入与受控修复](docs/通用接入与受控修复.md)。本项目services.local.json已忽略；配置本项目身份、服务版本和固定PromQL/LogQL，然后设置INCIDENT_SERVICES_FILE并重启后端。示例全部为占位值，默认不会连接真实系统。

免费修复演示：

```powershell
.\.venv\Scripts\python.exe scripts/demo_remediation.py
```

演示只模拟动作。真实修复需登记目标、真实诊断通过复核、operator审批摘要，再显式调用execute。当前Docker HTTP/CLI执行器仅支持restart_service，要求精确容器ID、有效HEALTHCHECK及登记症状验证；不会自动开启Docker API。扩缩容、回滚执行器尚未实现。此前已验证隔离数据库迁移和临时容器重启；知识库试点只登记只读观测，恢复 Worker 与索引重试由用户执行，本次收尾未重复执行外部修复。

## 可选前端和停止

工单现在可以显式选择 `purpose=status_check`（观测核查）或 `purpose=diagnosis`（故障原因与建议）；默认兼容旧记录为 diagnosis。状态核查只复核所列观测并说明覆盖范围，不确认整体健康，不生成修复建议。修改目的后先保存修订，再显式启动。实现、验证及使用步骤见[状态核查与登记补查约束](docs/状态核查与登记补查约束-20261009.md)。

故障诊断须包含经过复核的当前观测、原因假设和处理建议。仅有观测、缺少原因或建议时返回 `partial / needs_information`、`DIAGNOSIS_INCOMPLETE`，保留事实和具体缺口，不增加自动重试。修复说明和 Worker 演练示例见[故障诊断完整性修复](docs/故障诊断完整性修复-20261009.md)。

Diagnosis 的引用 Schema 列出本次可见的真实 `REF_` 编号，各引用列表要求不重复，程序继续独立校验。未知引用和重复引用分别报告具体字段位置；使用既有一次纠正额度，不能自动增加预算。实现及免费验证见[诊断引用约束与纠正修复](docs/诊断引用约束与纠正修复-20261009.md)。

当前提供轻量 IT 调试页面，复用 Vue 工单与事件回放，入口已去掉研究和记忆导航。已有前端依赖可直接启动：

```powershell
cd D:\code\it_incident_agent
.\.venv\Scripts\python.exe local_run.py frontend
```

打开 http://127.0.0.1:5174/，输入本项目访问令牌，选择登记服务、填写问题和时间窗口、保存工单，再设置预算并勾选本次付费授权。真实诊断只在点击启动后执行；刷新、读取结果和事件回放不会重新启动。令牌仅保存在页面内存，刷新后重新认证即可读取同一身份的工单。免费开发案例不调用真实模型。

用户反馈服务故障无需先提供索引任务 ID，可描述症状、发生时间与影响范围。当前六个工具查询登记观测和知识，不直接查询索引任务；具体任务是否成功仍由业务页面或人工确认。

前端代理从根目录 .env.local/.env 读取后端 PORT，当前本机为8002，缺省为8003；也可用 INCIDENT_API_TARGET 指定地址。修改代理配置后重启前端。尚未提供修复审批和执行页面。界面、构建与限制见[前端说明](front/agent_front/README.md)。

前后端按 Ctrl+C 停止；数据库仅停止本项目服务并保留数据：

```powershell
docker compose -f D:\code\it_incident_agent\docker-compose.local.yml stop postgres
```

详细结构见 [模块说明](docs/项目结构与模块说明.md)，本轮结果和限制见 [清理记录](docs/研究退役与目录整理记录.md)。历史实施、验收和研究文档保留供追溯，其旧启动命令不作为当前入口。
