# 服务基础取证包独立实验

本实验只改变取证入口：在原 Supervisor 前增加一个显式可选的程序取证节点。未传入 `diagnostic_pack` 时仍构建原图；HTTP 后端、普通 CLI、服务登记文件和默认配置不会加载该功能。诊断、Reviewer、输出契约、纠正额度、修复审批和总预算继续使用原实现。

## 配置与边界

注册格式见 `examples/diagnostic-packs.example.json`，示例只针对公开合成服务，不会读取或覆盖 `services.local.json`。每个包匹配精确 service/environment/service_version，最多两个基础检查，每次最多三个指标，只支持已登记指标、无过滤日志和变更查询。时间窗口由服务器工单填入，包不能提供 start/end、日志级别、错误码、HTTP地址、凭据、任意脚本或预期根因。

程序在第一项读取前校验全部参数。沿用身份、能力、时间范围、证据和引用注册、查询去重、12000字符结果边界、全局预算与原传输重试限制。基础包占一个原任务槽，不增加模型或工具额度，也不调用独立取证模型。每个运行只执行一次，已失败、空查或截断的相同查询不会因包而重读。

`diagnostic_pack.checks` 分开保存 satisfied、empty、failed、truncated、missing_samples、unavailable、not_executed。非 satisfied 项生成具体人工取证待办；渠道失败和空结果不能证明健康。goal_verified 和 mechanism_confirmed 固定为 false，所有来源仍由原 Diagnosis/Reviewer 判断。状态检查跳过机制包；无匹配服务不会套用其他服务的包。

基础包整体超出剩余预算、时间或收尾额度时立即带缺口退出。**该策略有代价：小预算下可能失去原流程尚能取得的部分观测，尚不适合直接设为默认。**配置错误拒绝执行，不以失败重试掩盖。

## 免费对照

```powershell
# 默认只显示计划；不读取模型Key、真实服务登记、答案或评价标准。
.\.venv\Scripts\python.exe scripts/compare_diagnostic_packs.py
# 显式运行两个分支的合成机械控制，没有付费或真实服务调用。
.\.venv\Scripts\python.exe scripts/compare_diagnostic_packs.py --execute-control
```

可用 `--cases connection_wait authentication source_unavailable` 缩小批次，`--output` 指定输出根目录。每次创建新批次，先保存计划和数据快照，逐条保存运行，再生成 JSON/Markdown 汇总和报告哈希。两分支使用相同服务数据、预算和脚本规划策略；能复用已取得查询，实验在语义判断前停止，必要时用固定空诊断完成图收尾。所有质量结果标记 excluded_scripted。

11个控制场景覆盖连接等待、认证、渠道失败、机制改变、服务/指标改名、状态检查、小预算、空日志、长记录、密集日志和未登记服务。基础样本来自已有冻结开发场景；衍生扰动由同一实现者设计，不是独立外部或保留集测试。执行模块没有案例编号、错误码到根因规则，也不读取参考答案。

公共控制摘要见 `examples/showcase/diagnostic-pack-control.json`。它描述哪些基础查询被实际执行、失败或截断、额外工具调用、输入长度与保留的交接信息；不报告诊断正确率或多 Agent 优势。

## 当前取舍

- 连接等待、不同机制、改名登记三个控制场景补齐资源日志与压力对照，各增加两次工具调用。
- 认证服务复用原查询，无连接池指标和额外工具调用；状态检查和无登记服务维持原查询范围。
- 渠道失败只尝试一次，带人工核查缺口；完整空结果和采样截断保持限制。
- 小预算分支整体推迟，两次基础检查均未执行；原分支还能读取部分指标。需要后续单独比较“按预算执行部分基础检查”策略，不在本轮混入改动。
- 密集日志的规划输入接近28000字符上限。当前脚本控制未触发门禁，但真实 Diagnosis/Reviewer 输入可能不同；尚未证明端到端交付稳定。

应用源码指纹因可选模块改变，旧冻结脚本会按原规则拒绝使用新源码冒充旧版本；没有更新旧 freeze.json 或历史成绩。对照恢复点为统计改动提交 `0d8eb37`。本实验只为后续决策提供控制证据，不自动启用生产功能，也没有增加付费模型验证。

## 显式接入实验

Python 调用 `collaborate(..., diagnostic_pack=validated_pack)` 可启用；或向 `Collaboration` 传入同名参数。需要 programmatic 取证模式。诊断包必须由服务维护人员在模型运行前登记，不由模型生成，不依据评估答案选择。配置摘要和指纹进入报告的 collection_execution，用于与原流程分别统计。
