# 中低风险维护操作

本批在冻结基线之上的独立分支增加五种操作，沿用诊断复核、方案创建、operator 审批、单独确认执行、单次写入和有界恢复验证。冻结标签 codex/default-flow-2026-10-13 保持原内容。新能力不会自动配置到业务服务，也不会因为模型建议而直接执行。

| 动作 | 执行器 | 前置检查 | 执行后检查 |
| --- | --- | --- | --- |
| start_service：启动已停止服务 | docker_lifecycle_cli / docker_lifecycle_http | 精确容器标识、已停止且未暂停、已配置健康检查 | 启动时间改变、运行且未暂停、健康检查通过 |
| resume_service：恢复已暂停服务 | docker_lifecycle_cli / docker_lifecycle_http | 精确容器标识、运行且暂停、已配置健康检查 | 保留原启动时间、运行且未暂停、健康检查通过 |
| reload_config：热重载配置 | application_http | 登记版本可用、应用校验有效、尚未加载 | 加载版本与登记一致、操作编号匹配、状态版本递增 |
| resume_consumer：恢复暂停消费 | application_http | 指定消费者已暂停，应用允许恢复 | 消费不再暂停、操作编号匹配、状态版本递增 |
| refresh_cache：刷新指定缓存组 | application_http | 指定缓存组允许刷新、存在有效代次 | 缓存就绪、代次增加、操作编号匹配、状态版本递增 |

全部真实操作还需要登记的业务症状指标通过。平台状态成功不等于业务恢复，验证失败或结果不确定转人工处理，不自动重试写操作或执行反向操作。原 docker_cli / docker_http 仍只支持重启。

风险等级是本项目的相对提示，不是跨业务保证。恢复暂停通常比重启更少干扰进程，但可能恢复写入或积压处理。启动、重载配置、恢复消费和刷新缓存按中风险提示；五种新操作均强制填写适用条件、风险与预期验证。人工须核实计划维护、幂等性、容量和配置版本等业务前提。

## 配置

完整示例见 [services.maintenance.example.json](services.maintenance.example.json)。示例中的容器 ID、身份、观测地址、控制目标和指标均须替换；它不会覆盖本地登记。使用已有离线校验入口：

```powershell
.\.venv\Scripts\python.exe scripts/register_service.py check --file examples/services.maintenance.example.json --json
```

Docker 目标固定容器完整 ID 和 context／控制地址，模型不能提供容器名、命令或 Shell 参数。CLI 使用固定参数且 shell=False；写操作前再次检查状态。Docker 状态检查与写入之间仍可能发生外部并发变化，需要避免多个工具同时维护同一容器。

应用目标单独登记一个动作、固定 control_target 和 control_scope。热重载额外固定 desired_config_version，该版本显示在前端并保存进审批方案摘要。变更配置会使原审批失效。新增动作的 parameters 必须为空，模型不能选择地址、缓存键、消费组或版本。

## 应用控制接口约定

三个应用动作已实现 HTTP 执行适配；**目标应用或其管理服务需要提供以下接口**。本项目没有替目标应用实现配置加载、消息消费或缓存存储逻辑；缺少接口时操作会失败或被阻止。

地址来自登记，认证使用 authorization_env 指定的环境变量完整值，例如 Bearer 令牌。非本机地址要求 HTTPS；请求不跟随重定向。接口应使用独立、仅能操作登记目标的运维身份。

读取：

```http
GET /ops/targets/{control_target}
```

配置目标的状态示例：

```json
{
  "target_id": "api-config",
  "scope": "maintenance_demo-example-api-staging",
  "kind": "config",
  "supported_actions": ["reload_config"],
  "revision": 7,
  "healthy": false,
  "config_valid": true,
  "available_config_version": "approved-v2",
  "loaded_config_version": "approved-v1"
}
```

消费目标 kind 为 consumer，提供 consumer_paused 和 resume_allowed 布尔字段。缓存目标 kind 为 cache，提供 refresh_allowed、cache_ready 布尔字段和非负整数 cache_generation。每个目标都有正整数 revision 和布尔 healthy；未声明支持的动作拒绝执行。状态接口不得把其他目标重新标记成当前目标。

执行：

```http
POST /ops/targets/{control_target}/actions/{action}
```

```json
{
  "operation_id": "服务端为本次操作生成的UUID",
  "expected_revision": 7,
  "config_version": "approved-v2"
}
```

config_version 只随热重载请求发送，来自登记配置。操作编号由本项目生成，并在发送写请求前保存进执行事件；响应丢失后可以用该编号人工核实结果。expected_revision 来自执行预检。应用必须在一次原子状态变更中检查 revision；不匹配时拒绝写入，并按 operation_id 去重。单纯在客户端先读后写不足以解决并发冲突。

成功受理回执：

```json
{
  "accepted": true,
  "operation_id": "请求中的同一个UUID",
  "target_id": "api-config",
  "scope": "maintenance_demo-example-api-staging"
}
```

执行生效后，状态接口必须返回递增 revision、相同 last_operation_id 和实际目标状态；满足恢复条件时 healthy 为 true。只返回 accepted 不算恢复成功。若期间另一次操作覆盖了 last_operation_id，本次无法确认，保留为人工处理。

缓存组由管理接口映射到固定业务范围；刷新逻辑应重建或刷新该组，接口不得实现全库清理、任意键匹配或任意脚本执行。消费者恢复应由应用检查业务可恢复条件；配置加载由应用先校验对应版本再生效。

## 免费验证

```powershell
$env:PYTHONPATH = "app;."
.\.venv\Scripts\python.exe -m unittest tests.workflow.test_maintenance_executors -v
```

使用真实执行器和修复服务，HTTP、CLI 与业务指标响应为测试桩。覆盖五种动作的审批及单次执行、状态变化、错误范围、固定命令、配置版本绑定、操作编号、缓存代次及业务恢复检查。不会操作真实容器、刷新缓存、恢复消费或调用模型。此验证不替代目标应用的联调与业务验收。

Docker 动作语义依据：[启动已停止容器](https://docs.docker.com/reference/cli/docker/container/start/)、[恢复暂停进程](https://docs.docker.com/reference/cli/docker/container/unpause/)。
