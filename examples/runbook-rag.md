# 轻量排障手册 RAG

链路位于当前故障项目：手册文本 → 切分 → Embedding → 本地 JSON 向量索引 → 余弦相似度 Top-K → 知识证据 → 现有诊断与复核。没有独立 RAG 服务、向量数据库、重排模型或新增问答模型。

Knowledge 阶段通过现有工具 `search_runbooks` 运行检索，Supervisor 决定查询内容。只检索并返回原文片段，答案仍由已有 Diagnosis 生成。手册提供排查方法，不能替代当前故障日志或指标。

## 配置和导入

使用项目基础依赖即可。给**已有服务配置**的 `observations` 增加：

```json
{"runbook_rag": {"index_dir": "app/data/runbooks"}}
```

默认使用 `text-embedding-v1`、1536 维，通过 `DASHSCOPE_API_KEY` 环境变量读取凭据。可在配置中调整向量模型、维度、Top-K、相似度门槛；查询和导入必须使用同一设置。完整示例见 [services.rag.example.json](services.rag.example.json)。不需要额外服务登记系统或知识库权限管理。

资料格式见 [runbooks.example.json](runbooks.example.json)：每篇填写来源编号、类别、带时区的更新时间和完整文本。Markdown 可以作为 text 内容；当前不做 PDF/OCR 或自动目录爬取。

```powershell
.\.venv\Scripts\python.exe scripts/ingest_runbooks.py --services-file examples/services.rag.example.json --tenant synthetic_demo --service example-api --environment staging --documents examples/runbooks.example.json
```

默认只显示导入计划，不访问网络。加 `--execute` 后调用 Embedding 并写入索引。真实运行使用包含该配置的 `INCIDENT_SERVICES_FILE`；示例服务只有知识渠道，需要补充实际指标、日志来源。

默认每片 500 字、重叠 50 字，最多 200 片，8 片为一批生成向量；检索默认返回 3 片，最多 4 片。本地逐片扫描，适合少量排障手册。

更新时用**完整资料清单**重新运行导入，成功后原子替换该服务的旧索引，清单中移除的文档随之移除。导入失败保留旧索引。无需轮换命名空间、发布就绪标记或清理数据库集合。多个管理员应串行导入同一服务；并发导入以最后一次成功写入为准。

## 范围与成本

索引文件按已有租户、服务、环境、版本和向量设置自动区分。模型不能选择文件路径或其他服务资料；读取时检查整个文件的范围与维度。相对 index_dir 基于项目根目录，默认目录已被 Git 忽略。部署时让导入脚本和后端共享该目录。

没有索引、索引损坏、凭据缺失或接口异常会返回缺口。配置了 RAG 后不自动退回关键词检索；未配置的服务沿用原词匹配。

向量化调用计入现有模型预算与词元记录；每次检索最多一次查询 Embedding，不额外调用问答模型。文档与向量化请求先脱敏。返回来源编号、更新时间、相似度和现有字段引用，索引内容哈希用作证据版本。聊天和向量模型单价不同，混合运行暂不估算总货币费用。

## 免费验证

```powershell
.\.venv\Scripts\python.exe scripts/verify_runbook_rag.py
```

真实写入和读取临时文件索引，经实际工具执行器注册证据，再清理临时索引。Embedding 使用固定测试向量，不访问付费接口。报告表格为简体中文。

测试覆盖范围隔离、格式校验、失败保留旧索引、文档更新与移除、Top-K、脱敏、预算和引用。链路验证不代表真实语义准确率或诊断效果提升；当前没有导入真实手册，也没有覆盖业务服务配置。
