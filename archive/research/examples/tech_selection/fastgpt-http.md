# FastGPT HTTP 请求能力资料笔记

来源：[官方 HTTP Request 文档](https://doc.fastgpt.io/en/guide/build/workflow/nodes/http)。核对日期：2026-10-07；适用发布版本待核对。

这是人工整理的摘要，不是原文或实际连接结果。文档介绍 HTTP 节点向指定 URL 发请求，可配置 Params、Body、Headers，使用变量，解析响应数据供下游节点使用。

这能支持存在 HTTP 集成能力的结论，不能保证实际企业内部接口可达；网络、认证、版本兼容和部署条件需要在 PoC 中验证。该笔记没有总成本和吞吐量实测。
