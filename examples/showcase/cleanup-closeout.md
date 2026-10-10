# 项目清理与合并收尾

2026-10-11 清理围绕当前 IT 工单系统进行，不扩大 Agent、前端或运维修复功能范围。

## 清理范围

- 删除10个未使用的 Vue 欢迎组件、配套图标及模板素材。入口实际引用的业务组件和样式保留。
- 根目录的研究 README 移入 `archive/research/legacy-guide.md`，四份研究技术选型笔记移入 `archive/research/examples/tech_selection/`。
- 清理前根 README 保存在 [历史开发说明](development-history.md)，保留此前对照结果和验收限制；当前根 README 只描述现有系统、源码模块、演示和运行入口。
- `pyproject.toml` 更正项目名称及 Python 版本，依赖统一读取已有 `requirements.txt`，仅打包 IT 模块、FastAPI 入口及 SQL 迁移。移除研究专用 Milvus / Redis 检查点依赖声明，不重建或卸载现有环境。
- `.env.example` 与 `.env.local.example` 统一为当前 IT 模板；前端地址覆盖统一使用 `INCIDENT_API_TARGET`，未设置时按 `PORT` 连接。
- 忽略本地打包生成的 `build/`；CI 加入免费的并行控制演示。
- 将已验证的 Investigation / Knowledge 并行取证一并纳入收尾。

## 保留内容

归档中52项原覆盖、审计及研究源码快照保持清单哈希；全部冻结评测、历史对照、失败结果与验收记录保留。`docs/` 仍仅在本地维护，不加入 Git。

现有 `.env`、身份凭据、`config.json`、服务登记、数据库、业务容器、运行报告和日志不清除。`config.json` 的旧字段由现有配置读取器忽略，仅模型、Key、PostgreSQL DSN 参与 IT 运行。旧只读运行 URL 与数据库表名保留兼容。

归档不是运行链路；单 Agent 对照、开发案例、Provider 适配、评测和审批执行器均仍有用途，保留在当前工程。

## 验证与发布范围

本轮仅执行免费控制测试、离线打包、前端类型/构建及本地演示，不调用付费模型，不执行业务修复。源代码可合并成为求职展示基线；真实模型质量和生产性能仍按历史结果及现有边界解释。

本地最终检查见 [cleanup-validation.json](cleanup-validation.json)：全量357项中350项通过、7项隔离数据库测试跳过；52项归档内容哈希一致；前端类型与生产构建、离线 wheel（67项锁定依赖、API入口及两份迁移）、原免费展示和串并行演示均通过。GitHub 隔离 PostgreSQL 检查另以提交后的 CI 结果为准。

合并前以实际提交的 GitHub Actions 为最终检查：免费后端、前端构建、隔离 PostgreSQL 集成。合并保留开发提交历史，随后将本地切换到 `main`；不删除开发分支或原数据。
