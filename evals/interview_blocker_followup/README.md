# supported缺引用降级反馈的一次补充验证

应用提交 `e6551ec`、指纹 `fa6e3b3b76224064`。首批失败显示N1原稿为supported但没有观测引用，既有门禁降级uncertain后才要求blocker；原反馈遗漏该降级过程。本次仅补充输入规则及错误中的submitted／effective判定、引用路径和原因，保持原支持门禁。

业务重复案例固定运行一次，无预置草稿／反馈，无回放。沿用标准执行器、2200／30秒及原额度；最多16次请求，此前累计208次，本次最多累计224次，仍低于336上限。

```powershell
.\.venv\Scripts\python.exe scripts/verify_interview_blocker_followup.py
.\.venv\Scripts\python.exe scripts/verify_interview_blocker_followup.py --execute-live
```

实际6次请求，无校验失败，最终有效复核后缺信息交接。N1首次已填引用，故本次没有触发support_gate纠正分支；该分支通过定向回归验证。技术指标仍不能证明业务重复，详见[完整记录与局限](../../examples/showcase/interview-blocker-followup-validation.json)。没有继续重跑至通过。
