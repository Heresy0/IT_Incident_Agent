# IT 工单评估

新增[定向真实模型展示集](showcase_live/README.md)，与原独立集、专项集和脚本演示分开：首批show_001/show_002及一次工单修订show_101共6条真实运行、47模型/16合成只读工具，所有结果保留。当前只取得独立复核拒绝不充分候选的片段，没有完整诊断质量优势。[结果与面试讲解](../examples/showcase/curated-live-guide.md)记录完整边界。`scripts/compare_showcase_live.py`、`scripts/compare_showcase_revision.py`默认免费计划，`scripts/evaluate_showcase_live.py`只离线核验已保存报告。

当前评估入口是 evals/incident，真值与 Agent 可见 fixtures/incident 分离。评分、结构检查和同预算单/多对照都不调用模型；真实运行必须通过脚本的显式 live 入口和预算。

```powershell
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m evals.incident.check_fixtures
.\.venv\Scripts\python.exe scripts/compare_incident.py --fake --output output/incident-comparison
```

公共报告读写辅助在 evals/common.py，应用指纹在 app/runtime/version.py；不再导入旧 evals.run。脚本模式始终 excluded_scripted，不计业务质量。历史失败、partial、人工待确认结果保留。

旧研究评估代码在 archive/research/evals；evals/baselines、docs/evaluations 和旧验收报告仅为历史记录，不参与当前CI，也不是IT系统成绩。

新增[独立合成测试集](independent/README.md)：10个新事件，6例验证、4例默认保留。工单/观测与答案隔离、版本哈希冻结，统一按结论、证据推理、不确定性、建议与交接人工评分，允许不同合理取证路径。新入口 `scripts/compare_independent.py` 默认免费计划，`--check`离线预检，`--fake`只验证控制行为；真实调用需显式案例、预算和执行，单批最多2例。本次没有真实模型成绩，不改写旧评测。

新增[多Agent专项能力测试集](targeted/README.md)：历史误导、范围冲突、区分性取证各两个不同事件，加两例普通对照；5例开发/普通、3例默认保留。复用现有单/多链路与执行器，完整保留假设状态、建议条件和待确认项评分，专项与普通结果分开，支持同版本有限批次离线汇总。`scripts/compare_targeted.py`默认免费计划；未进行付费验证，不预设多Agent胜出。
