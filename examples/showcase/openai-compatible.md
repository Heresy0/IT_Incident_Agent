# OpenAI 兼容调用入口

2026-10-12，按用户要求参考 Enterprise_knowladge 项目的 `ChatOpenAI + base_url` 接入方式，将当前模型调用由 `ChatTongyi + MultiModalConversation` 改为 `ChatOpenAI + Chat Completions`。不新增代理服务器，也不冒充模型身份；请求仍发往配置指定的服务。

统一配置读取仍使用项目 `.env`，进程变量优先，旧密钥变量保持兼容：

```dotenv
MODEL=qwen3.7-plus
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# MODEL_API_KEY=your-provider-key
# 未设置 MODEL_API_KEY 则读取原 DASHSCOPE_API_KEY。
MODEL_EXTRA_BODY={"enable_thinking":false}
```

同一服务上换兼容模型只需改 MODEL。跨服务换 MODEL_BASE_URL 和 MODEL_API_KEY，按厂商支持调整 MODEL_EXTRA_BODY；不支持 enable_thinking 的服务用 `{}`。目标须支持 function tools 和 JSON object 格式。本项目使用 Chat Completions，不自动切换 Responses。厂商差异不能仅凭统一协议完全消除。

模型端 JSON object 只约束 JSON 格式，具体合同与引用仍由程序验证。[OpenAI 接口约定](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)与[阿里云兼容调用文档](https://www.alibabacloud.com/help/en/model-studio/text-generation)支持这一接入方式。

原预算、角色共享方式、提示词、补查与审批逻辑保持不变。单次请求 30 秒、温度 0、输出上限 2200；OpenAI SDK 的 max_retries=0 对应一次请求，替代旧 Tenacity stop_after_attempt(1) 的同等行为。JSON 格式经 extra_body 传递，避免当前 LangChain 版本进入 SDK 的严格工具自动解析分支；线上 HTTP 请求仍包含标准 response_format 字段。

新增 langchain-openai 1.0.3 及其锁定依赖，保留原 langchain-core 1.0.5，依赖检查通过。HTTP 边界模拟验证工具调用、工具结果消息、JSON 输出、usage 计数、同预算纠正、超时不隐藏重试及敏感错误隔离。模型名不会选择不同厂商 SDK 路径。

完整免费回归：437 项，7 项数据库集成跳过，62.713 秒；随后补充余额错误分类测试与更新当前对照脚本元数据，65 项相关测试全部通过，7.729 秒。最终应用指纹 `9d59922278d78522`。真实接口检查先连接失败，后请求到达服务并返回 HTTP 400 / Arrearage；未取得模型输出、未完成真实工具往返。原失败记录保留于 output/openai-compatible-smoke.json。当前余额/额度问题不证明协议成功或失败，也不作为多 Agent 推理质量判定。

旧冻结评估指纹属于历史版本，不因迁移覆盖其报告或修改冻结哈希。后续真实评估必须记录新应用指纹。

用户随后充值并切换至 Qwen3.7-flash，[真实工具往返与 JSON 验证已通过](changed-model-verification.md)；代表性诊断案例仍因引用与重复调度问题未完成。该结果单独保留，不覆盖此前 Arrearage 记录。
