# Windows 本地启动

当前后端只运行 IT 工单链路。源码结构和能力以[项目入口](README.md)为准；研究源码和旧演示资料集中在 archive/research，只供追溯。前端已提供轻量 IT 调试页面。历史试点结果见本地[真实联调验收与稳定版收尾](docs/真实联调验收与稳定版收尾-20261010.md)。

## 免费求职演示

已有本项目 Python 环境时直接运行：

```powershell
cd D:\code\it_incident_agent
.\.venv\Scripts\python.exe scripts/showcase_incident.py
```

会生成 `output/showcase/<批次>/index.html`、`summary.json` 和报告。三条演示使用真实图、脚本模型与合成观测，覆盖正常诊断、一次实际补查、预算不足退出；同时运行同预算模拟对照及现有约束测试。没有模型 Key、数据库或 Docker 也能运行，不产生付费调用或真实修复。新机器先按下节创建虚拟环境并安装依赖，演示无需设置身份或启动数据库。

架构取舍、历史真实对照、五分钟讲解和人工评分入口见 [公开展示材料](examples/showcase/README.md)。历史报告和人工评分模板仅本机可用，公开仓库只提供安全指标摘要；助手分析不当作人工确认。

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

Python 构建配置 `pyproject.toml` 复用同一锁定依赖，只打包 `app/` 下的 IT 模块及数据库迁移。`.env.example` 和 `.env.local.example` 均为当前 IT 模板，前端地址覆盖使用 `INCIDENT_API_TARGET`；未设置时按 `PORT` 自动连接。已有真实环境、身份与登记文件不会被模板覆盖。

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

## 服务登记模板与免费预检

继续使用根目录 `services.local.json`，不需要服务管理页面。登记按“生成独立模板 → 调整固定查询 → 离线预检 → 显式只读试连 → 合入本地登记并重启后端”进行。已有知识库登记可以直接预检，不需要重新生成或替换。

```powershell
cd D:\code\it_incident_agent
$env:PYTHONIOENCODING='utf-8'
# 预检当前环境指定的登记文件；未指定环境变量时读取 services.local.json。
.\.venv\Scripts\python.exe scripts/register_service.py check --json

# 为新服务生成独立的只读模板；身份值应换成 /api/v1/auth/me 返回的真实 tenant_id/user_id。
.\.venv\Scripts\python.exe scripts/register_service.py template --profile generic --service example-api --tenant example_tenant --user example_operator --environment staging --version v1 --owner-team alice --output output/service-registration/new-service.template.json
.\.venv\Scripts\python.exe scripts/register_service.py check --file output/service-registration/new-service.template.json --json
```

`--profile generic` 复用通用指标和日志例子，替换示例服务、环境标签；`--profile enterprise` 复用现有知识库 Worker 的固定指标和日志映射，其 job/stack/日志环境选择器仍需核对实际部署。可用 `--prometheus-url`、`--loki-url` 指定基础地址；省略则保留示例本机地址。`--user` 可以重复传入。负责人未明确就省略 `--owner-team`，脚本会保留信息缺口。

新增三个知识库组件模板：`--profile enterprise-api`、`--profile postgres`、`--profile redis`，复用 `examples/services.enterprise-stack.example.json`。API 模板登记实际已实现的 HTTP 指标；PostgreSQL/Redis 模板只登记运维日志，独立 exporter 尚未接入，缺少直接指标会明确提示。生成时手册同步服务标识与指定版本；固定 Docker service、stack、日志环境和 Prometheus job 选择器仍须核对部署。

模板始终生成 `repairs: {}`，不会复制示例中的重启权限，也不会自动合并或覆盖 `services.local.json`；目标文件已存在就退出。默认输出到忽略提交的 `output/service-registration/services.template.json`。新增服务时先核对模板 `services[0]`，再手工加入现有文件的 `services` 数组，保留其他登记。修复目标仍需另行登记固定 context/容器ID和新鲜症状恢复规则。

预检和后端启动共用规则：字段类型与必填项、租户/服务/环境重复、访问用户、基础地址和凭据环境变量名称、固定查询来源、日志映射、负责人/手册字段、修复动作和预算、症状指标引用与新鲜度查询。错误只显示字段位置和安全原因；例如 `$.services[0].observations.metrics[1].unit` 指第一个服务下第二个指标缺少单位。动态字典键按文件顺序编号，避免错误输出泄露误填的凭据。

预检退出码：0表示结构有效（可能有提示），1表示配置或文件无效，2表示命令参数无效。空观测渠道、负责人/手册缺失、模板容器占位值以提示保留。预检不验证查询语义、地址可达性、凭据有效性或整体健康，不连接数据库、模型、Docker或外部观测服务。认证头仅登记 `authorization_env` 环境变量名，凭据留在本项目环境中，不把明文写入模板或命令参数。

需要核对实际连接时，显式复用已有免费工具：

```powershell
# 文件中的租户/用户必须匹配本项目本地 operator 身份；多个 operator 时指定 --user 和 --tenant。
.\.venv\Scripts\python.exe scripts/probe_registered_service.py --services-file output/service-registration/new-service.template.json --service example-api --environment staging --minutes 60
```

试连会实际读取已登记观测，但不调用模型或执行修复。模板占位地址、固定查询和身份未调整时，不应把空结果当成成功。修改实际登记后，重启 HTTP 后端加载；不会自动重启业务 Worker。实现与本次验证见 [服务登记模板与免费预检](docs/服务登记模板与免费预检-20261010.md)。

## 接入知识库服务的免费只读联调

本地 `services.local.json` 通过 `INCIDENT_SERVICES_FILE` 加载。知识库事件日志支持登记 `log_mapping`，公开骨架见 `examples/services.enterprise.example.json`。指标、日志地址及选择器由管理员固定，凭据不传给模型。

```powershell
.\.venv\Scripts\python.exe scripts/probe_registered_service.py --service enterprise-indexing --minutes 1440
```

该命令只查询已登记观测并校验证据，不调用模型、不创建工单或执行修复。结果写入忽略提交的 `output/registered-service-probe.json`；退出码0表示检查通过，2表示有空结果/截断等缺口，1表示无法开始。指标仅检查最近最多5分钟的小样本，日志使用指定窗口。详细操作与边界见 [知识库服务只读接入与联调](docs/知识库服务只读接入与联调-20261009.md)。配置或代码修改后，HTTP 后端需要重新启动。

## 新增 API、PostgreSQL 与 Redis 登记

本机已在 `services.local.json` 追加 `enterprise-api`、`enterprise-postgres`、`enterprise-redis`，沿用索引 Worker 的租户、允许访问用户、环境、版本和本项目已登记观测地址；用户确认三个服务负责人均为 alice。API 的依赖登记为上述数据库和缓存，提供依赖负责人查询；跨服务指标和日志仍需各自工单范围，不自动扩大当前工单权限。原 Worker 登记和修复目标保持原样，新三项 `repairs: {}`。

| 服务 | 当前可查内容 | 限制 |
| --- | --- | --- |
| enterprise-api | api_up、request_rate、request_error_rate、request_latency_p95；QA 运维事件的 resource/dependency/configuration 日志；负责人、手册 | api_up 仅为抓取状态；错误速率不是错误百分比；日志未覆盖所有上传/接口异常堆栈 |
| enterprise-postgres | postgres 容器启动、停止、恢复、连接、认证、写入和超时等固定状态日志；负责人、手册 | 未登记独立指标，没有连接数、CPU、磁盘或慢查询观测 |
| enterprise-redis | redis 容器启动、停止、持久化及常见资源/认证错误状态日志；负责人、手册 | 未登记独立指标，没有内存、连接数、命中率或阻塞观测 |

API 使用事件白名单和既有日志映射，仅保留事件名与允许的运维字段。PostgreSQL/Redis 在 Loki 查询中提取固定状态短语，不返回SQL、缓存键值、用户或业务内容。历史就绪日志与空日志均不能证明当前健康。源码实现依据为现有知识库监控代码、Prometheus job 和 Alloy 标签；实际查询语义及采样覆盖留待统一试连确认。

当前工单后端已在空闲时重载，8002 和 5174 前端代理的目录读取均能看到四个服务。已打开的页面刷新后重新认证，即可更新服务列表。2026-10-10 已显式执行一次下列免费统一联调，结果见下文；命令保留供后续按需使用：

```powershell
cd D:\code\it_incident_agent
$env:PYTHONIOENCODING='utf-8'
$onboardingServices = @('enterprise-indexing', 'enterprise-api', 'enterprise-postgres', 'enterprise-redis')
$onboardingResults = @()
foreach ($onboardingService in $onboardingServices) {
    .\.venv\Scripts\python.exe scripts/probe_registered_service.py --service $onboardingService --environment staging --minutes 1440 --output "output/service-onboarding/probes/$onboardingService.json"
    $onboardingResults += [pscustomobject]@{Service=$onboardingService; ExitCode=$LASTEXITCODE}
}
$onboardingResults | Format-Table
```

该批次只读取已登记观测，模型调用为0；多个 operator 时为命令补上 `--user` 和 `--tenant`。PostgreSQL/Redis 缺少指标时，现有探针会报告 `partial / metrics: NO_REGISTERED_METRICS`，这是明确的覆盖缺口。日志无匹配数据也保留缺口。结果不等于整体健康或故障诊断通过。真实诊断与隔离异常演练后续另行显式启动，继续遵循既有预算与付费授权。

登记实施阶段36项定向免费测试通过，实际登记离线预检0错误、2项指标缺口提示；当时未执行真实观测查询或业务修复，未修改知识库项目。详细范围和统一测试计划见 [三个组件接入与后续统一测试](docs/知识库API与PostgreSQL及Redis登记-20261010.md)。

后续统一联调已完成：20次工具尝试、0模型调用，17项检查返回有效结果、3项API QA日志为空，24条实质引用全部校验通过，provider错误0。四份报告均partial：Worker日志因证据容量截断，API固定QA筛选无匹配，PostgreSQL/Redis未登记指标。两次额外只读统计确认API同窗口有5870条日志，固定事件筛选0条；没有故障注入或修复操作。详细结果见 [四服务统一只读联调结果](docs/四服务统一只读联调结果-20261010.md)。

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

后端完善与隔离实测见[后端完善与持久化验收](docs/后端完善与持久化验收-20261009.md)。Docker Desktop可用时，可显式运行scripts/verify_postgres.py --start，使用独立临时测试库5435并自动清理；不能用业务库。真实修复必须登记症状规则及对应指标freshness_query。支持docker_cli执行器，登记固定context和容器ID，无需开放Docker TCP。数据库7项和临时容器重启已实测；2026-10-10知识库试点用户确认修复成功及文档成功入库。

配置结构见examples/services.example.json，具体步骤、权限、API和未完成项见[通用接入与受控修复](docs/通用接入与受控修复.md)。本项目services.local.json已忽略；配置本项目身份、服务版本和固定PromQL/LogQL，然后设置INCIDENT_SERVICES_FILE并重启后端。示例全部为占位值，默认不会连接真实系统。

免费修复演示：

```powershell
.\.venv\Scripts\python.exe scripts/demo_remediation.py
```

演示只模拟动作。真实修复需登记目标、真实诊断通过复核、operator审批摘要，再显式调用execute。当前Docker HTTP/CLI执行器仅支持restart_service，要求精确容器ID、有效HEALTHCHECK及登记症状验证；不会自动开启Docker API。扩缩容、回滚执行器尚未实现。知识库本地登记现已包含 Worker 重启目标和新鲜活跃指标恢复规则；配置保存在忽略提交的services.local.json，不随仓库分发。本次提交收尾不重复执行外部修复或调用模型。

前端修复操作顺序：

1. 选择当前版本的真实故障工单；最新诊断须为live、completed/passed，状态核查不能作为修复来源。
2. 在“修复方案与执行”选择登记目标，填写处理依据，勾选1–8条支持动作的本次观测。恢复Worker通常参考workers_active=0、queue_depth>0及本次采集到的停止日志；不能仅凭这些数字确定深层原因或证明维护条件已满足。
3. 创建待审批方案，核对诊断建议中的人工前提以及方案影响、失败处理和恢复验证标准。创建不执行。
4. 本工单所属operator单独确认并批准方案，再单独确认执行。批准不执行；网络异常后显式重试沿用原请求编号。
5. 查看执行阶段及新鲜恢复观测，确认实际业务结果，再填写人工确认表单关闭工单。Worker活跃恢复不等于索引或检索全部成功。

诊断与复核现在共享登记能力及症状恢复标准；有条件建议通过复核不等于审批，未知触发原因仍保留信息缺口。旧partial报告不会自动变为通过。该批定向免费回归71项通过，前端9个模拟浏览器场景及类型/构建检查通过；真实业务成功以用户反馈为依据，未另行核验正式闭单或检索结果。

修复提案现已保存并展示三项审批依据：适用条件与执行前确认、操作风险、预期结果与验证方法。选择诊断建议后自动填入；内容或证据有修改时按人工提案保存。后端从已复核结果读取原始建议，保存来源run_id/action_index，三个字段与来源一起纳入审批摘要。新页面的人工提案须填写完整；API的RepairProposal新增可选approval_details对象（condition/risk/expected_result，非空且每项最多1000字符），兼容现有客户端。from-recommendation接口只接收建议编号等原有参数，不能由调用方替换建议原文；缺失审批依据返回REPAIR_RECOMMENDATION_INCOMPLETE。

已创建方案从数据库读取原内容，不随报告展示变化；旧方案不回填，页面明确显示缺失提示，已有摘要和幂等请求保持兼容。本次完善通过37项后端回归、7项隔离数据库检查、11个模拟浏览器场景及前端类型/构建检查。无需为确认界面变化重新付费诊断或创建真实修复。

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

前端代理从根目录 .env.local/.env 读取后端 PORT，当前本机为8002，缺省为8003；也可用 INCIDENT_API_TARGET 指定地址。修改代理配置后重启前端。工单页面已提供已登记重启目标的修复提案、审批、显式执行和进度；普通用户不能审批或执行，状态核查不能创建真实修复。新增修复能力读取接口需重启后端加载。界面、构建与限制见[前端说明](front/agent_front/README.md)。

前后端按 Ctrl+C 停止；数据库仅停止本项目服务并保留数据：

```powershell
docker compose -f D:\code\it_incident_agent\docker-compose.local.yml stop postgres
```

详细结构见 [模块说明](docs/项目结构与模块说明.md)，本轮结果和限制见 [清理记录](docs/研究退役与目录整理记录.md)。历史实施、验收和研究文档保留供追溯，其旧启动命令不作为当前入口。
