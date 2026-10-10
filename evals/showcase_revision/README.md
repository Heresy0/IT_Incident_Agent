# 一次工单修订的真实模型展示

`show_101` 来源于 `showcase_live` 的 `show_001`：观测与历史语义保持一致，只增加用户提供、仍需核对的 HTTP400 UNKNOWN_FIELD recipient_id 摘要，并将业务目标限定为只读诊断与一个具体核查下一步。两个链路收到相同信息。

修订在查看首批结果后完成，另行冻结版本、案例编号及答案，不覆盖原数据。只运行一对真实模型，4/3与13/3（模型/工具），单Agent completed、多Agent partial/needs_information。Reviewer确实没有为缺少直接业务错误记录支持的具体候选背书，但没有取得完整诊断优势；本批没有Knowledge调用或成功执行复核补查。

数据检查免费；真实运行仍需显式授权、案例与预算：

```powershell
.\.venv\Scripts\python.exe scripts/compare_showcase_revision.py --check
.\.venv\Scripts\python.exe scripts/compare_showcase_revision.py --live --execute-live --cases show_101 --model-budget 16 --tool-budget 8
```

详见[完整结果和面试讲解](../../examples/showcase/curated-live-guide.md)。本轮已停止追加调用，旧报告、失败结果和原测试集均保留。
