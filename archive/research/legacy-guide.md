> 历史研究底座说明：该链路已退出当前项目运行与CI，源码在 archive/research 原样归档。本文保留供追溯，旧启动命令不再适用于当前系统。

# Deep Research

基于 LangGraph、FastAPI 和 Vue 的多 Agent 研究助手，支持问题拆解、网络搜索、本地知识库检索、证据分析和 Markdown 报告生成。

当前演示场景为企业 Agent 应用平台技术选型：提供业务目标、候选方案和硬性条件，研究部署、集成与维护取舍，并明确资料缺口。仍采用固定工作流编排。收尾调整及实际验证见 [技术选型收尾说明](docs/技术选型收尾说明.md)。

当前交付版本为 `harness-v1.2-evidence`，保留 8 个角色、11 个节点；工具由程序节点执行。交付记录与演示步骤见 [现有版本交付说明](docs/现有版本交付说明.md)，面试准备见 [项目面试逐字稿](docs/项目面试逐字稿-当前版本.md)。自主工具选择方案仅作为备选设计，尚未实施。

## 功能

- 保留 8 个角色，用 LangGraph 固定流程编排；快速问答与研究请求分流。
- 博查网络检索、Milvus 本地检索并行汇合，最多补搜 1 轮。
- Harness 控制模型/工具预算、结构校验、有限重试、证据支持与引用。
- 将原始需求固定为首个覆盖任务；复核后逐项展示已覆盖、部分覆盖、未覆盖，剔除结论会同步影响覆盖状态。
- 复核引文不符合原片段时最多修复一次，共享结构修复额度；修复仍不符合的结论排除。
- 复核可选择程序生成的 `quote_id`，由程序提取真实片段，避免模型誊抄原文；未知 ID 和冲突引文仍会拒绝。
- 取证与审计返回紧凑来源 ID，由程序保留真实标题、URL 和片段；明确的官方请求经过小型官方入口目录与域名/路径校验。目前目录覆盖 Dify、FastGPT，不是通用官方身份识别器。
- 被排除的额外候选结论单独保留审计记录；必要问题仍有缺口才影响覆盖，避免全问题已覆盖却被误降级。
- 没有有效证据时返回有限结果或失败说明，停止事实性报告写作。
- PostgreSQL 保存运行、事件、结果与检查点；SSE 支持按序号回放。
- 页面显示运行 ID、状态、调用次数、已知 Token、错误与引用片段；刷新读取原运行。
- JSON 滚动日志、应用级调用轨迹、Prometheus 格式 `/metrics`。
- 会话记忆保留，API/页面默认关闭，可按需启用。

身份隔离与记忆完善见 [记忆与权限隔离说明](docs/记忆与权限隔离说明.md)。研究流程和角色分工保留，新增服务端令牌身份、归属授权、私有知识资料范围与可管理的两层记忆。

首版修改、实际验证和范围说明见 [首版调整说明](docs/首版调整说明.md)。

## 技术选型演示

页面提供企业平台选型、输入模板和本地资料比较示例，不增加独立业务表单。先提供业务目标、2–3 个同层级候选及必要条件，再要求有来源的取舍与未知项。报告不保证总能给出确定推荐，运行成功不代表完成了实际部署或性能测量。

`examples/tech_selection/` 是四份人工整理的官方资料笔记，包含来源、核对日期和适用范围，不是网页全文或产品实测。先在 `.env.local` 设置 `RESEARCH_PUBLIC_KNOWLEDGE_COLLECTION` 为经确认可共享的集合，再使用入库入口导入：

```powershell
.\.venv\Scripts\python.exe local_run.py ingest examples/tech_selection --public
```

入库仍会追加，避免对同一集合重复导入。此次本机已导入独立集合 `tech_selection_v1` 并更新 `.env.local` 的 `KNOWLEDGE_COLLECTION`，原集合保留。其他机器可使用自己的集合，无需修改 RAG 实现。

使用“只依据本地知识库”或“仅使用本地资料”开头的明确请求时，初次及补搜均不调用博查；普通研究仍使用双路检索。联网模式需要有效 `BOCHA_API_KEY`。认证失败会展示错误，不会假装有搜索结果。

演示脚本将新研究与只读回放分开。先查看问题，无外部调用：

```powershell
.\.venv\Scripts\python.exe scripts/demo_research.py --list
```

运行演示脚本前在本地设置 `RESEARCH_ACCESS_TOKEN` 为自己的令牌；脚本不打印令牌。创建一次研究必须显式加 `--live`，会消耗模型、搜索或 Embedding 额度：

```powershell
.\.venv\Scripts\python.exe scripts/demo_research.py --case local-deploy --live
.\.venv\Scripts\python.exe scripts/demo_research.py --case cost-gap --live
.\.venv\Scripts\python.exe scripts/demo_research.py --case official-docker --live
```

脚本通过 SSE 接收结果，随后 GET 校验存储与完整事件回放，并保存到忽略提交的 `output/release-demos/`。读取已完成运行使用 `--run-id <运行ID>`，不会重新发起模型调用。已验证的报告快照见 [演示目录](docs/demos/README.md)；新运行结果可能变化，不作为效果保证。

## 本地运行

需要 Python 3.12、Node.js 20.19+ 或 22.12+、Docker Desktop，以及有效的百炼 API Key；联网检索还需要有效的博查 API Key。

在项目根目录的 PowerShell 中执行：

```powershell
Copy-Item .env.local.example .env.local
# 编辑 .env.local，填写 DASHSCOPE_API_KEY 和 BOCHA_API_KEY。
uv venv .venv --python 3.12 --cache-dir .uv-cache
uv pip install --python .venv\Scripts\python.exe --cache-dir .uv-cache -r requirements.txt
docker compose -f docker-compose.local.yml up -d --wait --wait-timeout 300
.\.venv\Scripts\python.exe scripts/setup_demo_auth.py
.\.venv\Scripts\python.exe local_run.py check
.\.venv\Scripts\python.exe local_run.py backend
```

在另一个 PowerShell 中安装前端依赖并启动：

```powershell
cd front\agent_front
npm ci
cd ..\..
.\.venv\Scripts\python.exe local_run.py frontend
```

打开本地 `.demo-credentials.local.json`，将一个随机 `token` 粘贴到前端的令牌输入框并验证；文件已忽略提交。访问前端 `http://127.0.0.1:5173`，接口文档 `http://127.0.0.1:8002/docs`。PostgreSQL 使用 5433 端口，Milvus 使用 19530 端口。

## 导入知识库

```powershell
.\.venv\Scripts\python.exe local_run.py ingest "D:\资料目录" --tenant-id team_a --user-id alice
```

支持 UTF-8 `.txt`、`.md`、`.markdown` 文件，目录递归导入。省略 `--user-id` 则租户内共享；公开资料须显式使用 `--public`。这属于可信的本地管理员操作。重复导入会追加文档。

## 首版默认预算

| 项目 | 默认值 |
| --- | --- |
| 补搜 | 最多 1 轮 |
| 模型调用 | 16 次，包含结构修复与支持性检查 |
| 博查 HTTP 尝试 | 12 次，包含失败与重试 |
| 工具调用 | 24 次，包含检索、记忆操作和 Embedding 批次 |
| 活动时间 | 180 秒；研究阶段预留 15 秒和 2 次模型调用收尾 |
| 并发研究 | 2 个；繁忙返回 HTTP 429 |
| 模型输入 | 28,000 字符；共享证据池最多 10 条 |

在 `.env.local` 中设置 `RESEARCH_MAX_*`，修改后重启后端。时间预算在调用边界检查，不会立即中断在途请求；用量未知时明确标记，费用不作估算。

## API 与运行记录

研究和记忆接口要求 `Authorization: Bearer <token>`。用户与租户由服务端绑定，越权查询运行或事件返回 404。页面默认关闭每次研究的记忆读写，可显式启用。

- `POST /api/v1/research/run`：启动并等待结果。
- `POST /api/v1/research/stream`：启动一次研究；响应头 `X-Research-Run-ID`，SSE 带 `id`/`seq`。
- `GET /api/v1/research/runs/{run_id}`：查看运行状态与结果。
- `GET /api/v1/research/runs/{run_id}/events?after_seq=10`：读取后续事件，也支持 `Last-Event-ID`。
- `GET /api/v1/auth/me`：当前令牌身份。
- `GET /api/v1/memory`、`PUT /api/v1/memory/profile`、`POST /api/v1/memory/notes`、`DELETE /api/v1/memory`：本人记忆管理，参数见记忆说明。
- `GET /health`：应用存活检查；`GET /metrics`：仅演示运维身份可访问的进程内聚合指标。

研究结果新增 `question_coverage`、`coverage_stage`、`missing_gaps`、`verification_rejections`；`run_summary.question_coverage` 保存覆盖统计。同步响应、SSE final、PG 结果与刷新读取共用这些字段。覆盖关联由程序检查，是否真正回答问题仍包含模型语义判断，需要人工复核。

终态包括 `completed`、`partial`、`failed`。刷新或断线后用 GET 读取原运行；再次 POST 会创建新运行。首版仅支持单后端进程，重启后将未完成运行标记为 `PROCESS_INTERRUPTED`，不自动恢复执行。

## 验证

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m unittest tests.test_controls tests.test_identity_memory.IdentityTests tests.test_evaluation -v
.\.venv\Scripts\python.exe -m evals.run --check-fixtures
```

以上控制测试不调用付费模型、博查或 Embedding。PostgreSQL 集成测试需设置 `RESEARCH_TEST_POSTGRES_DSN`，数据库名必须以 `_test` 结尾，详细步骤见 `evals/README.md`。

固定资料评测现有 20 个案例（16 开发、4 保留），包含业务标准答案、错误前提、无证据、过期记忆与个性化场景。支持多次运行、语义复核、分组报告及基线门禁。历史收尾记录见交付说明；当前结果以 [评估体系说明](docs/评估体系说明.md) 为准。资料格式校验不代表模型评测通过。

```powershell
# 使用真实模型，消耗模型 API 额度；检索固定为离线资料，不调用博查/Embedding。
.\.venv\Scripts\python.exe -m evals.run --live-model --repeats 2
cd front\agent_front
npm run build
```

当前免费回归共 73 项，包含原有 56 项和新增 17 项评测器测试；本机启用 PG 与 Milvus 集成验证。20 个案例各两次的首轮 AI 辅助复核语义通过率为 65%，质量门禁未通过；完整现状基线保留在 `evals/baselines/`，不能把引用合法率当准确率。默认 CI 不运行付费模型评测。

`.github/workflows/checks.yml` 配置控制测试、身份隔离、评测器回归、资料校验、隔离 PostgreSQL 集成及前端构建。真实模型与 Embedding 评测手动运行；语义复核记录 human/assistant 来源，待复核不算通过。引用 ID 有效不等于事实准确。命令与门禁见 [评测指南](evals/README.md)。

## 当前范围

网络证据来自搜索摘要，未抓取网页正文；本地入库沿用 TXT/Markdown，重复导入会追加。当前新增静态令牌演示认证与服务端数据隔离，仍未增加注册找回密码、Redis、持久化队列、取消/续跑或 OTel 采集平台。后端仍按单进程用于本地开发与演示。

运行与事件可以手动清理：`python scripts/cleanup_runs.py --days 14`。该脚本只删除到期终态运行及其事件，不清理检查点、知识库或记忆。

## 目录

| 路径 | 用途 |
| --- | --- |
| `app/backend/` | FastAPI 接口与 SSE |
| `app/mult_agents/` | Agent、工作流、状态与工具 |
| `app/mult_agents/harness/` | 预算、校验、日志、指标和 Embedding 边界 |
| `app/mult_agents/rag/` | 知识库检索与入库 |
| `app/mult_agents/memory/` | 记忆管理 |
| `front/agent_front/` | Vue 前端 |
| `tests/`、`evals/` | 控制回归、数据库集成、固定资料质量评测 |
| `local_run.py` | 本地启动与检查入口 |
| `docker-compose.local.yml` | 本地 PostgreSQL 与 Milvus |

模型、检索和入库会消耗外部 API 额度。真实 Key、本地数据库、依赖目录和生成文件不提交到版本库。
