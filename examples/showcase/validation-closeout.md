# 标准流程验证与案例复现

原始JSON、数据集和冻结协议保留原标识符、路径与哈希。历史复现使用对应冻结提交，当前说明文档只作技术分析，不覆盖或重算旧结果。

执行主线是三个模型角色：Supervisor规划、Diagnosis诊断、Reviewer核验；Investigation与Knowledge按计划执行受限只读取证。架构见[当前说明](system-architecture.md)，不要把旧五模型角色演示当作当前默认。

## 人工评估的四个问题

这些是阅读报告的评估标准，不是新的运行时规则、Schema或自动评分器。引用合法和业务语义成立分开判断。

| 问题 | 应检查的内容 | 不足时如何表述 |
| --- | --- | --- |
| 业务现象成立吗？ | 记录是否能关联业务对象、操作、时间和实际结果；例如同一订单确有两次成功处理，而非只有两次请求 | 延迟、错误率和负载只支持技术异常；用户描述保留为待核实，不能据此确认业务重复 |
| 因果方向成立吗？ | 候选机制如何产生该结果，中间环节是否有记录支持 | 幂等键碰撞可能导致错误去重或漏处理，不能直接解释重复处理；缺环节就保留候选与缺口 |
| 反证针对原命题吗？ | 同一对象、时间范围、谓词及肯定／否定方向；是否把“没有支持”升级成直接矛盾 | 部署完成时间晚于错误不证明部署开始／生效时间；反驳“发布导致错误”不能写成反驳“发布并非原因” |
| 交接能执行吗？ | 人工应取得什么记录、从哪个实际可用渠道取得，以及结果怎样改变判断 | 例如关联订单／请求、幂等键、消费尝试和成功结果，区分重试、重复投递与重复生效；渠道未登记就明确人工核查，不能虚构工具或负责人 |

近端机制可以有依据，触发来源仍未知。有效人工核查待办不要求预先取得结果；核查成功也不等于服务已恢复。

## 两个固定验证案例

这里选择既有、完整留档的两次真实模型运行用于报告展示，不重新抽取通过样本计算准确率，也不重跑付费请求。两次均使用合成只读工具数据。

**机制案例：只读写入。** 来源为[首批最小修复档案](interview-minimal-repairs-validation.json)的closeout_002，4次真实请求、2次工具执行，completed／passed。展示EROFS与进程命名空间内挂载ro如何支持近端写入受阻，再展示为何变成只读仍待人工核实。原报告对竞争解释的排除偏强，部署完成时间不能证明开始／生效时间；该限制需与运行结果一并解释。它展示诊断流程与机制依据，不代表整份报告语义全对或服务已恢复。

**交接案例：业务重复。** 来源为[补充复验档案](interview-blocker-followup-validation.json)的closeout_003，6次真实请求、3次工具执行，partial／needs_information，终止NEEDS_INFORMATION。展示原始查询、复核缺口和人工待办；程序允许保留未知并结束。原报告N1仍用技术指标支持业务重复，H2因果方向也有问题，因此交接有效与诊断准确分开讲。这是有缺陷的真实结果，不将其美化成理想交接。

### 离线复现报告展示

在仓库根目录运行下面整段PowerShell。只读取已提交档案，调用已有渲染器，写入新的UUID目录；无需模型Key、网络或数据库。它复现保存结果的展示，不复现模型自主推理。

```powershell
$env:PYTHONPATH='app;.'
$env:PYTHONIOENCODING='utf-8'
@'
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from evidence.render import render

root = Path.cwd()
sources = [
    ('mechanism', 'interview-minimal-repairs-validation.json',
     'c3daf7dec0ed534fe35f8a0d559983917defd6432bd70f3ab1e02a8ade945704'),
    ('handoff', 'interview-blocker-followup-validation.json',
     '593d2e2e3a208821cb3d2547e99279e09bf88dddae9268a45e7a593f177265b7')
]
folder = root / 'output/standard-flow-display' / str(uuid4())
folder.mkdir(parents=True, exist_ok=False)
for name, filename, expected in sources:
    path = root / 'examples/showcase' / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('展示档案字节与固定来源不一致')
    archive = json.loads(path.read_text(encoding='utf-8'))
    row = (next(r for r in archive['runs'] if r['case_id'] == 'closeout_002')
           if name == 'mechanism' else archive['run'])
    report = row['raw_report']
    (folder / (name + '.md')).write_text(render(report), encoding='utf-8')
    (folder / (name + '.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(name, report['status'], report['review_status'])
print(folder.resolve())
'@ | .\.venv\Scripts\python.exe -
```

需要现场运行实际标准图时，沿用免费命令：

```powershell
.\.venv\Scripts\python.exe scripts/demo_standard_flow.py
```

该命令使用脚本模型与合成工具，验证程序取证、引用、预算及报告链路；不是一次新的自主诊断。两个真实案例的输入、响应、原始报告和失败记录在上述档案中保留。

## 本轮限定试验结论

**候选提示不保留，恢复原提示并停止优化。** 运行时代码在验证期间保持不变，只在验证进程中对Diagnosis和Reviewer追加同样的两句提醒。原提示与候选各运行两个已见案例一次，无草稿／反馈预置、无模型回放、无运行后重试。

| 案例 | 原提示 | 候选提示 | 阅读结果 |
| --- | --- | --- | --- |
| 只读写入 | 4请求、2工具，completed／passed | 4请求、2工具，completed／passed | 候选H1更聚焦只读机制，但H3草稿“新版本并非直接原因”被Reviewer按相反的肯定命题反驳，发布为refuted；部署时序仍被过度解释 |
| 业务重复 | 9请求、3工具，partial／needs_information | 7请求、3工具，partial／needs_information | 候选N1正确保留为pending，H2描述幂等失效后的重复接受，方向较合理；但最终建议退回“日志恢复后重试”已失败查询，缺少明确的业务关联核查；两种均追加补查预算拒绝、终止REVIEW_INCOMPLETE |

候选业务例少了两次纠正，局部成本较低，不足以证明稳定收益。原提示该例保留两次失败：Diagnosis空事实引用、Reviewer未知EV编号，随后各按既有一次额度纠正成功；不继续针对此次重新出现的错误增加规则。

本批24请求、技术校验失败2次均来自原提示业务例；输入126921token、输出17367token，共144288token，费用未估算。没有网络错误、输出截断或输入门禁拒绝，但两个业务例追加补查均被预算准入拒绝。原提示合计13请求、79472token，候选11请求、64816token；每例各一次且轨迹不同，不能据此宣称降低成本或提升准确率。相关累计238次请求，低于既有336上限。

记录脚本首个报告保存后误读停止原因字段，汇总中断。保留原脚本与失败日志，改为termination_reason后只续跑未执行的三次；首个报告和四次请求原样沿用，没有重跑付费案例。这个修复仅涉及本地验证记录脚本，不进入运行时。

原始计划、完整消息／响应、报告、两次校验失败、预算拒绝与两版记录脚本见[完整收尾档案](interview-closeout-validation.json)。小样本无法确认副作用由提醒造成；这里采用保守保留标准，没有足够清晰的整体收益就不进入默认版本。

本轮免费标准图排练完成，4次脚本模型步骤、2次工具执行、0次取证模型调用、0次付费请求；两个历史报告离线展示配方已实际运行。运行时代码指纹仍为fa6e3b3b76224064，本轮未修改app或测试，沿用上一轮701项回归（681通过、20项数据库跳过），不把它标成新增测试成绩。对2699份旧输出与验证档案的字节检查通过，原实验分支保留。

本次验证已经结束。后续只有新增真实业务记录、可复现的确定性缺陷或明确改变业务需求时，才重新评估修改；不以偶发模型失败启动新一轮编排扩展。
