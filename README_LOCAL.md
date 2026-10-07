# Windows 本地启动

本仓库当前运行研究底座，IT 工单功能待实施。以下是新副本的首次安装步骤；复制源码没有复制依赖、密钥或数据库。

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
