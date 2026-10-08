# Windows 本地启动

Web/API 已具备 M4 工单工程闭环，开发案例已增至六个。CLI 支持 Investigation、M3 串行协作及 M5 同预算单/多对照；真实语义与返工验收仍有缺口。以下安装步骤仅使用本项目自己的依赖、密钥和数据库。

## 首次准备

安装 Python 3.12、Node.js 20.19+ 或 22.12+（含 npm）、Docker Desktop，并启动 Docker 引擎。

在 PowerShell 中执行：

```powershell
cd D:\code\it_incident_agent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.local.example .env.local
.\.venv\Scripts\python.exe scripts/setup_demo_auth.py
```

编辑 `.env.local`，填写自己的 `DASHSCOPE_API_KEY` 与 `BOCHA_API_KEY`。模型默认为 `qwen-turbo`；联网研究需要搜索 Key。API 调用与文本向量入库会消耗相应额度。

认证脚本生成 `.auth.local.json`（服务端哈希注册表）与 `.demo-credentials.local.json`（本地演示令牌）。页面需要填入后者中的令牌；两个文件均已被 `.gitignore` 排除。

安装前端依赖：

```powershell
cd D:\code\it_incident_agent\front\agent_front
npm ci
```

## 启动

```powershell
cd D:\code\it_incident_agent
docker compose -f docker-compose.local.yml up -d --wait --wait-timeout 300
.\.venv\Scripts\python.exe local_run.py check
.\.venv\Scripts\python.exe local_run.py init
.\.venv\Scripts\python.exe local_run.py backend
```

保留后端终端，在第二个 PowerShell 启动前端：

```powershell
cd D:\code\it_incident_agent
.\.venv\Scripts\python.exe local_run.py frontend
```

打开 `http://127.0.0.1:5174`。后端接口文档：`http://127.0.0.1:8003/docs`。

默认打开“故障工单”。验证身份后，选择开发案例填充 → 创建工单 → 保持“免费流程演示”启动 → 查看角色/工具事件、引用与结果 → 刷新或重新连接回放原运行。只有本工单 owner 的 operator 身份可填写原因与实际处理结果并确认已解决；completed/partial 运行本身不会解决工单。资料研究可从左侧切换。

后端启动会在本项目配置的 PostgreSQL 中执行 M4 幂等迁移，保留原运行表；只启动一个后端进程，不使用多 worker。真实诊断需手动选择“真实模型（付费）”并指定模型/工具预算，点击启动才会调用本项目 Key。本轮 M4 验收没有付费调用。自由工单尚未绑定真实观测源，会返回需要补充信息；开发案例范围须与原合成观测一致。

接口和验收详情见 [M4 实施与验收记录](docs/M4-实施与验收记录.md)。

新数据库与知识库初始为空；知识库入库方式、权限与记忆管理参见 [研究底座说明](README_RESEARCH.md) 及 [记忆与权限隔离](docs/记忆与权限隔离说明.md)。历史文档中的本机已导入资料和测试记录描述的是研究底座，不是此副本的数据状态。

## 与研究项目分开运行

| 资源 | 本项目默认值 |
| --- | --- |
| Compose 项目名 | `it-incident-local` |
| PostgreSQL 主机端口 | `5434` |
| Milvus 主机端口 | `19531` |
| Milvus 健康接口主机端口 | `9092` |
| 后端 | `8003` |
| 前端 | `5174` |
| 默认记忆集合 | `it_incident_memory_local` |
| 默认知识集合 | `it_incident_knowledge_local` |

Compose 项目名使 PostgreSQL 和 Milvus 使用独立的数据卷；容器内部端口保持原值。各项目使用各自的 `.env.local` 和身份文件。若端口被其他应用占用，需要同时修改 Compose、环境文件与前端配置中的对应地址。

## 停止

前后端分别按 Ctrl+C。只停止本项目的 Docker 服务，保留数据：

```powershell
docker compose -f D:\code\it_incident_agent\docker-compose.local.yml stop
```

## 免费工程回归

```powershell
cd D:\code\it_incident_agent
$env:PYTHONPATH='app'
$env:DASHSCOPE_API_KEY='test-only'
.\.venv\Scripts\python.exe -m unittest tests.test_controls tests.test_identity_memory.IdentityTests tests.test_evaluation
.\.venv\Scripts\python.exe -m evals.run --check-fixtures
```

测试通过说明研究底座的工程约束通过检查，不代表 IT 工单功能已完成。

## Investigation CLI（M0–M2）

无需 Docker、Milvus、搜索 Key 或模型 Key 即可运行免费控制探针、工具/循环回归与资料检查：

```powershell
cd D:\code\it_incident_agent
$env:PYTHONPATH='app'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe scripts/probe_incident_tools.py
.\.venv\Scripts\python.exe -m unittest tests.test_incident_tools tests.test_incident_investigation tests.test_incident_acceptance -v
.\.venv\Scripts\python.exe -m evals.incident.check_fixtures
.\.venv\Scripts\python.exe scripts/investigate_incident.py --fake --case case_001 --output output/incident
.\.venv\Scripts\python.exe scripts/investigate_incident.py --fake --case case_002 --output output/incident
.\.venv\Scripts\python.exe scripts/investigate_incident.py --fake --case case_003 --output output/incident
```

`--fake` 是明确编排的控制演示，不是自主选择评测。JSON 保存真实执行计数、证据快照与顺序事件；
Markdown 是确定性取证报告。结果为 completed 只表示取证输出完成；假设尚未独立复核、工单未解决。

当前模型输出使用 `reference_selection_v2`：工具证据附带 `reference_options`，模型为每条发现选择
`reference_id`；程序从当前证据登记表填入原始路径、值、单位、时间、版本和引文，最终 JSON
继续保存完整引用。未知或未取证的引用编号被拒绝；旧完整引用格式仅保留严格兼容，记录为
`literal_refs_v1`。输出字段有效不代表自然语言结论已经得到独立复核。

真实调用只由显式 `--live` 启动。先在本项目 `.env.local` 填写自己的 `DASHSCOPE_API_KEY`、
`MODEL=qwen-turbo` 与 `INCIDENT_BEARER_TOKEN`（从本项目生成的本地演示身份中选择一个）；
脚本沿用 backend.auth 解析服务端 Principal，不接收用户自报租户/身份。
Investigation 入口无论是否加 `--probe`，都限制为 **一次运行最多 3 次模型请求、6 次工具实际尝试**，包括结构修复/工具重试，
不自动批量运行。没有 Key/有效身份会在请求前停止。需费用时由用户显式执行：

```powershell
.\.venv\Scripts\python.exe scripts/investigate_incident.py --live --probe --case case_001 --output output/incident-live
```

该 CLI、M4 页面/API 的显式 live 启动及下文 M5 的显式 live 对照均可能付费；普通测试不会请求模型。未测得完整 token 或费用时保留 unknown，
不估造数字。大批评测、扩大 live 上限或运行另外两个案例需后续明确执行清单。

内部单任务最多 4 个模型步骤（含结束输出和修复），共享上限 16 模型/24 工具/180 秒，
保留 3 次模型/20 秒给收尾。单角色 Investigation 不使用预留来伪装复核；M3 协作使用同一预算执行调度、诊断与复核。
请求连接/读超时为 30 秒；不声称 180 秒严格 SLA，晚到结果不会变成有效取证输出。
当前 Provider 只有合成目录，不为自由工单制造观测；知识工具目前是 fixture 检索，尚未验证真实 Milvus 召回。

M0–M2 实际检查结果与未完成项见 [实施记录](docs/M0-M2-实施记录.md)。
M2 三案例真实验收见 [验收报告](docs/M2-验收报告.md)。离线检查入口为
`python -m evals.incident.acceptance --report <run.json>`；逐案显式传入三个报告，
可加 `--output <summary.json>` 保存安全汇总。它不会请求模型；gold 仅供离线评估模块读取。
返回 0 只说明机械检查通过；完整验收仍需逐项核查语义支持与缺口。
报告的工具执行记录展示校验后的枚举过滤条件和空/错/截断；仅引用元数据的发现被拒绝。
指标按最新采样优先返回，数量上限与 12000 字符消息上限均保持不变。
发生截断时提示聚焦查询，并保守标记 partial/limited_by_truncation；旧运行文件不改写。
日志 level 是精确过滤，ERROR 会排除 WARN/INFO；核心观测查询 empty 且仍有工具步骤时，
程序反馈校验后的过滤条件，让模型选择放宽可选条件或其他检查，不自动扩大查询。

## 串行协作 CLI（M3）

沿用同一脚本，通过 `--workflow collaboration` 选择五角色协作，不需要新增依赖或启动数据库：

```powershell
cd D:\code\it_incident_agent
$env:PYTHONPATH='app'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m unittest tests.test_incident_collaboration -v
.\.venv\Scripts\python.exe scripts/investigate_incident.py --fake --workflow collaboration --case case_003 --output output/incident/m3-control
```

Supervisor 根据实际输入决定派发 Investigation/Knowledge 或交给 Diagnosis；Reviewer 逐项复核。
有区分价值的缺口可以通过 Supervisor 派发一次补查，再由 Diagnosis 修订、Reviewer 二次复核。
简单案例可以跳过 Knowledge；不要求每次都调用全部角色。

所有角色共用一个 RunContext：默认全局 16 模型/24 工具/180 秒；最多 6 子任务、4 次调度、
1 轮返工、2 次复核和全运行 1 次结构修复。排查步骤仍最多 4 次。
取证保留 3 次模型/20 秒供收尾；最终调度、Diagnosis 和 Reviewer 可使用预留。
派发前检查最低调用余量；实际多步取证也受共享预算限制，余量不足便保留缺口。
Supervisor 会收到当前可派发任务数；小预算优先保留当前观测任务，延后超额任务。
子任务 step_limit 按实际剩余模型额度缩小。取证预算耗尽且已有观测时转入 Diagnosis/Reviewer，
不会仅因工具额度耗尽跳过复核；收尾失败或证据不足仍保留 partial。
仅历史支持的原因即便被 Reviewer 接受，程序也降级为 unresolved。

结果保存为 `<run_id>.json/.md`，包含证据、任务、诊断版本、复核、补查请求、协商事件与总计数。
`supported` 表示 Reviewer 对当前证据的判断；人工确认和工单 resolved 已由 M4 的 operator 确认接口实现，运行完成本身不会解决工单。
免费路径的所有角色均为测试替身，不能据此认定真实模型验收通过。

M3 的付费入口必须同时显式指定模型和工具预算；缺少预算、Key 或有效身份会在请求前停止。
模型上限允许 6–16、工具上限允许 1–24；不自动跑三个案例。以下命令仅作为后续显式执行清单，
首次真实验证及修复后的复测均按此简单案例预算，以及 16 模型/8 工具的冲突案例预算执行：

```powershell
.\.venv\Scripts\python.exe scripts/investigate_incident.py --live --workflow collaboration --case case_001 --model-budget 6 --tool-budget 4 --output output/incident-live-m3
```

预算是调用次数上限，不保证成功或完整 token/费用可知。M3 不兼用 M2 的 `--probe`。
文件改动、免费验证和未完成项见 [M3 实施记录](docs/M3-实施记录.md)。
首次真实验证结果与修复见 [M3 验收报告](docs/M3-验收报告.md)。

## 同预算单/多对照（M5 准备）

复用现有工具循环和协作流程，六案例每案各运行一次 SingleAgent 和多 Agent。默认只打印计划；免费执行不加载 Key 或 Bearer 身份：

```powershell
$env:PYTHONPATH='app'
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe scripts/compare_incident.py --plan --cases case_003 --model-budget 16 --tool-budget 8
.\.venv\Scripts\python.exe scripts/compare_incident.py --fake
```

可用 `--cases case_004 case_005` 限定免费案例。单/多每运行使用相同预算，默认 16 模型/8 工具/180 秒，预留 3 模型/20 秒；SingleAgent 无独立复核，假设保持 tentative。脚本结果排除在业务质量评分之外。

输出保存到 `output/incident-comparison/<新目录ID>/`，包含 `bundle.json`、原始运行 JSON、`comparison.json` 和 `review-template.json`，原运行不覆盖。离线入口：

```powershell
.\.venv\Scripts\python.exe -m evals.incident.comparison --bundle output/incident-comparison/<目录ID>/bundle.json --output output/incident-comparison/<目录ID>/summary.json
```

真实对照必须同时指定 `--live --execute-live --cases ... --model-budget ... --tool-budget ...`，预算范围与 M3 相同，且使用本项目自己的 Key/注册身份；缺少条件在请求前停止。Key 写在本项目 `.env.local`；若尚未设置 INCIDENT_BEARER_TOKEN，可将已有本地演示令牌放入当前终端环境，以下只赋值、不显示令牌，也不重新生成身份：

```powershell
$incidentCredentials = Get-Content -Raw .demo-credentials.local.json | ConvertFrom-Json
$env:INCIDENT_BEARER_TOKEN = $incidentCredentials.principals[0].token
```

case_003 已按每运行 16 模型/8 工具完成一次真实单/多对照；机械检查通过，业务语义仍有缺口，没有自动扩展或重跑。后续付费执行先明确运行清单与预算。完整流程、控制结果见 [M5 开发集与对照准备](docs/M5-开发集与对照准备.md)，最新用量和验收缺口见 [M5 真实对照验收记录](docs/M5-真实对照验收记录.md)。

随后已修复事实来源错配、证据复用和连接池 CPU 缺口；最新免费回归 98 项及六案例 12 次对照通过，未新增付费请求。事实文本现在按引用的实际字段生成；假设和建议仍需要复核。详细改动、执行命令和下一次显式真实复测清单见 [M5 验收问题修复记录](docs/M5-验收问题修复记录.md)。
