# 旧版诊断链路归档与恢复

2026-10-19 进一步清理的共享模块残留与历史评估辅助见[剩余旧版归档](residual-20261019/README.md)。原退役清单与回滚材料继续保持不变。

2026-10-18 退役前提交：`da288f60724848215dbcc49bc380aa047f0d914a`。固定标签：`rollback/pre-legacy-retirement-20261018`。

当前运行入口统一为 `autonomous_v2 / closed_loop`。此目录保存旧版专属应用代码、脚本和测试的原始字节，逐文件 SHA256 见 [manifest.json](manifest.json)。归档不参与当前运行导入、打包或测试发现；不要把本目录加入当前服务的 `PYTHONPATH`。

## 恢复完整旧版

在独立目录恢复完整仓库，避免把新旧模块混装：

```powershell
git worktree add --detach ../it_incident_agent_legacy rollback/pre-legacy-retirement-20261018
```

本机还保存了不依赖原仓库的完整 Git bundle。以下命令可恢复到独立目录：

```powershell
git clone --branch rollback/pre-legacy-retirement-20261018 output/legacy-retirement-20261018/rollback.bundle ../it_incident_agent_legacy_backup
```

如需精确复现冻结的文件字节，使用本机 `output/legacy-retirement-20261018/rollback-source.zip`。它包含退役前全部 1,036 个受跟踪文件，保留原换行符；bundle 与 ZIP 的哈希记录在清单中。两者不含本机模型密钥或数据库凭据，原数据库和运行证据继续保留在本机原位置。

本轮已在 `output/legacy-retirement-20261018/restore-check/` 实际恢复完整 Git 历史，回填 ZIP 并逐一核对 1,036 个文件，再免费执行旧版 `scripts/demo_standard_flow.py`，结果为 `completed / passed`、0 次付费请求。恢复的是原版本及其既有限制，不代表旧版所有历史命令都成功。

## 共享能力与新入口

原 schema 压缩迁入 [runtime/schema.py](../../app/runtime/schema.py)；引用校验和展开迁入 [evidence/validation.py](../../app/evidence/validation.py) 与 [evidence/references.py](../../app/evidence/references.py)。相关函数体保持不变，并以 AST 对照测试保护。v2 调查、诊断、审阅与质疑提示词保持原文。

普通后端、`main.py` 与 CI 的免费演示均使用 v2。显式配置旧引擎会报错；配置了准入策略但未通过时拒绝创建诊断，不再回退旧版。历史报告按原状态和原版本继续读取。

## 评估证据边界

原评估 JSON、数据集、评分、原始消息、报告和事件均保留原位，未重新评分。旧评估辅助代码若依赖退役引擎，则随源码归档；完整旧版可恢复执行。`scripts/evaluate_frozen_versions.py` 仍通过隔离的冻结源码子进程比较版本，当前后端不导入旧编排器。

当前离线测试不再发现本目录中的旧版专属测试；新版测试数量不能与退役前直接比较为质量提升。本轮只验证依赖解耦、入口、控制流程及历史报告交付，没有新的真实模型质量成绩。

本轮结果、保护范围和本地原档哈希见[退役与解耦验收记录](retirement-validation-20261018.json)。
