# 固定工作流演示与报告快照

以下是2026-10-07实际运行记录。报告原样保留，元数据见 `manifest.json`；完整事件和首次失败记录留在本机忽略提交的 `output/`。小样本只说明这些运行的结果，不代表平均性能或准确率。

## 推荐演示顺序

| 演示 | 快照 | 实际结果 | 核对要点 |
| --- | --- | --- | --- |
| 官方单项核实 | [official-docker.md](official-docker.md) | completed，原问题1/1，7次模型、2次真实博查，37秒 | 官方URL、搜索摘要范围、引用与覆盖；本轮只回放已有运行 |
| 本地部署对比 | [local-deploy.md](local-deploy.md) | completed，原问题1/1，7次模型、0次博查，约19秒 | 两份笔记依据，版本与适用范围不能扩大 |
| 成本未知 | [cost-gap.md](cost-gap.md) | partial，NO_EVIDENCE，3次模型、0次博查，约8.7秒 | 未编造月成本；没有接受结论，也没有假装已完成费用计算 |
| 模拟硬条件选型 | [hard-condition.md](hard-condition.md) | completed，固定资料q11复验 | A优先验证、B不满足私有部署；不是实际商业产品结论 |
| 模拟版本冲突 | [version-conflict.md](version-conflict.md) | partial，收尾状态修复前q15记录 | 接受当前C不支持OCR，保守拒绝了旧分支结论；仍需人工核对 |

`hard-condition-before.md` 保存q11首次结果：所有问题已覆盖，但一条额外费用结论被拒绝后被误标partial。修复后复验另存文件；两次模型输出不同，不能把状态变化或耗时差异全部归因于代码修复。控制回归以固定候选集证明误降级修复。

q15中Reviewer将“旧分支曾有预览能力”与“当前版本是否支持”混为检查目标，产生保守拒绝，并规划出不够必要的辅助问题。这是质量边界，不能称该案例全部通过。已接受事实未把旧分支能力拼接到当前版本。

## 只读回放，不产生新模型调用

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts/demo_research.py --run-id 98037b84-f055-4145-8130-58a243207c4b
.\.venv\Scripts\python.exe scripts/demo_research.py --run-id adc93781-157a-4d4b-92a9-da16472c09a5
.\.venv\Scripts\python.exe scripts/demo_research.py --run-id de98244c-6c58-4236-a3c3-ec6b66a619f1
```

上述ID需要本机原PostgreSQL数据存在。全新机器可直接阅读报告快照，或创建自己的运行；不要把历史ID当成随仓库携带的数据库记录。

脚本验证数据库结果、完整连续事件、SSE终态、回放前后序号及用量一致。前端默认代理地址5173，直连后端可加 `--base-url http://127.0.0.1:8002`。

## 创建新研究

```powershell
.\.venv\Scripts\python.exe scripts/demo_research.py --list
.\.venv\Scripts\python.exe scripts/demo_research.py --case local-deploy --live
.\.venv\Scripts\python.exe scripts/demo_research.py --case cost-gap --live
.\.venv\Scripts\python.exe scripts/demo_research.py --case official-docker --live
```

`--list`不发请求。没有`--live`会拒绝新建研究；显式创建会消耗外部API额度。两个本地案例需要先导入`examples/tech_selection`公开笔记，避免重复入库。它们关闭记忆与补搜，不发送博查请求，但Embedding和模型仍是外部服务。

每次新运行都保留独立ID和文件，不覆盖旧结果。网页搜索会变化、模型输出有随机性，因此新结果可能与快照不同。
