# Windows 本地启动

当前后端只运行 IT 工单链路。研究源码在 archive/research 中保留历史快照，不能通过旧开关重新启用。前端当前保留原页面，后续再统一整理。

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

## 日志和指标

JSON日志写入控制台和 logs/incident.log，单文件约2 MB，保留3个轮转备份；可通过 INCIDENT_LOG_DIR 修改位置。已有 research.log 不删除。

GET /metrics 返回 Prometheus 格式的 it_incident_* 指标，需要 operator 身份。指标为进程内统计，重启清零；事件和运行记录保存到 PostgreSQL。尚未部署 Prometheus/Grafana 或告警。

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

配置结构见examples/services.example.json，具体步骤、权限、API和未完成项见[通用接入与受控修复](docs/通用接入与受控修复.md)。本项目services.local.json已忽略；配置本项目身份、服务版本和固定PromQL/LogQL，然后设置INCIDENT_SERVICES_FILE并重启后端。示例全部为占位值，默认不会连接真实系统。

免费修复演示：

```powershell
.\.venv\Scripts\python.exe scripts/demo_remediation.py
```

演示只模拟动作。真实修复需登记目标、真实诊断通过复核、operator审批摘要，再显式调用execute。当前Docker HTTP执行器仅支持restart_service，要求精确容器ID及有效HEALTHCHECK；不会自动开启Docker API或调用现有容器。扩缩容、回滚执行器尚未实现。真实数据库迁移、监控接入、模型候选和容器重启本轮均未实测。

## 可选前端和停止

前端本轮不改动。要使用现有页面，可在 front/agent_front 安装 Node依赖，并执行 local_run.py frontend，端口5174。旧研究导航尚在，但研究创建/记忆 API 已退出后端；旧只读运行地址保留以继续查看工单。

前后端按 Ctrl+C 停止；数据库仅停止本项目服务并保留数据：

```powershell
docker compose -f D:\code\it_incident_agent\docker-compose.local.yml stop postgres
```

详细结构见 [模块说明](docs/项目结构与模块说明.md)，本轮结果和限制见 [清理记录](docs/研究退役与目录整理记录.md)。历史实施、验收和研究文档保留供追溯，其旧启动命令不作为当前入口。
