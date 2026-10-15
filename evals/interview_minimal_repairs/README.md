# 标准流程最小修复验证

此为历史冻结协议。复现下述命令应使用冻结提交 `7f16252` 并提供协议列出的原始运行记录；当前技术文档已整理，不按当前 `main` 的文档路径重新解释或重写冻结哈希。

第一批应用提交 `97ef0c2`、指纹 `9581bf486e6dc735`。五个局部文件补充引用错误路径、程序任务查询标识、阻塞规则输入和一次完整错误定位；没有新角色、状态机、引用传输或交叉条件schema。

三个固定已见合成案例各自主运行一次，沿用标准执行器和付费记录器；默认2200输出token／30秒、temperature0、SDK零重试，输入48000，16模型／8工具／180秒，预留3模型／20秒。没有草稿、反馈预置或回放，不在批次内改代码。

```powershell
.\.venv\Scripts\python.exe scripts/verify_interview_repairs.py
.\.venv\Scripts\python.exe scripts/verify_interview_repairs.py --execute-live
```

计划最多48次新增请求，实际18次；此前190次，累计208次。首批业务重复场景仍失败，全部原始记录见[首批档案](../../examples/showcase/interview-minimal-repairs-validation.json)。后续修改使用独立提交和冻结，不能覆盖本批失败或把后续结果算作本批通过。
