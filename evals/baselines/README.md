# 首轮诊断基线

`qwen-turbo-v2/` 保留 20 个案例各两次的原始结果、AI 辅助语义复核、待填人工复核模板及两次小型真实检索基准。内容只包含公开虚构资料、虚构记忆和安全的模型参数，不含 API Key、真实用户记录或生产知识库数据。

这是**未达到质量门禁的现状基线**：语义通过率 26/40，不能当作验收通过版本。人工复核模板全部 pending；AI 复核明确标为 assistant。历史原始 JSON 不重写，后续运行保存在 `output/` 新目录。

免费重建首轮报告：

```powershell
.\.venv\Scripts\python.exe -m evals.report evals/baselines/qwen-turbo-v2/workflow.json --reviews evals/baselines/qwen-turbo-v2/assistant-reviews.json --output output/baseline-report.md --check-gates
```

预期退出码为 2，表示原始模型质量未达初始阈值，而不是报告生成失败。详细范围、评分口径和实际薄弱点见 `docs/评估体系说明.md`。

版本比较校验原始资料/案例文件字节哈希、评分器、重复次数与样本集合；文件或标注改变则需要另立基线。首轮在记录完整工作流代码哈希和结构化节点终止原因前启动，因此这两个新字段未补写入原始结果。生产工作流整个实测期间未改动，对应提交 `8121dd4`；终止说明仍可在最终报告中复查。新评测器已记录代码哈希和节点终止原因，首轮缺失项明确显示未记录，不从文本推造指标。
