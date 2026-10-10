import json
import hashlib
import time
from uuid import uuid4
from pydantic import ValidationError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from runtime.context import RunContext, Limits, ExecutionError, activate, invoke_chat_model
from evidence.contracts import InvestigationOutput, InvestigationSelection, Finding, INVESTIGATION_TOOLS, KNOWLEDGE_TOOLS, SINGLE_TOOLS
from tools.executor import ToolExecutor
from agents.investigation_prompt import SYSTEM
from evidence.observations import FACT_RENDERING, source_statement, pool_control_gaps, log_control_gaps, gap_message
from evidence.model_view import MODEL_VIEW_VERSION, result_view, fit_messages, substantive
from evidence.ownership import OwnerSelectionError, observed_owner_team, parse_owner_selection, resolve_owner, owner_options, owner_schema
from evidence.completion import diagnosis_gaps, required_sections

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
                  "missing_information", "escalation_team", "escalation_ref", "reference_id"}
        errors = exc.errors(include_input=False, include_context=False, include_url=False)[:5]
        return {"reason": "invalid_json" if any(e["type"] == "json_invalid" for e in errors) else "schema_mismatch",
                "details": [{"path": ".".join(str(p) if isinstance(p, int) or p in fields else "<extra>" for p in e["loc"]),
                             "type": e["type"]} for e in errors]}
    if isinstance(exc, (ReferenceValidationError, OwnerSelectionError)):
        return {"reason": "reference_mismatch", "details": [{"path": exc.path, "type": exc.reason}]}
    return {"reason": "invalid_output_type", "details": []}


def validate_output(output, evidence, allowed_kinds=("observation",)):
    for fi, finding in enumerate(output.findings):
        for ri, ref in enumerate(finding.refs):
            path = f"findings.{fi}.refs.{ri}"
            item = evidence.get(ref.evidence_id)
            if item is None:
                raise ReferenceValidationError("unknown_evidence", path + ".evidence_id")
            if item.kind not in allowed_kinds:
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
        if not any(ref.field_path in {"value", "message", "error_code", "summary", "team", "escalation", "text", "resolution"}
                   or ref.field_path.startswith("config_summary.") for ref in finding.refs):
            raise ReferenceValidationError("metadata_only_reference", f"findings.{fi}.refs")
    teams = {team for item in evidence.values() if (team := observed_owner_team(item)) is not None}
    if output.escalation_team is not None and output.escalation_team not in teams:
        raise ReferenceValidationError("unobserved_team", "escalation_team")


def resolve_output(content, executor, allowed_kinds=("observation",)):
    data = json.loads(content)
    # Explicit compatibility for literal refs from old offline fixtures. Still strictly checked.
    # The schema and prompts supplied to new models contain only reference_id selectors.
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        legacy = any(isinstance(f, dict) and isinstance(f.get("refs"), list)
                     and any(isinstance(r, dict) and "evidence_id" in r for r in f["refs"])
                     for f in data["findings"])
        if legacy:
            output = InvestigationOutput.model_validate_json(content)
            validate_output(output, executor.evidence, allowed_kinds)
            output = output.model_copy(update={'findings': [f.model_copy(update={
                'statement': source_statement(f.refs, executor.evidence)}) for f in output.findings]})
            return output, "literal_refs_v1"
    selection = parse_owner_selection(InvestigationSelection, content, executor)
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
        missing_information=list(selection.missing_information), escalation_team=resolve_owner(selection.escalation_ref, executor))
    validate_output(output, executor.evidence, allowed_kinds)
    output = output.model_copy(update={'findings': [f.model_copy(update={
        'statement': source_statement(f.refs, executor.evidence)}) for f in output.findings]})
    return output, "reference_selection_v2"


def investigate(model, provider, principal, *, limits=INCIDENT_LIMITS, max_steps=4, emit=None,
                context=None, event_log=None, executor=None, objective=None, role="investigation", repair_state=None,
                output_schema=InvestigationSelection, output_resolver=resolve_output, system_prompt=None,
                approved_checks=None):
    if not 1 <= max_steps <= (16 if role == "single" else 4):
        raise ValueError("max_steps exceeds the registered role limit")
    if role not in {"investigation", "knowledge", "single"}:
        raise ValueError("unregistered observation role")
    standalone = context is None
    if not standalone and event_log is None:
        raise ValueError("shared context requires its event log")
    events = [] if standalone else event_log
    event_start = len(events)
    collection_keys = ('name', 'status', 'query_profile', 'truncated', 'error')
    prior_collection = [{k: e[k] for k in collection_keys}
                        for e in events[:event_start]
                        if e['type'] == 'tool_result' and e.get('role') == role]
    # Parallel branches receive a frozen prior-round snapshot instead of the
    # mutable global event log. Keep that history when adding this task's reads.
    if not prior_collection and objective:
        prior_collection = [{k: e[k] for k in collection_keys}
                            for e in objective.get('collection', []) if e.get('role') == role]
    task_span = ""
    task_parent = ""
    def on_event(event):
        nonlocal task_span
        if event.get("type") == "call_start" and event.get("kind") == "task":
            task_span = event["span_id"]
        event = {**event, "seq": len(events) + 1}
        events.append(event)
        if emit:
            emit(event)
    ticket = provider.ticket()
    if standalone:
        context = RunContext(limits=limits, emit=on_event, scope=ticket.scope)
    elif context.scope != ticket.scope:
        raise ValueError("shared scope mismatch")
    limits = context.limits
    executor = executor or ToolExecutor(provider, principal, ticket.scope, context, role=role)
    if executor.context is not context or executor.role != role or executor.scope != ticket.scope or executor.principal != principal:
        raise ValueError("shared executor mismatch")
    repair_state = repair_state if repair_state is not None else {"repairs": 0}
    allowed_tools = SINGLE_TOOLS if role == "single" else INVESTIGATION_TOOLS if role == "investigation" else KNOWLEDGE_TOOLS
    allowed_kinds = ("observation", "runbook", "past_incident") if role == "single" else ("observation",) if role == "investigation" else ("runbook", "past_incident")
    if role == "knowledge":
        from agents.prompts import KNOWLEDGE
        system = KNOWLEDGE
    else:
        system = SYSTEM
    system = system_prompt or system
    model = model.bind_tools(executor.schemas())
    task_id = str(uuid4())
    def event(kind, **fields):
        context.emit({"type": kind, "role": role, "task_id": task_id,
                      "span_id": task_span, "parent_span_id": task_parent or context.root_span, **fields})
    model_input = {
        "ticket": ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
        "step_limit": max_steps,
        "read_only_capabilities": executor.capabilities(),
        "objective": objective}
    if role == 'single':
        model_input['completion_requirements'] = {
            'required_sections': required_sections(ticket.purpose),
            'insufficient_evidence': '缺项时保留观测和具体信息缺口，以partial结束；不得编造原因或不适用的建议。'}
    messages = [SystemMessage(content=system), HumanMessage(content='')]
    output, stop, repairs, call_ids = None, "", 0, set()
    failures = []
    coverage_gaps, gap_feedback_sent = [], False
    task_feedback_sent = False
    initial_tool_calls = context.counts['tool_calls']
    required_checks = []
    if role != 'single' and objective:
        required_checks = [{'tool': c['tool'], 'args': executor.validate_args(c['tool'], c['args']).model_dump(mode='json')}
                           for c in objective.get('required_checks', [])]
    permitted = {(c['tool'], executor.validate_args(c['tool'], c['args']).model_dump_json())
                 for c in (approved_checks or [])}
    checked, model_omitted = set(), False
    output_protocol = "none"
    with activate(context), context.span("task", role) as span:
        task_span, task_parent = span['span_id'], span['parent_span_id']
        event("task_created")
        event("task_started")
        # Reviewer-selected, Supervisor-dispatched checks are already authorized.
        # Execute through the same scoped executor before asking for interpretation,
        # so a model returning a final answer cannot silently skip the approved work.
        if approved_checks:
            calls = [{'name': c['tool'], 'args': c['args'], 'id': f'approved_{n}', 'type': 'tool_call'}
                     for n, c in enumerate(approved_checks, 1)]
            messages.append(AIMessage(content='', tool_calls=calls))
            for call in calls:
                call_ids.add(call['id'])
                event('tool_selected', name=call['name'], execution_source='approved_rework')
                result = executor.execute(call['name'], call['args'])
                signature = (call['name'], executor.validate_args(call['name'], call['args']).model_dump_json())
                if result.status in {'ok', 'empty'} and not result.truncated:
                    checked.add(signature)
                event('tool_result', name=call['name'], status=result.status,
                      evidence_ids=[e.evidence_id for e in result.evidence],
                      query_profile=dict(executor.last_query_profile), sample_order=result.sample_order,
                      truncated=result.truncated, error=result.error.model_dump() if result.error else None,
                      execution_source='approved_rework')
                messages.append(ToolMessage(content=result_view(result), tool_call_id=call['id']))
                if result.error and result.error.code == 'BUDGET_EXCEEDED':
                    stop = 'BUDGET_EXCEEDED'
                    break
            messages.append(HumanMessage(content='The server executed the approved checks above. Interpret their results '
                'and return the output JSON. Preserve failed/truncated check gaps; do not repeat completed queries.'))
        for step in range(max_steps):
            if stop:
                break
            try:
                terminal = role == "single" and (step == max_steps - 1 or not context.can_research())
                if terminal:
                    messages.append(HumanMessage(content="Only the final output remains within budget. Return diagnosis JSON, no tools; preserve gaps."))
                collection = [*prior_collection, *[{k: e[k] for k in collection_keys}
                              for e in events[event_start:]
                              if e['type'] == 'tool_result' and e.get('role') == role]]
                current_objective = dict(objective) if objective is not None else None
                if current_objective is not None:
                    from workflow.task_checks import completion
                    current_objective.update(required_checks=required_checks,
                        check_completion=completion(required_checks, executor),
                        collection=collection[-12:],
                        query_state_source='server_read_ledger')
                # Refresh after tool reads; an initial null-only contract must not
                # hide a newly observed owner, including during correction turns.
                messages[1] = HumanMessage(content=json.dumps({**model_input,
                    'objective': current_objective, 'collection': collection[-12:],
                    'output_schema': owner_schema(output_schema, executor),
                    'owner_options': owner_options(executor)}, ensure_ascii=False))
                messages, compacted = fit_messages(messages, executor, collection, limits.input_chars)
                if compacted:
                    model_omitted = model_omitted or bool(compacted['omitted_evidence_count'])
                    event('model_input_compacted', **compacted)
                response = invoke_chat_model(model, messages, role, terminal=terminal)
                # A blocking SDK call is not cancelled by an outer timer. Discard late responses.
                if time.monotonic() - context.started >= limits.seconds - (0 if terminal else limits.reserve_seconds):
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
                    step_truncated = False
                    empty_observations = []
                    for call in calls:
                        selected = call.get("name")
                        name = selected if selected in allowed_tools else "denied"
                        event("tool_selected", name=name, step=step + 1)
                    if step == max_steps - 1 or terminal:
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
                        signature = None
                        if permitted:
                            try:
                                args = executor.validate_args(call['name'], call['args'])
                                signature = (call['name'], args.model_dump_json())
                                code = None if signature in permitted else 'CHECK_NOT_APPROVED'
                            except ExecutionError as exc:
                                code = exc.code
                            if code:
                                executor.last_query_profile = {}
                                result = executor.rejected(code)
                            else:
                                result = executor.execute(call['name'], call['args'])
                        else:
                            result = executor.execute(call["name"], call["args"])
                        if signature and result.status in {'ok', 'empty'} and not result.truncated:
                            checked.add(signature)
                        event("tool_result", name=call["name"] if call["name"] in allowed_tools else "denied",
                              status=result.status, evidence_ids=[e.evidence_id for e in result.evidence],
                              query_profile=dict(executor.last_query_profile),
                              sample_order=result.sample_order,
                              truncated=result.truncated, error=result.error.model_dump() if result.error else None)
                        step_truncated = step_truncated or result.truncated
                        if result.status == "empty" and call["name"] in {"get_service_logs", "get_service_metrics"}:
                            empty_observations.append({"tool": call["name"], "query_profile": dict(executor.last_query_profile)})
                        messages.append(ToolMessage(content=result_view(result), tool_call_id=cid))
                        if result.error and result.error.code == "BUDGET_EXCEEDED":
                            stop = "BUDGET_EXCEEDED"
                            break
                    if stop:
                        break
                    # Explicit final-step instruction counts as part of the normal conversation.
                    if step == max_steps - 2:
                        messages.append(HumanMessage(content="Next step is final: return the output JSON, no tools. State any gaps."))
                    elif step_truncated:
                        messages.append(HumanMessage(content="A tool result was truncated. Metrics are latest-first; "
                            "the returned samples do not cover the full requested window. Use remaining steps for "
                            "a focused query with fewer relevant metrics or a discriminating log check before finalizing. "
                            "Do not infer service health or absence of errors from incomplete observations."))
                    if empty_observations and step < max_steps - 2:
                        messages.append(HumanMessage(content="These observation queries returned no matching rows: "
                            + json.dumps(empty_observations) + ". This is limited to the selected filters. "
                            "An exact ERROR level excludes WARN and INFO. Use your remaining observation step "
                            "to reconsider optional level/error_code or choose another useful check within scope. "
                            "Do not finalize a claim that the service/category has no logs or is healthy. "
                            "If you cannot obtain evidence, state the specific query gap."))
                    if executor.log_gaps and step < max_steps - 2:
                        messages.append(HumanMessage(content='ERROR-only log queries excluded WARN/INFO. '
                            'Use a focused get_service_logs query for the same category/window, omitting level '
                            'and unnecessary error_code, or retain that explicit gap. Do not repeat the ERROR query. '
                            + json.dumps(list(executor.log_gaps.values()))))
                    continue
                try:
                    candidate, output_protocol = output_resolver(response.content, executor, allowed_kinds)
                    if required_checks:
                        from workflow.task_checks import completion
                        task_coverage = completion(required_checks, executor)
                        unexecuted = [c for c in task_coverage['checks'] if c['status'] == 'not_executed']
                        if unexecuted:
                            remaining_calls = limits.model_calls - limits.reserve_model_calls - context.counts['model_calls']
                            # One tool-selecting response and one final interpretation
                            # must both fit the existing step and shared call budgets.
                            can_correct = (not task_feedback_sent and step < max_steps - 2
                                           and remaining_calls >= 2 and context.can_research())
                            if can_correct:
                                task_feedback_sent = True
                                event('task_execution_correction', step=step + 1,
                                      pending_tools=[c['tool'] for c in unexecuted])
                                messages.append(HumanMessage(content='声明的检查尚未执行。不能将输入中的检查缺口当作查询结果直接结束。'
                                    '请在当前工单范围内实际调用已允许的只读工具，选择有价值的未执行检查；随后解释工具结果并返回最终 JSON。'
                                    '不要重复已执行、失败或返回空结果的同一查询，不扩大权限或预算。待执行检查：'
                                    + json.dumps([{'tool': c['tool'], 'args': c['args']} for c in unexecuted], ensure_ascii=False)))
                                continue
                            candidate = candidate.model_copy(update={'missing_information': list(dict.fromkeys([
                                *task_coverage['missing_information'], *candidate.missing_information]))[:8]})
                            if context.counts['tool_calls'] == initial_tool_calls:
                                output, stop = candidate, 'NO_TOOL_PROGRESS'
                                event('task_execution_stalled', code=stop,
                                      correction_sent=task_feedback_sent,
                                      pending_tools=[c['tool'] for c in unexecuted])
                                break
                    has_hypotheses = bool(getattr(candidate, 'hypotheses', getattr(candidate, 'tentative_hypotheses', [])))
                    coverage_gaps = [*(pool_control_gaps(executor.evidence) if has_hypotheses else []),
                                     *log_control_gaps(executor)]
                    if permitted - checked:
                        coverage_gaps.append('approved_read_only_checks_not_completed')
                    if coverage_gaps and not gap_feedback_sent and not terminal and step < max_steps - 1 and context.can_research():
                        gap_feedback_sent = True
                        event('observation_gap', missing_controls=coverage_gaps)
                        messages.append(HumanMessage(content=gap_message(coverage_gaps) +
                            ' Use remaining research budget for a focused useful check within scope. For ERROR-only log gaps, '
                            'omit level on the same category/window to include WARN/INFO. On rework use only approved_checks. '
                            'Otherwise finish with this explicit gap and a qualified hypothesis. Do not repeat completed queries.'))
                        continue
                    if coverage_gaps:
                        candidate = candidate.model_copy(update={'missing_information': list(dict.fromkeys([
                            gap_message(coverage_gaps), *candidate.missing_information]))[:8]})
                    output = candidate
                    stop = "FINISHED"
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    failure = {"step": step + 1, **validation_details(exc)}
                    failures.append(failure)
                    event("validation_failure", node=role, **failure)
                    if step < max_steps - 1 and context.claim_repair(repair_state):
                        repairs += 1
                        event("validation_repair", node=role)
                        reference_options = {eid: [o.model_dump() for o in e.reference_options if substantive(o.field_path)]
                                             for eid, e in executor.evidence.items()}
                        messages.append(HumanMessage(content="Output contract/reference check failed. Safe error details: "
                            + json.dumps(failure) + ". Available reference_options by evidence_id: "
                            + json.dumps(reference_options) + ". Return refs as [{\"reference_id\":\"an exact REF_ ID from the options\"}]. "
                            + "Do not copy field_path, value, unit, timestamp or quote; never prepend payload. "
                            + "The server expands these fields. Omit unverifiable findings. An empty query has no REF: "
                            + "use findings=[] and state its filter/window limitation in missing_information; never use refs=[]. "
                            + "escalation_ref must select owner_options or be null. No additional tool calls required."))
                    else:
                        stop = "MODEL_OUTPUT_INVALID"
                        break
            except ExecutionError as exc:
                context.error(exc, role)
                stop = exc.code
                break
        if not stop:
            stop = "STEP_LIMIT"
        if output is None:
            if role == "single":
                from agents.contracts import DiagnosisDraft
                output = DiagnosisDraft(findings=[], hypotheses=[], recommended_actions=[],
                    missing_information=[f"SingleAgent stopped: {stop}; evidence requires human review."], escalation_team=None)
            else:
                output = InvestigationOutput(missing_information=[f"Investigation stopped: {stop}; evidence requires human review."])
        # Conservative M2 boundary: a truncated source remains explicitly partial, even
        # when selected fields are valid. M3 can assess whether later checks close that gap.
        incomplete = model_omitted or any(e["type"] == "tool_result" and e["truncated"] for e in events[event_start:])
        if model_omitted:
            output = output.model_copy(update={'missing_information': [
                'Some evidence was omitted from the bounded model input; full snapshots remain in the audit report.',
                *output.missing_information][:8]})
        if role == 'single' and stop == 'FINISHED':
            gaps = diagnosis_gaps(output, ticket.purpose)
            if gaps:
                stop = 'DIAGNOSIS_INCOMPLETE'
                output = output.model_copy(update={'missing_information': list(dict.fromkeys([
                    *output.missing_information, *gaps]))})
                event('diagnosis_gate', code=stop, missing_information=gaps)
        if standalone:
            context.stop_reason = stop
        empty_checks_completed = False
        if required_checks:
            from workflow.task_checks import completion
            coverage = completion(required_checks, executor)
            empty_checks_completed = coverage['status'] == 'checks_completed_empty'
        status = "completed" if stop == "FINISHED" and (output.findings or empty_checks_completed) and not incomplete and not coverage_gaps else "partial" if executor.evidence or stop in {"FINISHED", "BUDGET_EXCEEDED", "STEP_LIMIT", "TIME_BUDGET_EXCEEDED", "NO_TOOL_PROGRESS", "DIAGNOSIS_INCOMPLETE"} else "failed"
        event("task_completed", status=status, reason=stop)
    return {"run_id": context.run_id, "task_id": task_id, "task_type": "incident_" + role, "incident_id": ticket.incident_id,
            "scope": ticket.scope.model_dump(mode="json"), "data_source": provider.name,
            "prompt_version": hashlib.sha256((system + json.dumps(output_schema.model_json_schema(), sort_keys=True)
                + json.dumps(executor.schemas(), sort_keys=True)).encode()).hexdigest()[:12],
            "output_protocol": output_protocol,
            "fact_rendering": FACT_RENDERING, "coverage_gaps": coverage_gaps,
            "model_input_view": MODEL_VIEW_VERSION, "model_input_omitted": model_omitted,
            "status": status, "review_status": "not_performed", "output": output.model_dump(mode="json"),
            "observation_coverage": "limited_by_truncation" if incomplete else "not_truncated",
            "evidence": [e.model_dump(mode="json") for e in executor.evidence.values()],
            "events": list(events[event_start:]), "run_summary": {**context.summary(), "termination_reason": stop}, "repairs": repairs,
            "validation_failures": failures}
