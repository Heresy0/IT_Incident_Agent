# IT 工单评估

当前评估入口是 evals/incident，真值与 Agent 可见 fixtures/incident 分离。评分、结构检查和同预算单/多对照都不调用模型；真实运行必须通过脚本的显式 live 入口和预算。

```powershell
$env:PYTHONPATH='app'
.\.venv\Scripts\python.exe -m evals.incident.check_fixtures
.\.venv\Scripts\python.exe scripts/compare_incident.py --fake --output output/incident-comparison
```

公共报告读写辅助在 evals/common.py，应用指纹在 app/runtime/version.py；不再导入旧 evals.run。脚本模式始终 excluded_scripted，不计业务质量。历史失败、partial、人工待确认结果保留。

旧研究评估代码在 archive/research/evals；evals/baselines、docs/evaluations 和旧验收报告仅为历史记录，不参与当前CI，也不是IT系统成绩。
