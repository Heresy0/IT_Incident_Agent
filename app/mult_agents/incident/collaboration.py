"""Serial coordinator: model decisions inside a bounded, server-owned lifecycle."""
import hashlib
import json
import time
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from mult_agents.harness.runtime import RunContext, ExecutionError, activate, invoke_chat_model
from .contracts import Finding, InvestigationOutput
from .collaboration_contracts import SupervisorDecision, DiagnosisSelection, DiagnosisDraft, Hypothesis, ReviewDecision
from .collaboration_prompts import ROLE_PROMPTS
from .investigation import INCIDENT_LIMITS, investigate, resolve_output, validate_output, validation_details
from .tools import ToolExecutor


from .diagnosis import ProtocolError, expand_refs, expand_diagnosis
from .observations import FACT_RENDERING, pool_control_gaps, gap_message


class Collaboration:
    def __init__(self, models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None, event_log=None):
        if set(models) != {"supervisor", "investigation", "knowledge", "diagnosis", "reviewer"}:
            raise ValueError("five registered models required")
        self.models, self.provider, self.principal = models, provider, principal
        self.ticket = provider.ticket()
        provider.authorize(principal, self.ticket.scope)
        self.events, self.tasks, self.negotiation, self.drafts, self.reviews = [], [], [], [], []
        self.decisions = []
        def sink(event):
            event = {**event, "seq": len(self.events) + 1}
            self.events.append(event)
            if emit:
                emit(event)
        if context is None:
            self.context = RunContext(limits=limits, scope=self.ticket.scope, emit=sink)
        else:
            if context.scope != self.ticket.scope or event_log is None:
                raise ValueError("shared collaboration context requires matching scope and event log")
            self.context, self.events = context, event_log
        self.executors = {role: ToolExecutor(provider, principal, self.ticket.scope, self.context, role=role)
                          for role in ("investigation", "knowledge")}
        self.repairs = {"repairs": 0}
        self.evidence, self.references, self.seen_tasks = {}, {}, set()
        self.supervisor_calls = self.reworks = 0
        self.pending, self.draft, self.review = None, None, None
        self.phase, self.stop, self.extra_gaps, self.team = "collect", "", [], None
        self.workflow_span = self.context.root_span

    def event(self, kind, role="supervisor", **fields):
        self.context.emit({"type": kind, "role": role, "span_id": self.workflow_span,
                           "parent_span_id": self.context.root_span, **fields})

    def note(self, kind, sender, target, *, hypothesis_id=None, evidence_ids=(), reason="", check=""):
        self.negotiation.append({"type": kind, "sender": sender, "target": target,
            "hypothesis_id": hypothesis_id, "evidence_ids": list(evidence_ids), "reason": reason, "requested_check": check})
        self.event({"propose_hypothesis": "hypothesis_proposed", "request_evidence": "rework_requested",
                    "accept": "review_accepted", "escalate": "escalation_recommended"}.get(kind, kind), sender,
                   hypothesis_id=hypothesis_id, evidence_ids=list(evidence_ids))

    def evidence_ids(self, ids):
        if len(ids) != len(set(ids)) or any(eid not in self.evidence for eid in ids):
            raise ProtocolError("UNKNOWN_OR_DUPLICATE_EVIDENCE")

    def owner(self, team):
        if team is not None and team not in {e.payload.get("team") for e in self.evidence.values() if "team" in e.payload}:
            raise ProtocolError("UNOBSERVED_TEAM")

    def sources(self, ids=None):
        items = [self.evidence[eid] for eid in ids] if ids is not None else list(self.evidence.values())
        result = []
        for e in items:
            fields = {k: v for k, v in e.payload.items() if k in {"metric", "value", "unit", "aggregation",
                "category", "level", "error_code", "message", "summary", "config_summary", "team", "escalation",
                "text", "resolution", "versions", "updated_at", "confirmed_at", "validity"}}
            options = [o.model_dump() for o in e.reference_options if o.field_path in {
                "value", "message", "error_code", "summary", "text", "resolution", "team", "escalation"}
                or o.field_path.startswith("config_summary.")]
            result.append({"evidence_id": e.evidence_id, "kind": e.kind, "service": e.service,
                "environment": e.environment, "observed_at": e.observed_from.isoformat(),
                "data_version": e.data_version, "payload": fields, "reference_options": options})
        return result

    def ask(self, role, schema, payload, *, terminal=False, validate=None):
        messages = [SystemMessage(content=ROLE_PROMPTS[role]), HumanMessage(content=json.dumps({
            "input": payload, "output_schema": schema.model_json_schema()}, ensure_ascii=False))]
        while True:
            response = invoke_chat_model(self.models[role], messages, role, terminal=terminal)
            deadline = self.context.limits.seconds - (0 if terminal else self.context.limits.reserve_seconds)
            if time.monotonic() - self.context.started >= deadline:
                raise ExecutionError("TIME_BUDGET_EXCEEDED")
            self.event("model_output", role, output_chars=len(str(response.content)))
            try:
                if response.tool_calls or getattr(response, "invalid_tool_calls", None) or len(str(response.content)) > 12000:
                    raise ProtocolError("ROLE_OUTPUT_INVALID")
                value = schema.model_validate_json(response.content)
                return validate(value) if validate else value
            except (ValidationError, ValueError, TypeError) as exc:
                details = {"reason": exc.code, "details": []} if isinstance(exc, ProtocolError) else validation_details(exc)
                self.event("validation_failure", role, **details)
                if self.repairs["repairs"] >= 1:
                    raise ExecutionError("MODEL_OUTPUT_INVALID") from None
                self.repairs["repairs"] += 1
                self.event("validation_repair", role)
                messages.extend([response, HumanMessage(content="Repair once using the supplied contract and observed IDs. "
                    + json.dumps(details) + ". Return JSON only, no tools or additional identity fields.")])

    def expand_refs(self, refs):
        return expand_refs(refs, self)

    def diagnosis(self, selection):
        return expand_diagnosis(selection, self)

    def targets(self):
        return {f"{prefix}{n}": item for prefix, items in (("F", self.draft.findings),
            ("H", self.draft.hypotheses), ("A", self.draft.recommended_actions)) for n, item in enumerate(items, 1)}

    def reviewer_input(self):
        targets = {}
        for key, item in self.targets().items():
            value = item.model_dump(mode="json")
            for field in ("refs", "support_refs", "counter_refs"):
                if field in value:
                    value[field] = [{"evidence_id": r["evidence_id"], "field_path": r["field_path"]} for r in value[field]]
            targets[key] = value
        return {"targets": targets, "evidence": self.sources(), "collection": self.collection(), "rework_used": self.reworks,
                "prior_challenge": self.pending, "observation_gaps": pool_control_gaps(self.evidence),
                "budget": {"can_collect": self.context.can_research(), "remaining_tools": self.context.limits.tool_calls - self.context.counts['tool_calls']}}

    def collection(self):
        # Empty/truncated/error results remain visible to the tool-free judging roles.
        return [{k: event[k] for k in ("role", "name", "status", "query_profile", "truncated", "evidence_ids", "error")}
                for event in self.events if event["type"] == "tool_result"]

    def validate_review(self, review):
        targets = self.targets()
        ids = [a.target_id for a in review.assessments]
        if len(ids) != len(set(ids)) or set(ids) != set(targets):
            raise ProtocolError("REVIEW_TARGET_COVERAGE")
        checked = []
        for assessment in review.assessments:
            self.evidence_ids(assessment.evidence_ids)
            target = targets[assessment.target_id]
            if assessment.verdict == "supported" and assessment.target_id.startswith("H"):
                if not any(self.evidence[r.evidence_id].kind == "observation" for r in target.support_refs):
                    assessment = assessment.model_copy(update={"verdict": "uncertain",
                        "reason": "Program gate: historical knowledge alone cannot establish a current cause."})
                    self.event("review_gate", "reviewer", target_id=assessment.target_id, code="NO_CURRENT_SUPPORT")
                elif pool_control_gaps(self.evidence):
                    assessment = assessment.model_copy(update={'verdict': 'uncertain', 'reason': gap_message(pool_control_gaps(self.evidence))})
                    self.event('review_gate', 'reviewer', target_id=assessment.target_id, code='MISSING_HEALTH_CONTROL')
            checked.append(assessment)
        request = review.request_evidence
        if request:
            self.evidence_ids(request.evidence_ids)
            if request.hypothesis_id not in targets or any(a.target_id == request.hypothesis_id and a.verdict == "supported" for a in checked):
                raise ProtocolError("INVALID_REWORK_TARGET")
        return review.model_copy(update={"assessments": checked})

    def diagnose_and_review(self):
        self.review = None  # A previous revision's review never approves a new draft.
        payload = {"ticket": self.ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
                   "evidence": self.sources(), "collection": self.collection(), "challenge": self.pending,
                   "observation_gaps": pool_control_gaps(self.evidence),
                   "task_results": [{"role": t["role"], "status": t["status"], "missing_information": t["output"]["missing_information"]} for t in self.tasks]}
        self.draft = self.ask("diagnosis", DiagnosisSelection, payload, terminal=True, validate=self.diagnosis)
        revision = len(self.drafts) + 1
        self.drafts.append({"revision": revision, "draft": self.draft.model_dump(mode="json")})
        for h in self.draft.hypotheses:
            self.note("revise" if revision > 1 else "propose_hypothesis", "diagnosis", "reviewer",
                      hypothesis_id=h.hypothesis_id, evidence_ids=[r.evidence_id for r in h.support_refs], reason=h.cause)
        if not self.targets():
            self.stop = "NEEDS_INFORMATION"
            return
        self.review = self.ask("reviewer", ReviewDecision, self.reviewer_input(), terminal=True, validate=self.validate_review)
        self.reviews.append({"revision": revision, "review": self.review.model_dump(mode="json")})
        self.event("review_completed", "reviewer", revision=revision,
                   status="needs_rework" if self.review.request_evidence else
                   "passed" if all(a.verdict == "supported" for a in self.review.assessments) else "needs_information")
        request = self.review.request_evidence
        if request:
            self.note("challenge", "reviewer", "diagnosis", hypothesis_id=request.hypothesis_id,
                      evidence_ids=request.evidence_ids, reason=request.missing_observation)
            if self.reworks >= 1:
                self.event("rework_denied", "supervisor", code="REWORK_LIMIT")
                self.stop = "REWORK_LIMIT"
            else:
                self.pending = {"challenge_id": "C1", **request.model_dump()}
                self.note("request_evidence", "reviewer", "supervisor", hypothesis_id=request.hypothesis_id,
                          evidence_ids=request.evidence_ids, reason=request.expected_value, check=request.proposed_check)
                self.phase = "rework"
        else:
            self.pending = None
            self.stop = "FINISHED" if all(a.verdict == "supported" for a in self.review.assessments) else "REVIEW_INCOMPLETE"
            self.note("accept" if self.stop == "FINISHED" else "escalate", "reviewer", "supervisor")

    def dispatch(self, decision):
        if not decision.tasks or len(self.tasks) + len(decision.tasks) > 6:
            raise ProtocolError("TASK_LIMIT")
        if self.phase == "rework" and (len(decision.tasks) != 1 or decision.tasks[0].role != self.pending["target_role"]):
            raise ProtocolError("INVALID_REWORK_DISPATCH")
        keys = []
        for task in decision.tasks:
            self.evidence_ids(task.evidence_ids)
            key = (task.role, " ".join(task.goal.casefold().split()), tuple(sorted(task.evidence_ids)))
            if key in self.seen_tasks or key in keys:
                raise ProtocolError("DUPLICATE_TASK")
            keys.append(key)
        capacity = self.dispatch_capacity()
        if capacity < 1 or not self.context.can_research():
            self.event("rework_denied" if self.phase == "rework" else "dispatch_denied", code="BUDGET_EXCEEDED",
                       requested_roles=[t.role for t in decision.tasks], available_tasks=capacity)
            if self.phase == "collect" and any(e.kind == "observation" for e in self.evidence.values()):
                self.event("finalization_started", reason="research_budget_unavailable")
                self.diagnose_and_review()
                return
            raise ExecutionError("BUDGET_EXCEEDED")
        selected = list(zip(decision.tasks, keys))
        if capacity < len(selected):
            # Current observations are a prerequisite for diagnosis; knowledge is optional.
            if not any(e.kind == "observation" for e in self.evidence.values()):
                selected.sort(key=lambda pair: pair[0].role != "investigation")
            for task, _ in selected[capacity:]:
                self.event("task_deferred", task.role, reason="insufficient_dispatch_budget")
            selected = selected[:capacity]
        self.seen_tasks.update(key for _, key in selected)
        follow_up = self.phase == "rework"
        if follow_up:
            self.reworks += 1
        previous_evidence = set(self.evidence)
        for index, (task, _) in enumerate(selected):
            if not self.context.can_research():
                self.event("task_deferred", task.role, reason="research_budget_unavailable")
                if not follow_up and any(e.kind == "observation" for e in self.evidence.values()):
                    self.event("finalization_started", reason="research_budget_unavailable")
                    self.diagnose_and_review()
                    return
                raise ExecutionError("BUDGET_EXCEEDED")
            ids = list(dict.fromkeys([*task.evidence_ids, *(self.pending["evidence_ids"] if follow_up else [])]))[:8]
            # Reuse same-role registered snapshots even when the scheduler supplies no IDs.
            # Selected sources retain priority, while the bounded fallback exposes earlier checks.
            available = self.executors[task.role].evidence
            ordered = list(dict.fromkeys([*ids, *reversed(list(available))]))
            prior, size = [], 0
            for source in self.sources(ordered):
                encoded = len(json.dumps(source, ensure_ascii=False))
                if size + encoded <= min(6000, self.context.limits.input_chars // 4):
                    prior.append(source); size += encoded
            objective = {"goal": task.goal, "evidence": prior, "focused_evidence_ids": ids,
                "collection": self.collection()[-12:], "observation_gaps": pool_control_gaps(self.evidence),
                "challenge": self.pending if follow_up else None}
            before = dict(self.context.counts)
            available_steps = self.context.limits.model_calls - self.context.limits.reserve_model_calls - before["model_calls"]
            steps = min(4, available_steps - 2 * (len(selected) - index - 1))
            result = investigate(self.models[task.role], self.provider, self.principal, context=self.context,
                event_log=self.events, executor=self.executors[task.role], objective=objective,
                role=task.role, repair_state=self.repairs, max_steps=max(1, steps))
            self.tasks.append({"task_id": result["task_id"], "role": task.role, "goal": task.goal,
                "challenge_id": self.pending["challenge_id"] if follow_up else None,
                "status": result["status"], "output": result["output"],
                "model_calls": self.context.counts["model_calls"] - before["model_calls"],
                "tool_calls": self.context.counts["tool_calls"] - before["tool_calls"],
                "prompt_version": result["prompt_version"], "termination_reason": result["run_summary"]["termination_reason"],
                "evidence_ids": [e["evidence_id"] for e in result["evidence"]]})
            executor = self.executors[task.role]
            self.evidence.update({k: v.model_copy(deep=True) for k, v in executor.evidence.items()})
            self.references.update({k: v.model_copy(deep=True) for k, v in executor.references.items()})
        if not follow_up and previous_evidence and not set(self.evidence).difference(previous_evidence):
            self.event('collection_stalled', reason='no_new_evidence', task_ids=[t['task_id'] for t in self.tasks[-len(selected):]])
            self.diagnose_and_review()
            return
        if follow_up:
            self.diagnose_and_review()

    def dispatch_capacity(self, pending_model_call=False):
        available = self.context.limits.model_calls - self.context.counts["model_calls"] - int(pending_model_call)
        tools = self.context.limits.tool_calls - self.context.counts["tool_calls"]
        return max(0, min(2, tools, (available - self.context.limits.reserve_model_calls) // 2))

    def run(self):
        with activate(self.context), self.context.span("workflow", "incident_collaboration"):
            self.workflow_span = self.events[-1]["span_id"]
            try:
                while not self.stop:
                    if self.supervisor_calls >= 4:
                        self.stop = "SUPERVISOR_LIMIT"
                        break
                    decision = self.ask("supervisor", SupervisorDecision, {
                        "ticket": self.ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
                        "phase": self.phase, "pending_challenge": self.pending, "evidence": self.sources(),
                        "collection": self.collection(), "observation_gaps": pool_control_gaps(self.evidence),
                        "tasks": [{**{k: t[k] for k in ("role", "goal", "status", "evidence_ids")},
                                   "missing_information": t["output"]["missing_information"]} for t in self.tasks],
                        "budget": {"used": dict(self.context.counts), "limits": self.context.limits.__dict__,
                                   "max_dispatch_tasks": self.dispatch_capacity(pending_model_call=True)}},
                        terminal=bool(self.evidence and self.phase == "collect"))
                    self.supervisor_calls += 1
                    self.decisions.append({"round": self.supervisor_calls, **decision.model_dump(mode="json")})
                    self.event("supervisor_decision", action=decision.action, round=self.supervisor_calls,
                               requested_roles=[t.role for t in decision.tasks])
                    if decision.action != "dispatch" and decision.tasks:
                        raise ProtocolError("UNEXPECTED_TASKS")
                    if decision.action == "dispatch":
                        self.dispatch(decision)
                    elif decision.action == "diagnose":
                        if self.phase == "rework" or not any(e.kind == "observation" for e in self.evidence.values()):
                            raise ProtocolError("DIAGNOSIS_WITHOUT_OBSERVATIONS")
                        self.diagnose_and_review()
                    elif decision.action == "finish":
                        if self.review is None or any(a.verdict != "supported" for a in self.review.assessments):
                            raise ProtocolError("UNREVIEWED_FINISH")
                        self.stop = "FINISHED"
                    else:
                        self.owner(decision.escalation_team)
                        self.team = decision.escalation_team
                        self.extra_gaps.extend(decision.missing_information or [decision.reason])
                        self.note("escalate", "supervisor", "human", reason=decision.reason)
                        self.stop = "ESCALATED" if decision.action == "escalate" else "NEEDS_INFORMATION"
            except (ExecutionError, ProtocolError) as exc:
                self.stop = exc.code
                self.context.error(exc if isinstance(exc, ExecutionError) else ExecutionError(exc.code), "incident_collaboration")
            self.context.stop_reason = self.stop
            self.event("workflow_completed", status=self.status(), reason=self.stop)
        return self.result()

    def status(self):
        if self.stop == "FINISHED" and self.review is not None:
            return "completed"
        return "partial" if self.evidence or self.stop in {"NEEDS_INFORMATION", "ESCALATED", "BUDGET_EXCEEDED", "SUPERVISOR_LIMIT"} else "failed"

    def result(self):
        approved = {a.target_id for a in self.review.assessments if a.verdict == "supported"} if self.review else set()
        findings, hypotheses, actions, used = [], [], [], set()
        if self.draft:
            for n, finding in enumerate(self.draft.findings, 1):
                if f"F{n}" in approved:
                    findings.append(finding.model_dump(mode="json"))
                    used.update(r.evidence_id for r in finding.refs)
            verdicts = {a.target_id: a.verdict for a in self.review.assessments} if self.review else {}
            for h in self.draft.hypotheses:
                verdict = verdicts.get(h.hypothesis_id)
                status = {"supported": "supported", "not_supported": "refuted", "uncertain": "unresolved"}.get(verdict, "tentative")
                hypotheses.append(h.model_copy(update={"status": status}).model_dump(mode="json"))
                if status == "supported":
                    used.update(r.evidence_id for r in [*h.support_refs, *h.counter_refs])
            actions = [a.model_dump() for n, a in enumerate(self.draft.recommended_actions, 1) if f"A{n}" in approved]
            self.extra_gaps.extend(self.draft.missing_information)
            self.team = self.draft.escalation_team or self.team
        if self.review:
            self.extra_gaps.extend(self.review.missing_information)
            for a in self.review.assessments:
                if a.verdict == "supported":
                    used.update(a.evidence_ids)
                else:
                    self.extra_gaps.append(f"{a.target_id}: {a.reason}")
        if self.status() != "completed":
            self.extra_gaps.append(f"协作尚有缺口：{self.stop}；需要补充信息或人工升级。")
        return {"run_id": self.context.run_id, "task_type": "incident_collaboration", "incident_id": self.ticket.incident_id,
            "scope": self.ticket.scope.model_dump(mode="json"), "data_source": self.provider.name, "status": self.status(),
            "review_status": "passed" if self.stop == "FINISHED" else "needs_information" if self.review else "not_performed",
            "business_result": "diagnosis_available" if self.status() == "completed" else "escalation_recommended" if self.team else "needs_information",
            "output": {"findings": findings, "hypotheses": hypotheses, "recommended_actions": actions,
                       "missing_information": list(dict.fromkeys(self.extra_gaps)), "escalation_team": self.team},
            "used_evidence_ids": sorted(used), "evidence": [e.model_dump(mode="json") for e in self.evidence.values()],
            "task_results": self.tasks, "drafts": self.drafts, "reviews": self.reviews, "negotiation": self.negotiation,
            "events": self.events, "run_summary": self.context.summary(), "repairs": self.repairs["repairs"],
            "fact_rendering": FACT_RENDERING, "coverage_gaps": pool_control_gaps(self.evidence),
            "rework_rounds": self.reworks, "supervisor_decisions": self.supervisor_calls,
            "scheduling_decisions": self.decisions,
            "prompt_version": self.prompt_version()}

    def prompt_version(self):
        from .collaboration_prompts import KNOWLEDGE
        from .prompts import SYSTEM
        schemas = [SupervisorDecision.model_json_schema(), DiagnosisSelection.model_json_schema(), ReviewDecision.model_json_schema()]
        payload = [ROLE_PROMPTS, SYSTEM, KNOWLEDGE, schemas, *[ex.schemas() for ex in self.executors.values()]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def collaborate(models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None, event_log=None):
    return Collaboration(models, provider, principal, limits=limits, emit=emit, context=context, event_log=event_log).run()
