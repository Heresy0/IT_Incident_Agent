# 运维辅助项目阶段收尾

本项目以可演示、可评测、可内部试用的运维辅助原型收尾。当前交付包含诊断和证据引用、人工审批与受控修复、pgvector 手册检索，以及 .md / .txt 上传与简单切分。阶段标签为 `codex/prototype-closeout-2026-10-13`；标签固定代码和本文，未部署到现有运行实例。

诊断规则继续以 `codex/default-flow-2026-10-13` 为历史对照，默认三个模型角色与程序取证、有限补查、复核及预算不变。本次不增加新的诊断实验或产品功能。应用代码父提交为 `aa7a71e`，本机应用源码指纹为 `073d92c4f426514f`；指纹读取文件字节，Git 换行设置不同可能改变其值。

## 从这里开始

新机器使用 Python 3.12 和 Node.js 24，先按 [Windows 本地启动](../../README_LOCAL.md) 安装锁定依赖。配置和身份保存在本地，不复制其他项目的密钥。

已有项目环境时，先做免费演示，不启动数据库或真实模型：

```powershell
.\.venv\Scripts\python.exe scripts/showcase_incident.py
```

固定展示正常诊断、复核触发一次补查、预算不足受控退出，并展示模拟审批执行闭环。预算不足的 partial 是预期结果。脚本模型、合成观测和旧 Agent 演示路径只验证控制流程，不作为默认真实模型诊断的质量成绩。

需要页面演示时，先在 `front/agent_front` 运行 `npm ci`、`npm run build`。以下路径使用独立临时测试库和本机验收服务，不读取日常工作流配置；启动前确认专用项目尚未存在、5435 与 8175 未被占用：

```powershell
docker compose -f docker-compose.test.yml -p it-incident-closeout ps -aq
# 上一条有容器输出时先核对已有测试项目，不直接覆盖。
docker compose -f docker-compose.test.yml -p it-incident-closeout up -d --wait --wait-timeout 90 postgres
$env:INCIDENT_TEST_POSTGRES_DSN='postgresql://incident_test:incident_test_local@127.0.0.1:5435/incident_test'
.\.venv\Scripts\python.exe scripts/closeout_demo_server.py --serve
```

页面地址为 `http://127.0.0.1:8175`，合成演示令牌为 `closeout-demo-operator`，只用于这一个临时服务。省略 `--serve` 只输出计划，不连接数据库。脚本拒绝非 *_test 数据库，只监听 127.0.0.1，向量请求全部使用固定测试传输，真实模型模式拒绝测试 Key；修复执行器只改变内存中的模拟状态。

服务运行时依次执行：

1. 登录并展开“排障手册 · 上传与切分”，分别选择 [Markdown 样例](closeout-runbook.md) 和 [TXT 样例](closeout-runbook.txt)，免费预览后确认导入。预期分别为 3 片、1 片，两个文件都保留在列表中。
2. 选择开发案例 case_001，创建工单，再点击“启动免费演示”。核对报告、来源引用、55 条事件及“待人工确认”状态；这不是真实根因确认。
3. 独立模拟审批闭环可运行 `scripts/demo_remediation.py`。正式页面对开发案例隐藏真实修复提案入口，不能用合成诊断操作真实目标。
4. 本次页面审批验收通过原有 HTTP 提案接口预置了 `live=false` 的模拟方案，然后在页面审批、单独确认执行，核对恢复验证与阶段记录。没有改动页面的开发案例保护，也没有执行真实容器重启。
5. 人工填写合成原因和处理结果，确认解决；整页刷新后重新登录，核对已关闭工单、报告、手册和修复记录仍可读取，模拟动作次数仍为 1。

需要复验第 4 步时，在本次临时服务的 `/docs` 使用合成令牌授权，向 `POST /api/v1/incidents/{incident_id}/repairs` 提交以下模板，将编号换成刚生成的工单、运行和 observation 证据编号。这仅在上述验收服务登记的 offline_demo 执行器上有效；正常服务没有该模拟登记。

```json
{
  "request_key": "closeout-demo-propose",
  "revision": 1,
  "run_id": "从诊断报告复制运行编号",
  "action": "restart_service",
  "target": "api",
  "parameters": {},
  "evidence_ids": ["从报告复制一个 observation 证据编号"],
  "reason": "隔离收尾验收，仅模拟状态恢复",
  "approval_details": {
    "condition": "仅适用于本次合成身份和模拟执行器",
    "risk": "模拟操作，真实业务写入为零",
    "expected_result": "模拟 unhealthy 转 healthy，然后人工确认合成工单"
  }
}
```

创建后在页面点击“刷新修复方案”，独立审批、确认执行，再填写人工解决信息。通过 `GET /_closeout/status` 可核对审批前后 simulated_writes 为 0、执行后为 1；此状态接口只存在于验收辅助服务。正式业务仍按现有服务登记与真实数据源操作。

结束时在服务终端按 Ctrl+C。服务只清理本次随机租户和手册范围；确认 `output/closeout-ui/server-summary.json` 的 cleanup 为 completed 后，再停止本次专用临时测试库：

```powershell
docker compose -f docker-compose.test.yml -p it-incident-closeout down
Remove-Item Env:INCIDENT_TEST_POSTGRES_DSN
```

不使用 down -v，不操作业务 Compose、其他容器、数据库或私有配置。临时测试库数据使用 tmpfs。正常后端与前端的开发启动仍分别使用 `local_run.py backend`、`local_run.py frontend`，步骤见本地启动文档。

## 验证依据

机器摘要见 [prototype-closeout-validation.json](prototype-closeout-validation.json)。原始日志、完整合成页面截图、带编号的运行和临时身份文件在本机 output 中，不提交。

| 检查 | 结果 | 解释范围 |
| --- | --- | --- |
| 全量免费回归 | 643 项，623 通过，20 项数据库检查跳过 | 跳过项不计为通过，另行验证 |
| 隔离真实数据库检查 | 上述 20 项全部通过 | 工单持久化、修复状态与 pgvector SQL |
| 前端类型与生产构建 | 已通过 | 本次在已有环境重新核对 |
| 免费固定展示 | 正常、补查、预算退出及 7 项控制检查通过 | 脚本控制，不评价模型质量 |
| 页面 MD / TXT 导入 | 通过，分别 3 片与 1 片 | 真数据库；向量服务模拟 |
| 页面诊断与事件回放 | 通过 | case_001，6 次脚本模型、2 次工具，55 条事件 |
| 页面审批、执行、恢复验证及人工关闭 | 通过 | 方案经 API 预置，模拟动作 1 次 |
| 整页刷新与重新登录 | 通过 | 数据仍可读取，没有再次执行 |
| 付费模型及真实修复动作 | 0 | 本次仅免费隔离验收 |

此前上传版本的 620 项离线检查、20 项数据库检查、前端构建与安装包检查均通过。本次在相同应用代码上补充验收辅助脚本、其隔离保护测试、页面交互与交付说明。

本机数据库验证使用已缓存的 pgvector 0.8.6 / PostgreSQL 16 镜像。配置的默认镜像为 0.8.7，历史下载遇到网络 EOF，不能把本机成绩写成 0.8.7 实测。需要离线复现时，可显式设置 INCIDENT_POSTGRES_TEST_IMAGE 为本机已有兼容测试镜像；业务 Compose 不受此覆盖变量影响。

隔离验收脚本首次启动未完全注入应用生命周期依赖，连接超时退出，未进入初始化。随后修正为显式测试工作流与测试配置，完成页面复验。该修正仅位于验收辅助脚本，没有修改默认诊断流程。

## 使用边界和维护方式

`completed` 是流程完成，`passed` 是本次模型复核通过，`verified` 是登记恢复检查通过，均不自动表示唯一根因成立或工单已解决。人工核对原因和业务恢复后才关闭工单。

诊断仍可能漏选关键取证、误用反证或复核误拦。RAG 手册是排查参考，不能替代故障现场观测。当前验收不证明真实手册语义检索质量、诊断准确率、行业泛化、并发容量或生产 SLA。

程序保护覆盖身份与服务范围、来源引用、预算、有限纠正及补查；它不能证明模型的语义判断正确。诊断包保持可选，默认不启用，不继续叠加新的实验开关。

运行支持单进程；诊断中断标记失败、修复中断转人工处理，不从检查点继续外部写操作。应用维护动作需要目标应用提供约定接口；真实扩缩容和版本回滚尚未实现。

上传只支持 UTF-8 的 .md / .txt，最多 256 KiB、10 万字符；默认每片最多 500 字、重叠最多 50 字，同范围总计最多 200 片。Markdown 支持 ATX 标题及代码围栏识别，不增加 PDF/OCR 或语义切分。网页上传更新单篇；批量导入脚本替换完整清单。上传中断后先等待并核对资料列表，跨进程持久化幂等记录尚未实现，人工重试可能再次产生向量费用。

后续进入维护阶段，只修实际复现的问题，记录案例、应用版本、预算和证据。新增需求另开分支，以当前阶段标签为交付对照、原诊断冻结标签为行为对照；保留失败与人工未确认结果，不以增加角色、规则或开关作为默认优化方向。
