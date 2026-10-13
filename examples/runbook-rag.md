# 轻量排障手册 RAG

链路位于当前故障项目：手册文本 → 切分 → Embedding → PostgreSQL / pgvector → 余弦相似度 Top-K → 知识证据 → 现有诊断与复核。复用 PostgreSQL，仅增加 pgvector 扩展、手册表和片段表；不增加独立 RAG 服务、重排模型或问答模型。

Knowledge 阶段通过现有工具 `search_runbooks` 运行检索，Supervisor 决定查询内容。只检索并返回原文片段，答案仍由已有 Diagnosis 生成。手册提供排查方法，不能替代当前故障日志或指标。

## 配置和导入

Python 使用项目基础依赖中的 psycopg，不增加向量客户端依赖。数据库需安装 pgvector 扩展；本地 Compose 默认镜像为 pgvector/pgvector:0.8.7-pg16-bookworm，保留 PostgreSQL 16。首次显式导入会创建 vector 扩展和两张知识表；检索阶段不执行建表或扩展安装。若数据库账号无建表／扩展权限，需管理员先启用 vector 扩展，再顺序执行 app/knowledge/migrations/001_runbooks.sql、002_source_name.sql。给**已有服务配置**的 `observations` 增加：

```json
{"runbook_rag": {"database_dsn_env": "POSTGRES_DSN"}}
```

数据库连接默认从 POSTGRES_DSN 环境变量读取，也可登记其他连接变量名；连接串和凭据不进入模型输入或报表。默认使用 `text-embedding-v1`、1536 维，通过 `DASHSCOPE_API_KEY` 环境变量读取凭据。可在配置中调整向量模型、维度、Top-K、相似度门槛；查询和导入必须使用同一设置。完整示例见 [services.rag.example.json](services.rag.example.json)。不需要额外服务登记系统或知识库权限管理。

批量资料格式见 [runbooks.example.json](runbooks.example.json)：每篇填写来源编号、类别、带时区的更新时间和完整文本。可选 source_format 为 markdown（按标题切分）或 text（默认按段落切分），source_name 用于展示文件名；当前不做 PDF/OCR 或自动目录爬取。

```powershell
.\.venv\Scripts\python.exe scripts/ingest_runbooks.py --services-file examples/services.rag.example.json --tenant synthetic_demo --service example-api --environment staging --documents examples/runbooks.example.json
```

默认只显示导入计划，不访问网络。加 `--execute` 后调用 Embedding 并写入索引。真实运行使用包含该配置的 `INCIDENT_SERVICES_FILE`；示例服务只有知识渠道，需要补充实际指标、日志来源。

默认每片 500 字、重叠 50 字，最多 200 片，8 片为一批生成向量；检索默认返回 3 片，最多 4 片。使用 SQL 余弦距离精确排序，暂不增加 HNSW／IVFFlat。

更新时用**完整资料清单**重新运行导入。程序先记录当前文档版本并生成全部向量，再在事务中替换当前范围的文档与片段；清单中移除的文档随之移除。写入失败整体回滚，已有资料保持可用。同范围导入通过事务锁串行提交，若期间已有新版本提交，本次返回 RAG_INDEX_CHANGED，不覆盖新资料，需要核对后重新导入。

表名为 incident_runbook_documents 和 incident_runbook_chunks；片段通过外键关联文档。向量长度必须与文档配置一致，数据库拒绝零向量。两张表与工单、运行表分开，导入不清理其他服务或业务表。浏览器上传的展示名称保存在可选 source_name 列；已有库通过 002_source_name.sql 增加此列，显式导入自动应用该迁移。当前不提供删除接口。

## 前端上传与简单切分

使用 operator 身份连接前端后，展开顶部“排障手册 · 上传与切分”，选择已配置 runbook_rag 的服务范围和手册分类，选择文件，再点击“免费预览切分”。确认片段和向量化费用后点击“确认并导入”。预览不调用 Embedding、不创建知识表；导入使用现有向量服务并返回请求数、已知词元用量和耗时。普通用户继续使用现有诊断入口。

仅接受 UTF-8 编码（可带 BOM）的 .md、.txt，单篇最大 256 KiB、10 万字符。MD 按 ATX 标题（# 到 ######）分节，长章节继续按段落与标点切分，每片重复当前标题路径；代码围栏内的 # 不作为标题。TXT 按空行、换行和标点递归切分。每片最多采用服务配置中的 chunk_size（默认 500）字，重叠最多 chunk_overlap（默认 50）字；标题上下文占用片段长度，超长标题会截短。不包含语义切分、OCR、Setext 标题识别或重排。预览最多展示前 5 片，导入保存全部片段。

上传前先脱敏完整文本，再切分和向量化。资料仍按租户、服务、环境、服务版本和向量设置隔离。服务器根据正规化且不区分大小写的完整文件名生成来源编号；同范围同名文件为更新，改名视为新增。上传只替换这一篇，其他手册保留；批量脚本仍替换完整清单，混用时需把待保留的上传资料也纳入清单。同范围所有资料总计最多 200 片，超过限制会在向量化前拒绝；并发提交时再次核对限制。

接口全部使用现有 Bearer 身份，仅对已授权服务的 operator 开放：

| 方法和路径 | 用途 |
| --- | --- |
| GET /api/v1/runbooks/services | 可上传的服务范围和切分设置 |
| GET /api/v1/runbooks | 当前范围手册列表与版本 |
| POST /api/v1/runbooks/preview | 免费校验与切分预览 |
| POST /api/v1/runbooks/upload | 经确认后向量化并原子更新单篇手册 |

列表、预览和上传使用 query 参数 service、environment、service_version；文件请求另带 filename、category（resource / dependency / configuration）。请求体直接传文件字节，Content-Type 为 application/octet-stream，不使用 multipart，不增加解析依赖。接口文档提供二进制请求体。

上传必须带预览返回的 preview_digest、expected_revision（新文档为空字符串）和 embedding_authorized=true。文件或配置与预览不一致返回 409；版本已更新同样返回 409，要求重新预览，旧版本不覆盖新资料。数据库写入在事务中完成，向量失败或提交失败保留旧文档。每进程最多同时处理 2 个导入任务，超过返回 429；每次最多 25 批向量请求，单批 8 片，时间预算 180 秒（不是强制终止所有 I/O 的墙钟超时）。

页面不会自动重试上传。连接中断或页面关闭时，服务端可能仍在导入，应等待后刷新资料列表核对；接口不提供跨进程、持久化的请求幂等记录，人工重复提交仍可能再次产生向量费用。不要把该接口当作后台批处理队列。

## 范围与成本

资料按已有租户、服务、环境、版本和向量设置自动区分。查询同时校验身份、文档元数据和 SQL 返回片段，并在 SQL 中固定范围过滤。元数据与向量检索在同一只读、可重复读事务中完成，避免并发更新导致版本和片段混用。数据权限由既有服务身份校验与范围条件保证，本实现没有自动添加数据库行级权限策略。

旧 JSON 索引不再读取，也不会自动删除或迁移。已有配置中的 index_dir 需替换为 database_dsn_env，原始资料可通过原导入脚本写入新库。旧实现可从冻结标签恢复。

没有索引、索引损坏、凭据缺失或接口异常会返回缺口。配置了 RAG 后不自动退回关键词检索；未配置的服务沿用原词匹配。

向量化调用计入现有模型预算与词元记录；每次检索最多一次查询 Embedding，不额外调用问答模型。文档与向量化请求先脱敏。返回来源编号、更新时间、相似度和现有字段引用，每篇文档的内容／向量哈希用作证据版本。聊天和向量模型单价不同，混合运行暂不估算总货币费用。

## 免费验证

以下命令仅显示计划：

```powershell
.\.venv\Scripts\python.exe scripts/verify_runbook_rag.py
```

本机 Docker 可用时，启动本项目独立临时测试库，完成数据库与 RAG 验收后自动停止：

```powershell
.\.venv\Scripts\python.exe scripts/verify_postgres.py --start
```

也可将 INCIDENT_TEST_RUNBOOK_DSN 指向明确隔离的 *_test 库，再运行 verify_runbook_rag.py --execute。验收脚本拒绝日常业务库。HTTP Embedding 使用固定测试向量，没有付费模型调用；SQL 写入、过滤、距离计算、事务回滚和一致性读取是真实执行。测试仅清理本次生成的随机范围，不删除知识表或其他数据。报表使用简体中文。

CI 使用同一 pgvector 镜像运行数据库测试。离线环境如需用已有兼容镜像验证，可设置 INCIDENT_POSTGRES_TEST_IMAGE；这只覆盖专用测试 Compose 的镜像，报告保留实际数据库版本。业务 Compose 不受该变量影响。

验收覆盖范围隔离、不同维度共存、失败保留旧资料、旧版本导入拒绝覆盖、并发更新下的一致读取、Top-K、类别和分数门槛、数据库约束、预算和引用。该验证不代表真实手册检索质量或诊断质量提升。

接口依据：[pgvector 官方检索与安装说明](https://github.com/pgvector/pgvector)。
