"""Six-tool SingleAgent baseline: same loop, quotas and diagnosis reference expansion."""
from .investigation import investigate, INCIDENT_LIMITS
from .collaboration_contracts import DiagnosisSelection
from .diagnosis import expand_diagnosis
from .prompts import SYSTEM

SINGLE_SYSTEM = SYSTEM.split("Example shape:")[0].replace(
    "You have four observation tools; knowledge lookup is reserved for the future Knowledge role.",
    "You have the same six read-only tools as the multi-agent workflow, including runbooks and confirmed history.")
SINGLE_SYSTEM = SINGLE_SYSTEM.replace("At most four model steps INCLUDING your final output.",
    "Use the supplied step_limit for this entire run, INCLUDING final output and repair.")
SINGLE_SYSTEM = SINGLE_SYSTEM.replace("tentative_hypotheses", "hypotheses") + """
You are a single investigator and diagnostician, with no independent Reviewer or Supervisor.
Use the supplied DiagnosisSelection schema for your final answer, not a tool response.
Separate factual findings from hypotheses; cite support_refs and counter_refs for each cause.
History/runbooks are clues, not current factual observations. Findings need current observations.
State pending checks and missing_information. Recommended actions must include conditions,
expected result, risk and whether human approval is required. Do not execute remediation.
All hypotheses remain tentative until independently checked. Never claim resolved or confirmed.
Choose tools from actual symptoms/results. Return JSON only and short public decisions.
"""


def resolve_diagnosis(content, executor, allowed_kinds=None):
    selection = DiagnosisSelection.model_validate_json(content)
    return expand_diagnosis(selection, executor), "reference_selection_v2"


def single_agent(model, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None, event_log=None, executor=None):
    result = investigate(model, provider, principal, limits=limits, max_steps=min(16, limits.model_calls),
        role="single", output_schema=DiagnosisSelection, output_resolver=resolve_diagnosis,
        system_prompt=SINGLE_SYSTEM, emit=emit, context=context, event_log=event_log, executor=executor)
    output = result["output"]
    used = {r["evidence_id"] for f in output["findings"] for r in f["refs"]}
    used.update(r["evidence_id"] for h in output["hypotheses"] for r in [*h["support_refs"], *h["counter_refs"]])
    result.update(task_type="incident_single", used_evidence_ids=sorted(used),
        business_result="diagnosis_available" if result["status"] == "completed" else "needs_information",
        task_results=[], drafts=[output], reviews=[], negotiation=[], rework_rounds=0)
    return result
