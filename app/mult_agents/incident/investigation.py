import json
import hashlib
import time
from uuid import uuid4
from pydantic import ValidationError
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from mult_agents.harness.runtime import RunContext, Limits, ExecutionError, activate, invoke_chat_model
from .contracts import InvestigationOutput, InvestigationSelection, Finding, INVESTIGATION_TOOLS
from .tools import ToolExecutor
from .prompts import SYSTEM

INCIDENT_LIMITS = Limits(reserve_model_calls=3, reserve_seconds=20)


class ReferenceValidationError(ValueError):
    """Only fixed reasons and server-generated paths; never rejected model values."""
    def __init__(self, reason, path):
        self.reason, self.path = reason, path
        super().__init__(reason)


def validation_details(exc):
    if isinstance(exc, json.JSONDecodeError):
        return {"reason": "invalid_json", "details": []}
    if isinstance(exc, ValidationError):
        fields = {"findings", "statement", "refs", "evidence_id", "field_path", "value", "unit",
                  "observed_at", "data_version", "quote", "reference_id", "tentative_hypotheses",
                  "missing_information", "escalation_team"}
        errors = exc.errors(include_input=False, include_context=False, include_url=False)[:5]
        return {"reason": "invalid_json" if any(e["type"] == "json_invalid" for e in errors) else "schema_mismatch",
                "details": [{"path": ".".join(str(p) if isinstance(p, int) or p in fields else "<extra>" for p in e["loc"]),
                             "type": e["type"]} for e in errors]}
    if isinstance(exc, ReferenceValidationError):
        return {"reason": "reference_mismatch", "details": [{"path": exc.path, "type": exc.reason}]}
    return {"reason": "invalid_output_type", "details": []}


def validate_output(output, evidence):
    for fi, finding in enumerate(output.findings):
        for ri, ref in enumerate(finding.refs):
            path = f"findings.{fi}.refs.{ri}"
            item = evidence.get(ref.evidence_id)
            if item is None:
                raise ReferenceValidationError("unknown_evidence", path + ".evidence_id")
            if item.kind != "observation":
                raise ReferenceValidationError("non_current_evidence", path + ".evidence_id")
            value = item.payload
            for part in ref.field_path.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ReferenceValidationError("unknown_field", path + ".field_path")
                value = value[part]
            if type(value) is not type(ref.value) or value != ref.value:
                raise ReferenceValidationError("value_mismatch", path + ".value")
            for field, matches in (("unit", ref.unit == item.payload.get("unit", "")),
                    ("observed_at", ref.observed_at == item.observed_from),
                    ("data_version", ref.data_version == item.data_version),
                    ("quote", ref.quote in item.excerpt)):
                if not matches:
                    raise ReferenceValidationError(field + "_mismatch", path + "." + field)
        if not any(ref.field_path in {"value", "message", "error_code", "summary", "team", "escalation"}
                   or ref.field_path.startswith("config_summary.") for ref in finding.refs):
            raise ReferenceValidationError("metadata_only_reference", f"findings.{fi}.refs")
    teams = {item.payload["team"] for item in evidence.values() if "team" in item.payload}
    if output.escalation_team is not None and output.escalation_team not in teams:
        raise ReferenceValidationError("unobserved_team", "escalation_team")


def resolve_output(content, executor):
    data = json.loads(content)
    # Explicit compatibility for literal refs from old offline fixtures. Still strictly checked.
    # The schema and prompts supplied to new models contain only reference_id selectors.
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        legacy = any(isinstance(f, dict) and isinstance(f.get("refs"), list)
                     and any(isinstance(r, dict) and "evidence_id" in r for r in f["refs"])
                     for f in data["findings"])
        if legacy:
            output = InvestigationOutput.model_validate_json(content)
            validate_output(output, executor.evidence)
            return output, "literal_refs_v1"
    selection = InvestigationSelection.model_validate_json(content)
    findings = []
    for fi, finding in enumerate(selection.findings):
        refs, seen = [], set()
        for ri, selected in enumerate(finding.refs):
            path = f"findings.{fi}.refs.{ri}.reference_id"
            ref = executor.references.get(selected.reference_id)
            if ref is None:
                raise ReferenceValidationError("unknown_reference", path)
            if selected.reference_id in seen:
                raise ReferenceValidationError("duplicate_reference", path)
            seen.add(selected.reference_id)
            refs.append(ref.model_copy(deep=True))
        findings.append(Finding(statement=finding.statement, refs=refs))
    output = InvestigationOutput(findings=findings, tentative_hypotheses=list(selection.tentative_hypotheses),
        missing_information=list(selection.missing_information), escalation_team=selection.escalation_team)
    validate_output(output, executor.evidence)
    return output, "reference_selection_v2"


def investigate(model, provider, principal, *, limits=INCIDENT_LIMITS, max_steps=4, emit=None):
    if not 1 <= max_steps <= 4:
        raise ValueError("max_steps must be 1..4")
    events = []
    task_span = ""
    def on_event(event):
        nonlocal task_span
        if event.get("type") == "call_start" and event.get("kind") == "task":
            task_span = event["span_id"]
        event = {**event, "seq": len(events) + 1}
        events.append(event)
        if emit:
            emit(event)
    ticket = provider.ticket()
    context = RunContext(limits=limits, emit=on_event, scope=ticket.scope)
    executor = ToolExecutor(provider, principal, ticket.scope, context)
    model = model.bind_tools(executor.schemas())
    task_id = str(uuid4())
    def event(kind, **fields):
        context.emit({"type": kind, "role": "investigation", "task_id": task_id,
                      "span_id": task_span, "parent_span_id": context.root_span, **fields})
    messages = [SystemMessage(content=SYSTEM), HumanMessage(content=json.dumps({
        "ticket": ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
        "output_schema": InvestigationSelection.model_json_schema(), "step_limit": max_steps}, ensure_ascii=False))]
    output, stop, repairs, call_ids = None, "", 0, set()
    failures = []
    output_protocol = "none"
    with activate(context), context.span("task", "investigation"):
        event("task_created")
        event("task_started")
        for step in range(max_steps):
            try:
                response = invoke_chat_model(model, messages, "investigation")
                # A blocking SDK call is not cancelled by an outer timer. Discard late responses.
                if time.monotonic() - context.started >= limits.seconds - limits.reserve_seconds:
                    stop = "TIME_BUDGET_EXCEEDED"
                    break
                if len(str(response.content)) > 12000:
                    stop = "MODEL_OUTPUT_INVALID"
                    break
                finish = getattr(response, "response_metadata", {}).get("finish_reason")
                event("model_output", step=step + 1, output_chars=len(str(response.content)),
                      finish_reason=finish if finish in {"stop", "length", "tool_calls"} else "unknown")
                messages.append(response)
                calls = response.tool_calls
                if getattr(response, "invalid_tool_calls", None) or len(calls) > 6:
                    stop = "MODEL_OUTPUT_INVALID"
                    break
                if calls:
                    for call in calls:
                        selected = call.get("name")
                        name = selected if selected in INVESTIGATION_TOOLS else "denied"
                        event("tool_selected", name=name, step=step + 1)
                    if step == max_steps - 1:
                        stop = "STEP_LIMIT"
                        break
                    for call in calls:
                        cid = call.get("id")
                        if not isinstance(cid, str) or not cid or len(cid) > 80 or cid in call_ids:
                            stop = "INVALID_TOOL_CALL_ID"
                            break
                        call_ids.add(cid)
                        if len(json.dumps(call["args"])) > 2000:
                            stop = "INVALID_ARGUMENTS"
                            break
                        result = executor.execute(call["name"], call["args"])
                        event("tool_result", name=call["name"] if call["name"] in INVESTIGATION_TOOLS else "denied",
                              status=result.status, evidence_ids=[e.evidence_id for e in result.evidence],
                              query_profile=dict(executor.last_query_profile),
                              truncated=result.truncated, error=result.error.model_dump() if result.error else None)
                        messages.append(ToolMessage(content=result.model_dump_json(), tool_call_id=cid))
                        if result.error and result.error.code == "BUDGET_EXCEEDED":
                            stop = "BUDGET_EXCEEDED"
                            break
                    if stop:
                        break
                    # Explicit final-step instruction counts as part of the normal conversation.
                    if step == max_steps - 2:
                        messages.append(HumanMessage(content="Next step is final: return the output JSON, no tools. State any gaps."))
                    continue
                try:
                    candidate, output_protocol = resolve_output(response.content, executor)
                    output = candidate
                    stop = "FINISHED"
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    failure = {"step": step + 1, **validation_details(exc)}
                    failures.append(failure)
                    event("validation_failure", node="investigation", **failure)
                    if repairs == 0 and step < max_steps - 1:
                        repairs += 1
                        event("validation_repair", node="investigation")
                        reference_options = {eid: [o.model_dump() for o in e.reference_options]
                                             for eid, e in executor.evidence.items()}
                        messages.append(HumanMessage(content="Output contract/reference check failed. Safe error details: "
                            + json.dumps(failure) + ". Available reference_options by evidence_id: "
                            + json.dumps(reference_options) + ". Return refs as [{\"reference_id\":\"an exact REF_ ID from the options\"}]. "
                            + "Do not copy field_path, value, unit, timestamp or quote; never prepend payload. "
                            + "The server expands these fields. Omit unverifiable findings. No additional tool calls required."))
                    else:
                        stop = "MODEL_OUTPUT_INVALID"
                        break
            except ExecutionError as exc:
                context.error(exc, "investigation")
                stop = exc.code
                break
        if not stop:
            stop = "STEP_LIMIT"
        context.stop_reason = stop
        if output is None:
            output = InvestigationOutput(missing_information=[f"Investigation stopped: {stop}; evidence requires human review."])
        status = "completed" if stop == "FINISHED" and output.findings else "partial" if executor.evidence or stop in {"FINISHED", "BUDGET_EXCEEDED", "STEP_LIMIT", "TIME_BUDGET_EXCEEDED"} else "failed"
        event("task_completed", status=status, reason=stop)
    return {"run_id": context.run_id, "task_type": "incident_investigation", "incident_id": ticket.incident_id,
            "scope": ticket.scope.model_dump(mode="json"), "data_source": provider.name,
            "prompt_version": hashlib.sha256((SYSTEM + json.dumps(InvestigationSelection.model_json_schema(), sort_keys=True)
                + json.dumps(executor.schemas(), sort_keys=True)).encode()).hexdigest()[:12],
            "output_protocol": output_protocol,
            "status": status, "review_status": "not_performed", "output": output.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in executor.evidence.values()],
            "events": events, "run_summary": context.summary(), "repairs": repairs,
            "validation_failures": failures}
