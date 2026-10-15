# 收敛版本验证协议

以 `fc01b2b` 为架构基线，当前应用冻结提交 `d3011c9`、指纹 `5c4abb5869e3d891`。仅保留三类通用修复：输入额度48000及有界环境配置、公开工具参数错误定位、可选模型超时（默认仍30秒）。不含后续实验修稿与Reviewer传输层。

三个固定已见合成案例各运行一次标准程序取证流程，无预置草稿、复核反馈或响应回放。所有角色使用默认2200输出token、30秒超时、temperature0、SDK零重试。每例16模型／8工具／180秒，预留3模型／20秒；输入48000字符。工具限制用于控制本轮成本，不改变业务默认工具额度。

此前相关批次实际176次付费请求，本轮最多48次，累计最多224次，低于既有336次上限。执行前后校验源码、数据、历史档案及驱动哈希；批次内不调参、不重跑至通过。保存完整模型输入、输出、结束原因、用量、校验失败及重复任务停止。

```powershell
.\.venv\Scripts\python.exe scripts/verify_interview_flow.py
.\.venv\Scripts\python.exe scripts/verify_interview_flow.py --execute-live
```

第一条仅展示计划；第二条使用已配置模型执行。结果见[收敛交付记录](../../examples/showcase/interview-consolidation.md)及[完整原始记录](../../examples/showcase/interview-consolidation-validation.json)。这三个已见案例不是独立盲测，不能计算泛化准确率。第二例发布记录仅表示完成时间，不能用于严格推断开始／生效时间。

`preservation.json`列出原实验分支、固定提交、44份新增历史资料哈希以及本地2607文件清单的摘要。原实验脚本需切回 `codex/review-driven-orchestration` 及相应冻结版本运行，保留的历史协议不表示当前主流程支持这些实验接口。
