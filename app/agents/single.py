"""Six-tool SingleAgent baseline: same loop, quotas and diagnosis reference expansion."""
from agents.investigation import investigate, INCIDENT_LIMITS
from agents.contracts import DiagnosisSelection
from evidence.diagnosis import expand_diagnosis
from agents.investigation_prompt import SYSTEM
from evidence.ownership import parse_owner_selection

SINGLE_SYSTEM = SYSTEM.split("示例结构：")[0].replace(
    "你有四个观测工具；知识检索由 Knowledge 角色负责。",
    "你拥有与多 Agent 工作流相同的六个只读工具，包括操作手册和已确认历史工单检索。")
SINGLE_SYSTEM = SINGLE_SYSTEM.replace("最多四个模型步骤，包括最终输出。",
    "整个运行使用提供的 step_limit，最终输出和输出修正也计入步数。")
SINGLE_SYSTEM = SINGLE_SYSTEM.replace("tentative_hypotheses", "hypotheses") + """
你独立负责取证和诊断，没有独立的 Reviewer 或 Supervisor。
最终答案使用提供的 DiagnosisSelection Schema，不得以工具响应作为最终答案。
区分事实观测与假设；为每个原因引用 support_refs 和 counter_refs。
历史工单和操作手册只是线索，不是当前事实观测。findings 必须有当前观测支持。
说明 pending_checks 和 missing_information。recommended_actions 必须包含适用条件、
预期结果、风险以及是否需要人工审批。不得执行修复操作。
所有假设在独立核查前都保持待验证，不得声称故障已解决或根因已确认。
根据实际现象和结果选择工具。只返回 JSON，使用简短的公开决策说明。
"""


def resolve_diagnosis(content, executor, allowed_kinds=None):
    selection = parse_owner_selection(DiagnosisSelection, content, executor)
    return expand_diagnosis(selection, executor), "reference_selection_v2"


def single_agent(model, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None, event_log=None, executor=None):
    result = investigate(model, provider, principal, limits=limits, max_steps=min(16, limits.model_calls),
        role="single", output_schema=DiagnosisSelection, output_resolver=resolve_diagnosis,
        system_prompt=SINGLE_SYSTEM, emit=emit, context=context, event_log=event_log, executor=executor)
    output = result["output"]
    purpose = provider.ticket().purpose
    used = {r["evidence_id"] for f in output["findings"] for r in f["refs"]}
    used.update(r["evidence_id"] for h in output["hypotheses"] for r in [*h["support_refs"], *h["counter_refs"]])
    result.update(task_type="incident_single", used_evidence_ids=sorted(used),
        purpose=purpose,
        business_result=("status_checked" if purpose == 'status_check' else "diagnosis_available")
            if result["status"] == "completed" else "needs_information",
        task_results=[], drafts=[output], reviews=[], negotiation=[], rework_rounds=0)
    return result
