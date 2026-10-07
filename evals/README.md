# 评估闭环

评估分为工程控制、固定资料工作流、真实检索与记忆三个层面。默认 CI 不使用付费 API。评估通过不等于生产 SLA；失败结果和待复核结果都保留。

## 1. 免费工程回归

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m unittest tests.test_controls tests.test_identity_memory.IdentityTests tests.test_evaluation -v
.\.venv\Scripts\python.exe -m evals.run --check-fixtures
```

新增评测器测试覆盖错误引用、原文校验、预算、无证据拒答、复核绑定、缺项、重复与未完成样本、基线可比性及检索排名。它们验证评测器的行为，不是模型质量结果。原有控制回归和权限/记忆集成仍保留。

PostgreSQL 集成必须使用名称以 `_test` 结尾的独立库。本地 Compose 首次创建测试库：

```powershell
docker compose -f docker-compose.local.yml exec postgres psql -U deepresearch -d deepresearch -c "CREATE DATABASE deepresearch_test"
$env:RESEARCH_TEST_POSTGRES_DSN='postgresql://deepresearch:deepresearch_local@127.0.0.1:5433/deepresearch_test'
# 可选：包含真实 Milvus 的权限预过滤测试，使用固定测试向量，不调用 Embedding。
$env:RESEARCH_TEST_MILVUS_URI='http://127.0.0.1:19530'
.\.venv\Scripts\python.exe -m unittest tests.test_postgres tests.test_identity_memory.MemoryPostgresTests -v
```

## 2. 固定资料与真实模型

`fixtures/solutions.md` 的 A/B/C/TEAM/D 均为维护者编写的虚构资料。`cases.json` 共 20 个案例：16 个开发样本、4 个保留样本，标注预期行为、业务要求、支持原文和禁止结论。新增错误前提纠正、限流与并发混淆、历史记忆过期冲突、称呼与风格注入。保留集也属于人工合成小样本；一旦用于调提示词，应移入开发集并另写新保留集。

```powershell
# 会消耗模型额度；读取 .env.local/.env 当前配置。
.\.venv\Scripts\python.exe -m evals.run --live-model --repeats 2
# 只调试一个案例；使用新输出文件。
.\.venv\Scripts\python.exe -m evals.run --live-model --case q08 --repeats 2 --output output/eval-q08-v2.json
# 单独保留集
.\.venv\Scripts\python.exe -m evals.run --live-model --split holdout --repeats 2
```

使用真实图、Agent 和模型；检索适配器返回固定片段，不调用博查/Milvus/Embedding。记忆案例注入固定快照，未经过真实记忆检索。`web_calls` 表示占位检索调用，不是博查 HTTP 次数。每次用独立 run/thread ID 和内存 Checkpointer；最多补搜一轮。连续两次供应商失败后停止新增付费调用，原始结果仍保存且标为未完成。

默认输出 `output/evaluations/<UTC时间-随机ID>/evaluation.json`、`.reviews.json`、`.report.md`。指定输出不能覆盖已有 JSON；每个样本结束后原子落盘。记录完整报告、候选/接受结论、证据、拒绝与缺口、模型参数、资料/数据集/提示词/结构契约版本、依赖版本、Token、调用次数和耗时。供应商实际模型修订版本和 HTTP 重试次数不可固定或完整观察，费用未估算。

## 3. 语义复核

程序校验：引用属于实际来源、支持引文来自原文、预算未越界、无证据案例未生成研究事实。引用 ID 合法不能证明引用支持结论；关键词命中仅保留作诊断。

编辑 `.reviews.json`，逐条复核**最终报告**：

- 每个标准答案要求判 `met / partial / missing`，覆盖分数为 1 / 0.5 / 0。
- 每个接受结论判 `supported / unsupported / unclear`，查数字、否定、版本、范围和建议前提。
- 判断全文事实是否正确、不确定性是否适当、是否出现禁止结论；不能只检查结论列表。
- 填 `reviewer.name`、`reviewer.kind`（`human` 或 `assistant`）与原因，最后将 `review_status` 改为 `complete`。AI 辅助复核不能标为人工复核。

复核通过要求：程序通过、全部业务要求满足、全部接受结论受支持、全文正确、未知项处理恰当、无禁止结论。`partial` 状态本身不会自动判业务失败：合理拒绝无证据问题也可能符合标准答案。`completed` 同样不会自动判语义通过。

复核通过运行 ID、结果 SHA256 与评估/数据集版本绑定。改写原始结果后旧复核失效；少评、重复评、错评其他运行将拒绝生成有效成绩。未复核为 pending，不计作通过。

```powershell
.\.venv\Scripts\python.exe -m evals.report output/evaluations/<目录>/evaluation.json --reviews output/evaluations/<目录>/evaluation.reviews.json --check-gates
```

退出码：0 通过，2 未达阈值，3 待复核，4 样本未完成。报告提供整体、分组、每次运行、mean/p50/p95 耗时、Token 与重复波动。标准答案覆盖与工作流自己生成的 `question_coverage` 分开，后者只作诊断。

## 4. 基线回归

初始门禁：程序通过率 100%、全部样本复核、语义通过率 ≥85%、标准答案宏平均覆盖 ≥90%。这些是本项目验收约定，可通过 `--policy <JSON>` 覆盖；配置键见 `metrics.DEFAULT_GATES`，值限定 0..1。

```powershell
.\.venv\Scripts\python.exe -m evals.report output/new/evaluation.json --reviews output/new/evaluation.reviews.json --baseline output/old/evaluation.json --baseline-reviews output/old/evaluation.reviews.json --check-gates
```

基线要求相同资料、数据集、评分器、案例、重复次数，双方完整且复核者类型分布相同。默认允许语义通过率下降最多 5 个百分点、平均耗时增长最多 25%。不同提示词/代码/模型用于比较改造效果，原始文件保留差异；未改配置的再次运行用于观察波动。两次运行不足以证明统计显著。

首轮未达标现状基线保留在 `evals/baselines/qwen-turbo-v2/`，可免费重建报告。它是诊断参照，不能当成已通过验收的发布版本。未来报告比较时，可将该目录中的 `workflow.json` 与 `assistant-reviews.json` 作为上述基线参数。

## 5. 真实检索与记忆基准

```powershell
$env:RESEARCH_TEST_POSTGRES_DSN='postgresql://deepresearch:deepresearch_local@127.0.0.1:5433/deepresearch_test'
# 会消耗少量 text-embedding-v1 额度；需本地 Milvus 和测试 PG 已启动。
.\.venv\Scripts\python.exe -m evals.retrieval --live-embeddings
```

知识基准：5 段公开虚构资料、8 个查询、top-3，统计 Recall@3、MRR 和 Hit@3。记忆基准：3 条本人记忆和 2 条其他用户/租户干扰记忆、3 个查询、top-2；另检查删除、过期与外用户数据。使用现有 `RAGSystem`、`ScopedMemoryManager`、真实 DashScope Embedding 和 Milvus。观察真实向量返回与索引/查询异常，关键词降级不能冒充向量召回；PG 权威记录继续做 TTL 和所有者校验。

每次新建随机前缀集合，PG 仅用独立测试库和随机租户。结束清理该次租户与集合，清理失败也判门禁失败。原始结果保存在新目录，可指定 `--output`，不能覆盖已有结果。初始门禁：两类 Recall ≥90%、无失效/外用户命中、无适配器错误、清理成功。此基准不验证大规模语料，也不验证真实记忆检索后生成回答的端到端效果。

## 6. CI 与范围

GitHub Actions 运行免费工程/评测器测试、资料标注校验、独立 PG 集成及前端构建。付费质量评测和真实 Embedding 基准手动运行，语义复核与回归门禁用保存的文件执行。未推送不会触发远端 CI。

当前没有全网时效性/搜索召回标注、网页正文支持率、生产用户数据集、并发压测、长期在线 A/B 或独立双人复核。首轮实测和可复核成绩见 [评估体系说明](../docs/评估体系说明.md)。
