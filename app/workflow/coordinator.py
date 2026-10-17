"""Role operations and guards reused by the incident LangGraph workflow."""
import hashlib
import json
import time
from runtime.context import RunContext, ExecutionError, activate
from agents.contracts import SupervisorSelection, DiagnosisSelection, ReviewDecision, ObservationReview, TaskRequest
from agents.prompts import ROLE_PROMPTS, INVESTIGATION_TASK, OBSERVATION_REVIEW, PLANNER
from agents.investigation import INCIDENT_LIMITS
from tools.executor import ToolExecutor
from agents.role_runner import RoleRequest, RoleRunner
from agents.protocol import RoleContractContext, role_output_schema, role_model_input, parse_role_output
from workflow.planning import PlanningContext, validate_plan
from workflow.state import WorkflowState, state_property
from workflow.policy import WorkflowPolicy, BudgetView, policy_property
from workflow.review import (ReviewContext, ReviewAudit, ReviewRoutingContext, validate_review,
    validate_observation_review, additional_rework_eligible, needs_review_follow_up,
    resolve_handoff, decide_rework, completion_reason)
from workflow.collection import (CollectionRequest, CollectionOutcome, CollectionRunner,
                                 execute_planned_reads, task_record, join_parallel)
from workflow.reporting import (PublicationInput, ReportSnapshot, publish_diagnosis, build_report,
                                unreviewed_observations, status_check_coverage)


from evidence.diagnosis import ProtocolError, expand_refs, expand_diagnosis, preserve_diagnosis
from evidence.observations import FACT_RENDERING, pool_control_gaps, log_control_gaps
from evidence.model_view import MODEL_VIEW_VERSION, evidence_view
from evidence.completion import diagnosis_gaps, required_sections
from workflow.task_checks import completion, adds_information, log_query_feedback
from workflow.evidence_needs import initial_needs, reviewed_needs, limitations, is_blocking
from workflow.diagnostic_audit import diagnostic_audit
from workflow.review_outcome import review_policy, readiness_gaps
from agents.protocol import REPAIR_POLICY, REVIEW_STAGE_POLICY, ROLE_REPAIR_POLICY
from workflow.handoff import (owner_choice, has_business_observation, fatal_collection_code,
                              can_finalize, handoff_notes, SingleAttemptOwnerProvider, FATAL_CODES)
from workflow.review_protection import conservative, BOUNDED_POLICY, DEFAULT_REVIEW_PROTECTION


class Collaboration:
    # Explicit legacy aliases share the aggregate; no duplicate attribute storage.
    phase = state_property('control', 'phase')
    next_node = state_property('control', 'next_node')
    stop = state_property('control', 'stop')
    supervisor_calls = state_property('control', 'supervisor_calls')
    reworks = state_property('control', 'reworks')
    pending = state_property('control', 'pending')
    additional_rework_authorized = state_property('control', 'additional_rework_authorized')
    review_follow_up_used = state_property('control', 'review_follow_up_used')
    collection_follow_up_used = state_property('control', 'collection_follow_up_used')
    protocol_fallback_used = state_property('control', 'protocol_fallback_used')
    last_supervisor_failure = state_property('control', 'last_supervisor_failure')
    limited_handoff = state_property('control', 'limited_handoff')

    tasks = state_property('collection', 'tasks')
    reused_requests = state_property('collection', 'reused_requests')
    evidence = state_property('collection', 'evidence')
    references = state_property('collection', 'references')
    seen_tasks = state_property('collection', 'seen_tasks')
    dispatch_queue = state_property('collection', 'dispatch_queue')
    dispatch_follow_up = state_property('collection', 'dispatch_follow_up')
    dispatch_previous = state_property('collection', 'dispatch_previous')
    dispatch_reads = state_property('collection', 'dispatch_reads')
    parallel_batch = state_property('collection', 'parallel_batch')
    parallel_plans = state_property('collection', 'parallel_plans')
    parallel_batches = state_property('collection', 'parallel_batches')
    collection_exits = state_property('collection', 'collection_exits')
    diagnostic_pack = state_property('collection', 'diagnostic_pack')
    diagnostic_pack_result = state_property('collection', 'diagnostic_pack_result')
    diagnostic_pack_exit = state_property('collection', 'diagnostic_pack_exit')

    draft = state_property('diagnosis', 'draft')
    review = state_property('diagnosis', 'review')
    evidence_needs = state_property('diagnosis', 'evidence_needs')
    drafts = state_property('diagnosis', 'drafts')
    reviews = state_property('diagnosis', 'reviews')
    observation_reviews = state_property('diagnosis', 'observation_reviews')
    deferred_review_requests = state_property('diagnosis', 'deferred_review_requests')
    review_hint_status = state_property('diagnosis', 'review_hint_status')
    extra_gaps = state_property('diagnosis', 'extra_gaps')
    team = state_property('diagnosis', 'team')

    events = state_property('audit', 'events')
    negotiation = state_property('audit', 'negotiation')
    decisions = state_property('audit', 'decisions')
    repairs = state_property('audit', 'repairs')
    workflow_span = state_property('audit', 'workflow_span')

    investigation_mode = policy_property('investigation_mode')
    knowledge_mode = policy_property('knowledge_mode')
    reviewer_repair_mode = policy_property('reviewer_repair_mode')
    review_protection = policy_property('review_protection')
    parallel_collection = policy_property('parallel_collection')
    blind_review = policy_property('blind_review')

    @property
    def planning_only(self):
        return self.policy.planning_only

    @property
    def role_corrections(self):
        return self.policy.role_corrections

    def __init__(self, models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None,
                 event_log=None, parallel_collection=True, blind_review=None, investigation_mode='programmatic', reviewer_repair_mode='shared', knowledge_mode=None, diagnostic_pack=None, review_protection=DEFAULT_REVIEW_PROTECTION):
        self.state = WorkflowState()
        self.policy = WorkflowPolicy(
            investigation_mode=investigation_mode,
            knowledge_mode=investigation_mode if knowledge_mode is None else knowledge_mode,
            reviewer_repair_mode=reviewer_repair_mode, review_protection=review_protection,
            parallel_collection=parallel_collection,
            blind_review=getattr(models.get('reviewer'), 'blind_review', False) if blind_review is None else blind_review)
        if set(models) != {"supervisor", "investigation", "knowledge", "diagnosis", "reviewer"}:
            raise ValueError("five registered models required")
        self.models, self.provider, self.principal = models, provider, principal
        self.ticket = provider.ticket()
        provider.authorize(principal, self.ticket.scope)
        if diagnostic_pack is not None:
            from workflow.diagnostic_pack import DiagnosticPack
            if not self.planning_only:
                raise ValueError('Diagnostic pack experiment requires programmatic collection')
            raw = diagnostic_pack.model_dump() if isinstance(diagnostic_pack, DiagnosticPack) else diagnostic_pack
            self.diagnostic_pack = DiagnosticPack.model_validate_json(json.dumps(raw))
            self.diagnostic_pack.assert_scope(self.ticket)
        self.evidence_needs = initial_needs(self.ticket)
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

    def supervisor_decision(self, selection):
        view = PlanningContext(
            phase=self.phase, purpose=self.ticket.purpose, evidence=self.evidence,
            references=self.references, evidence_needs=self.evidence_needs, executors=self.executors,
            investigation_mode=self.investigation_mode, knowledge_mode=self.knowledge_mode,
            planning_only=self.planning_only, reworks=self.reworks,
            additional_rework_authorized=self.additional_rework_authorized, pending=self.pending)
        return validate_plan(selection, view, emit=self.event,
            can_collect=lambda count: self.rework_budget(
                tool_calls=count, supervisor_completed=True)['can_collect'])

    def need_input(self):
        return [need.model_dump(mode='json') for need in self.evidence_needs.values()]

    def task_results(self):
        # Re-evaluate against all same-role reads so a later scoped rework can
        # discharge an earlier missing check without repeating a successful read.
        results = []
        for task in self.tasks:
            coverage = completion(task['required_checks'], self.executors[task['role']])
            results.append({**task, 'completion': coverage,
                'status': 'partial' if coverage['missing_information'] else task['execution_status']})
        return results

    def task_input(self):
        return [{**{k: t[k] for k in ('task_id', 'role', 'goal', 'expected_value', 'need_ids', 'status', 'execution_status', 'evidence_ids', 'completion')},
                 'candidate_mechanisms': t['output'].get('tentative_hypotheses', []),
                 'query_state_source': 'server_read_ledger',
                 'model_summary_verified': False,
                 'missing_information': t['output']['missing_information']} for t in self.task_results()]

    def task_gaps(self):
        return list(dict.fromkeys([
            *[f"任务目标未满足：{t['goal']}；{gap}" for t in self.task_results()
                for gap in t['completion']['missing_information']],
            *[f"复用查询限制：{t['goal']}；{gap}" for t in self.reused_requests
                for gap in completion(t['checks'], self.executors[t['role']])['missing_information']]]))

    def sources(self, ids=None):
        items = [self.evidence[eid] for eid in ids] if ids is not None else list(self.evidence.values())
        return [evidence_view(e) for e in items]

    def observation_gaps(self):
        return [*(pool_control_gaps(self.evidence) if self.ticket.purpose == 'diagnosis' else []),
                *log_control_gaps(self.executors['investigation'])]

    def ask(self, role, schema, payload, *, terminal=False, validate=None, system_prompt=None):
        view = RoleContractContext(
            evidence=self.evidence, references=self.references, executors=self.executors,
            evidence_needs=self.evidence_needs,
            targets=self.targets() if role == 'reviewer' and schema is ReviewDecision else {},
            purpose=self.ticket.purpose, phase=self.phase, planning_only=self.planning_only,
            investigation_mode=self.investigation_mode, knowledge_mode=self.knowledge_mode,
            review_protection=self.review_protection)
        output_schema = role_output_schema(role, schema, view)
        model_input = role_model_input(role, schema, payload, view)
        prompt = system_prompt or (PLANNER if role == 'supervisor' and self.planning_only else ROLE_PROMPTS[role])
        if self.review_protection == BOUNDED_POLICY:
            from agents.prompts import BOUNDED_DIAGNOSIS, BOUNDED_REVIEW
            if role == 'diagnosis':
                prompt += BOUNDED_DIAGNOSIS
            elif role == 'reviewer' and schema is ReviewDecision:
                prompt += BOUNDED_REVIEW
        request = RoleRequest(role, schema, output_schema, model_input, prompt, terminal)
        runner = RoleRunner(self.context, self.repairs, reviewer_repair_mode=self.reviewer_repair_mode,
                            role_corrections=self.role_corrections, purpose=self.ticket.purpose)
        return runner.run(request, self.models[role], emit=self.event,
            parse=lambda content: parse_role_output(role, schema, content, view, emit=self.event), validate=validate,
            follow_up=lambda result: self.role_follow_up(role, schema, result),
            exhausted=lambda exc, details, value: self.role_failure(role, schema, exc, details, value))

    def role_follow_up(self, role, schema, result):
        if role == 'supervisor' and self.needs_collection_follow_up(result):
            self.collection_follow_up_used = True
            pending = [n.model_dump(mode='json') for n in result.evidence_needs
                       if is_blocking(n) and n.status == 'pending']
            self.event('collection_follow_up', role, reason='unresolved_core_needs',
                       need_ids=[n['need_id'] for n in pending])
            return (
                '准备进入诊断，但这些关键问题仍待评估：' + json.dumps(pending, ensure_ascii=False)
                + '。声明的检查缺口：' + json.dumps(self.task_gaps(), ensure_ascii=False)
                + '。先用已有观测评估其含义；合理替代解释确实不影响判断时，可说明理由后not_required。'
                '若还缺有区分价值的观测且预算允许，选择一到两个未执行的登记只读检查，'
                '用任务need_ids关联缺口，expected_value说明不同结果如何改变判断。'
                '参考next_check及collection，已完整查空不能当作未查询或再次派相同检查。'
                '无法查询、需要人工操作或没有有价值的新检查时，明确原因并保留缺口收尾。'
                '不得为了通过而标supported，不必遍历所有渠道。此选择提醒全运行最多一次，仍返回完整JSON。')
        if role == 'reviewer' and schema is ReviewDecision and self.needs_review_follow_up(result):
            self.review_follow_up_used = True
            self.event('review_follow_up_correction', role,
                target_ids=[a.target_id for a in result.assessments
                    if a.target_id.startswith(('H', 'A')) and a.verdict == 'uncertain'])
            remaining = self.context.limits.model_calls - self.context.counts['model_calls'] - 1
            return (
                '你已指出H/A或共享证据需求的关键缺口，却没有提出补查或解释为何不能补查。'
                '请用request_evidence说明缺少哪个因果环节或操作条件，以及什么证据能改变判断，'
                '交给Supervisor规划具体补查；无需提供工具参数。关联已有need_id或H/A编号，'
                '只涉及建议条件时target_id选择该A编号，已支持的H可以保持supported。'
                '若没有有用的只读检查、需要人工变更实验或条件不影响当前结论，'
                '保留原评估并填写follow_up_reason；不得为完成流程编造检查或强行通过。'
                + '本轮程序校验后的缺口：' + json.dumps(result.missing_information, ensure_ascii=False)
                + '。'
                f'本次回复后最多还剩{remaining}次模型请求，补查仍受原收尾预留与一轮上限约束。仅返回完整JSON。')
        return None

    def role_failure(self, role, schema, exc, details, value):
        if role == 'diagnosis' and isinstance(exc, ProtocolError) and exc.code in {
                'READ_ONLY_ACTION_ALREADY_COVERED', 'READ_ONLY_ACTION_INVALID', 'ACTION_KIND_MISMATCH'}:
            preserved = preserve_diagnosis(value, self)
            self.event('diagnosis_actions_quarantined', role,
                       codes=[row['code'] for row in preserved.rejected_actions],
                       count=len(preserved.rejected_actions))
            return preserved
        if isinstance(exc, ProtocolError):
            locations = '、'.join(str(item.get('path', '')) for item in exc.details if item.get('path'))
            self.extra_gaps.append(f'{role}业务校验未通过（{exc.code}）'
                + (f'：{locations}' if locations else '')
                + '；本阶段的纠正额度已用尽，已有观测与有效草稿保留，未通过结果不作为已复核结论。')
        if role == 'diagnosis' and details['reason'] in {'UNKNOWN_REFERENCE', 'DUPLICATE_REFERENCE'}:
            label = '引用不在本次可用列表中' if details['reason'] == 'UNKNOWN_REFERENCE' else '同一引用列表内存在重复编号'
            location = details['details'][0]['path']
            self.extra_gaps.append(f'诊断引用校验未通过（{details["reason"]}）：{location}，{label}；取证与诊断共享的纠正额度已用尽。')
        if role == 'supervisor':
            self.last_supervisor_failure = details['reason']
        return None

    def expand_refs(self, refs):
        return expand_refs(refs, self)

    def diagnosis(self, selection):
        if self.ticket.purpose == 'status_check' and (selection.hypotheses or selection.recommended_actions):
            raise ProtocolError('STATUS_CHECK_SCOPE', [{'purpose': 'status_check',
                'instruction': '状态核查只输出观测和信息缺口，hypotheses、recommended_actions 均使用空列表。'}])
        if self.limited_handoff and selection.hypotheses:
            raise ProtocolError('HANDOFF_CAUSE_NOT_OBSERVED', [{
                'path': 'hypotheses', 'instruction': '受限交接没有当前业务观测，hypotheses必须为空；只整理观测缺口、人工只读核查和真实接手人。'}])
        if self.limited_handoff and any(a.kind != 'manual_check' for a in selection.recommended_actions):
            raise ProtocolError('HANDOFF_READ_ONLY_REQUIRED', [{
                'path': 'recommended_actions', 'instruction': '受限交接只允许manual_check、checks=[]、repair=null；不得提出变更或重复已失败的程序查询。'}])
        draft = expand_diagnosis(selection, self)
        if (self.limited_handoff and draft.escalation_team is not None
                and draft.escalation_team != owner_choice(self.evidence, self.ticket.scope.service,
                    self.executors['investigation'].check_results.values()).team):
            raise ProtocolError('HANDOFF_OWNER_UNCERTAIN', [{
                'instruction': '负责人查询有冲突或截断，escalation_ref必须为null；保留人工确认接手人的缺口。'}])
        return draft

    def targets(self):
        return {f"{prefix}{n}": item for prefix, items in (("F", self.draft.findings),
            ("H", self.draft.hypotheses), ("A", self.draft.recommended_actions)) for n, item in enumerate(items, 1)}

    def diagnosis_completion_gaps(self):
        # Reviewed facts alone complete a status check, not an incident diagnosis.
        if self.ticket.purpose != 'diagnosis' or self.draft is None:
            return []
        return diagnosis_gaps(self.draft, self.ticket.purpose)

    def finish_review(self):
        gaps = self.diagnosis_completion_gaps()
        self.extra_gaps.extend(gaps)
        self.stop = completion_reason(self.draft, self.review, self.ticket.purpose,
            diagnosis_gaps=gaps, observation_gaps=self.observation_gaps(), task_gaps=self.task_gaps(),
            blocking_gaps=limitations(self.evidence_needs, blocking_only=True))
        if self.stop == 'DIAGNOSIS_INCOMPLETE':
            self.event('diagnosis_gate', 'diagnosis', code=self.stop, missing_information=gaps)
        self.check_diagnostic_pack_exit('review_completion')

    def check_diagnostic_pack_exit(self, stage):
        if self.diagnostic_pack is None:
            return None
        from workflow.diagnostic_pack import check_exit
        return check_exit(self, stage)

    def diagnostic_pack_input(self):
        if self.diagnostic_pack is None:
            return {}
        from workflow.diagnostic_pack import state_input
        return state_input(self)

    def reviewed_gaps(self):
        return [*self.diagnosis_completion_gaps(),
                *readiness_gaps(self.draft, self.review, self.ticket.purpose),
                *self.observation_gaps(), *self.task_gaps(),
                *limitations(self.evidence_needs, blocking_only=True)]

    def reviewer_input(self):
        targets = {}
        for key, item in self.targets().items():
            value = item.model_dump(mode="json")
            # A previous model's verdict/explanation is not evidence. The claim,
            # selected fields and its stated unknowns remain available.
            value.pop('status', None)
            explanation = value.pop('evidence_explanation', None)
            if explanation is not None:
                value['claim_explanation_to_audit'] = explanation
            for field in ("refs", "support_refs", "counter_refs"):
                if field in value:
                    value[field] = [{"evidence_id": r["evidence_id"], "field_path": r["field_path"]} for r in value[field]]
            targets[key] = value
        # Raw evidence and mechanical coverage remain visible; previous model
        # verdicts and candidate summaries are not answers for this reviewer.
        needs = [{k: row[k] for k in ('need_id', 'question', 'purpose')}
                 for row in self.need_input()]
        scopes = {
            'mechanism': '评估当前如何发生；未知触发来源另留限制，不要求唯一深层根因。',
            'alternative': '只阻塞有当前线索且会改变机制或下一步的竞争机制；trigger来源未知与A措辞预设结果不属于竞争机制，不要求预先完成人工核查。',
            'trigger': '评估为何开始发生；只有影响当前下一步适用性时才阻塞，并关联未支持A。',
            'action_condition': '区分核查为何值得做与变更前提是否满足；核查结果未知不等于核查待办不成立。',
        }
        for need in needs:
            need['blocking_default'] = is_blocking(self.evidence_needs[need['need_id']])
            if need['purpose'] in scopes:
                need['review_scope'] = scopes[need['purpose']]
        tasks = [{k: row[k] for k in ('task_id', 'role', 'goal', 'need_ids', 'completion',
                                    'query_state_source', 'model_summary_verified')}
                 for row in self.task_input()]
        rework_budget = self.rework_budget(pending_model_calls=1, additional=self.reworks == 1)
        return {"targets": targets, "required_target_ids": list(targets),
                'claim_explanation_policy': 'claim_explanation_to_audit是待核验的原始陈述，不是来源或既有通过结论；连同cause中的因果、排除范围和查询叙述逐项复核。',
                **self.diagnostic_pack_input(),
                'evidence_needs': needs, 'required_need_ids': list(self.evidence_needs),
                'uncertain_need_rule': ('受限交接中核心symptom/mechanism没有业务观测，只能uncertain，不能用owner支持；保留原因，不要求给已支持的人工待办附加冲突blocker，不再规划补查。'
                    if self.limited_handoff else 'supported必须引用与此问题相关的当前观测；缺引用或仅历史线索会被原门禁降级uncertain，不能只凭窗口覆盖或查询完成宣告支持。'
                    '每个uncertain且阻塞的问题须给自己的blocker，或由request_evidence.need_id明确指向同一个N。'
                    '核心symptom/mechanism始终阻塞；其它问题blocking省略/null使用blocking_default，false须有非阻塞理由。'
                    'blocker填写affected_targets、missing_observation、decision_impact、resolution；reason不能替代这些字段。'
                    'cause_level由程序绑定，不输出；不得为满足格式改判supported。'),
                'core_need_ids': [n.need_id for n in self.evidence_needs.values()
                                  if n.purpose in {'symptom', 'mechanism'}],
                'independent_observation_review': self.observation_reviews[-1]['review']
                    if self.observation_reviews and self.observation_reviews[-1]['revision'] == len(self.drafts) else None,
                "purpose": self.ticket.purpose,
                'limited_handoff': self.limited_handoff,
                "diagnosis_completion_gaps": self.diagnosis_completion_gaps(),
                "ticket": self.ticket.model_dump(mode='json', exclude={'scope': {'tenant_id', 'user_id'}}),
                "task_results": tasks, "task_completion_gaps": self.task_gaps(),
                "read_only_capabilities": {r: ex.capabilities() for r, ex in self.executors.items()},
                "repair_capabilities": getattr(self.provider, 'repair_capabilities', []),
                "evidence": self.sources(), "collection": self.collection(), "rework_used": self.reworks,
                "prior_challenge": self.pending, "observation_gaps": self.observation_gaps(),
                "budget": {key: rework_budget[key] for key in
                           ('can_collect', 'remaining_model_calls', 'remaining_tools')}}

    def budget_view(self):
        # Read current runtime counters; WorkflowState never owns their copies.
        with self.context.lock:
            counts = dict(self.context.counts)
        return BudgetView(self.context.limits, counts['model_calls'], counts['tool_calls'],
            time.monotonic() - self.context.started, self.supervisor_calls, len(self.tasks),
            self.reworks, self.additional_rework_authorized)

    def rework_budget(self, *, pending_model_calls=0, tool_calls=1, supervisor_completed=False, additional=False):
        return self.policy.rework_budget(self.budget_view(), pending_model_calls=pending_model_calls,
            tool_calls=tool_calls, supervisor_completed=supervisor_completed, additional=additional)

    def additional_rework_eligible(self):
        return additional_rework_eligible(self.review_routing_context())

    def can_replan(self):
        return self.policy.can_replan(self.budget_view(),
            dispatch_capacity=self.dispatch_capacity(pending_model_call=1))

    def needs_collection_follow_up(self, decision):
        if self.planning_only:
            return False  # Pending semantic questions are for Diagnosis/Reviewer.
        return (self.ticket.purpose == 'diagnosis' and self.phase == 'collect'
            and decision.action == 'diagnose' and not self.collection_follow_up_used
            and (any(is_blocking(n) and n.status == 'pending' for n in decision.evidence_needs)
                 or bool(self.task_gaps()))
            and self.can_replan())

    def planning_guidance(self):
        unresolved = []
        for need in self.evidence_needs.values():
            if not is_blocking(need) or need.status in {'supported', 'not_required'}:
                continue
            proposal = need.next_check.model_dump(mode='json') if need.next_check else None
            unresolved.append({'need_id': need.need_id, 'purpose': need.purpose,
                'question': need.question, 'status': need.status, 'reason': need.reason,
                'next_check': proposal,
                'adds_information': adds_information([proposal], self.executors[need.role]) if proposal else None,
                'role': need.role})
        return {'unresolved_core_needs': unresolved,
                'log_query_feedback': log_query_feedback(self.executors['investigation']),
                'recent_reused_requests': self.reused_requests[-2:],
                'follow_up_used': self.collection_follow_up_used,
                'instruction': ('问题pending表示等待Diagnosis/Reviewer评估；Supervisor只用need_ids关联有区分价值的新检查。'
                    '已有观测且无有价值的新查询即可diagnose，真实取证限制交给诊断和复核。' if self.planning_only else
                    '已有观测先评估；待查问题用need_ids关联新检查，expected_value说明区分价值。'
                    '完整空查询保留范围限制，避免重复；无法获取新信息时明确原因收尾，替代解释可有理由地not_required。')}

    def collection_coverage(self):
        return {'questions': self.need_input(),
                **self.diagnostic_pack_input(),
                'pending_blocking_ids': [n.need_id for n in self.evidence_needs.values()
                    if is_blocking(n) and n.status == 'pending'],
                'declared_check_gaps': self.task_gaps(),
                'meaning': '问题覆盖不同于工具调用数量；空查询只覆盖其窗口和过滤条件，不证明原因或整体健康。'}

    def prepare_diagnosis_entry(self):
        """Every graph path to diagnosis must account for unresolved coverage.

        The Supervisor already receives one bounded coverage reminder. Every
        other exit (failure, no progress, budget) also gets explicit limitations.
        This checks ledger states, not the semantic truth of a model verdict.
        """
        pack_exit = self.check_diagnostic_pack_exit('diagnosis_entry')
        if pack_exit is not None and not pack_exit['allowed']:
            return False
        coverage = self.collection_coverage()
        if self.planning_only:
            declared_incomplete = bool(coverage['declared_check_gaps'] or self.observation_gaps())
            pack_limited = bool(pack_exit and pack_exit['limitations'])
            limited = declared_incomplete or pack_limited
            snapshot = {'revision': len(self.drafts) + 1, 'phase': self.phase,
                        'reason': 'declared_checks_incomplete' if declared_incomplete else
                                  'baseline_collection_limits' if pack_limited else 'collected_for_review',
                        'limited': limited, **coverage}
            self.collection_exits.append(snapshot)
            self.event('collection_coverage_gate', **snapshot)
            return True
        pending = coverage['pending_blocking_ids']
        reason = ('critical_questions_unresolved' if pending else
                  'declared_checks_incomplete' if coverage['declared_check_gaps'] else
                  'explicit_limitations' if limitations(self.evidence_needs, blocking_only=True) else
                  'questions_assessed')
        for need_id in pending:
            need = self.evidence_needs[need_id]
            self.evidence_needs[need_id] = need.model_copy(update={'status': 'deferred',
                'reason': ('关键问题仍未完成评估；预算/限次范围内未形成有效的新补查计划，'
                           '带限制进入诊断，不视为已取证充分。' + need.reason)[:400]})
        snapshot = {'revision': len(self.drafts) + 1, 'phase': self.phase,
                    'reason': reason, 'limited': reason != 'questions_assessed',
                    **self.collection_coverage()}
        self.collection_exits.append(snapshot)
        self.event('collection_coverage_gate', **snapshot)
        return True

    def needs_review_follow_up(self, review):
        if self.limited_handoff:
            return False
        return needs_review_follow_up(self.review_routing_context(review),
            can_collect=lambda: self.rework_budget(pending_model_calls=1)['can_collect'])

    def collection(self):
        # Empty/truncated/error results remain visible to the tool-free judging roles.
        return [{k: event[k] for k in ("role", "name", "status", "query_profile", "truncated", "evidence_ids", "error")}
                for event in self.events if event["type"] == "tool_result"]

    def review_context(self):
        return ReviewContext(needs=self.evidence_needs, evidence=self.evidence, references=self.references,
            executors=self.executors, targets=self.targets(), scope=self.ticket.scope,
            observation_gaps=self.observation_gaps(), protection=self.review_protection,
            require_blockers=not self.limited_handoff and (self.blind_review or getattr(self.models['reviewer'], 'json_output', False)),
            existing_gaps=tuple(self.extra_gaps))

    def review_routing_context(self, review=None):
        return ReviewRoutingContext(draft=self.draft, review=self.review if review is None else review,
            needs=self.evidence_needs, targets=self.targets() if self.draft is not None else {},
            purpose=self.ticket.purpose, protection=self.review_protection, reworks=self.reworks,
            protocol_fallback_used=self.protocol_fallback_used, clarification_used=self.review_follow_up_used)

    def validate_review(self, review):
        if self.limited_handoff and any(a.need_id in self.evidence_needs
                and self.evidence_needs[a.need_id].purpose in {'symptom', 'mechanism'}
                and a.verdict != 'uncertain' for a in review.need_assessments):
            raise ProtocolError('HANDOFF_CORE_UNRESOLVED', [{
                'instruction': '受限交接没有当前业务观测，核心symptom/mechanism只能uncertain，不能用负责人、查询错误或人工待办关闭。'}])
        audit = ReviewAudit(hint_status=list(getattr(self, 'review_hint_status', [])))
        try:
            return validate_review(review, self.review_context(), audit=audit, emit=self.event)
        finally:
            # Diagnostic effects survive failed candidates; verdict/need state is not committed here.
            self.review_hint_status = audit.hint_status
            self.extra_gaps.extend(audit.limitations)

    def diagnose(self):
        if not self.prepare_diagnosis_entry():
            return
        self.review = None  # A previous revision's review never approves a new draft.
        payload = {"ticket": self.ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
                   **self.diagnostic_pack_input(),
                   'diagnosis_policy': review_policy(),
                   'evidence_needs': self.need_input(),
                   "purpose": self.ticket.purpose,
                   'limited_handoff': self.limited_handoff,
                   "completion_requirements": {'required_sections': required_sections(self.ticket.purpose),
                       'insufficient_evidence': '保留缺失项并说明具体信息缺口，不编造原因或不适用的操作；故障诊断会以 partial 结束。',
                       'read_only_checks': '只提出未被collection完整覆盖的新检查；同参数失败重试不是新信息。'},
                   "evidence": self.sources(), "collection": self.collection(), "challenge": self.pending,
                   "observation_gaps": self.observation_gaps(),
                   "repair_capabilities": getattr(self.provider, 'repair_capabilities', []),
                   "read_only_capabilities": {r: ex.capabilities() for r, ex in self.executors.items()},
                   "read_only_tools": {r: ex.schemas() for r, ex in self.executors.items()},
                   "task_results": self.task_input(), "task_completion_gaps": self.task_gaps()}
        self.draft = self.ask("diagnosis", DiagnosisSelection, payload, terminal=True, validate=self.diagnosis)
        revision = len(self.drafts) + 1
        self.drafts.append({"revision": revision, "draft": self.draft.model_dump(mode="json")})
        for h in self.draft.hypotheses:
            self.note("revise" if revision > 1 else "propose_hypothesis", "diagnosis", "reviewer",
                      hypothesis_id=h.hypothesis_id, evidence_ids=[r.evidence_id for r in h.support_refs], reason=h.cause)
        if not self.targets():
            self.extra_gaps.extend(self.diagnosis_completion_gaps())
            self.stop = "NEEDS_INFORMATION"
            return
        self.next_node = 'reviewer'

    def review_draft(self):
        revision = len(self.drafts)
        if self.blind_review and self.ticket.purpose == 'diagnosis' and not self.limited_handoff:
            remaining = self.context.limits.model_calls - self.context.counts['model_calls']
            if remaining >= 2:
                self.event('observation_review_started', 'reviewer', revision=revision, draft_visible=False)
                observed = self.ask('reviewer', ObservationReview, {
                    'ticket': self.ticket.model_dump(mode='json', exclude={'scope': {'tenant_id', 'user_id'}}),
                    'evidence': self.sources(), 'collection': self.collection()},
                    terminal=True, validate=lambda value: validate_observation_review(value, self.review_context()),
                    system_prompt=OBSERVATION_REVIEW)
                self.observation_reviews.append({'revision': revision, 'review': observed.model_dump(mode='json')})
                self.event('observation_review_completed', 'reviewer', revision=revision, draft_visible=False)
            else:
                self.event('observation_review_skipped', 'reviewer', code='BUDGET_EXCEEDED', revision=revision)
        self.review = self.ask("reviewer", ReviewDecision, self.reviewer_input(), terminal=True, validate=self.validate_review)
        self.evidence_needs = reviewed_needs(self.evidence_needs, self.review.need_assessments)
        handoff = resolve_handoff(self.review_routing_context(), reviewed_gaps=self.reviewed_gaps)
        request = handoff.request
        if handoff.deferred_request:
            deferred = {'revision': revision, 'request': handoff.deferred_request.model_dump(mode='json'),
                        'reason': handoff.reason}
            self.deferred_review_requests.append(deferred)
            self.extra_gaps.append(handoff.limitation)
            self.event('review_request_deferred', 'reviewer', **deferred, verdicts_changed=False)
        self.event('evidence_needs_reviewed', 'reviewer', revision=revision,
                   evidence_needs=self.need_input(), blocking_gaps=limitations(self.evidence_needs, blocking_only=True))
        self.reviews.append({"revision": revision, "review": self.review.model_dump(mode="json")})
        self.event("review_completed", "reviewer", revision=revision,
                   follow_up_reason=self.review.follow_up_reason,
                   status="needs_rework" if request else
                   "passed" if not self.reviewed_gaps() else "needs_information")
        if request:
            if self.limited_handoff:
                self.extra_gaps.append('受限交接未进行额外补查：' + request.missing_observation)
                self.finish_review()
                self.stop = 'NEEDS_INFORMATION'
                return
            self.note("challenge", "reviewer", "diagnosis", hypothesis_id=request.hypothesis_id,
                      evidence_ids=request.evidence_ids, reason=request.missing_observation)
            decision = decide_rework(self.review_routing_context(),
                can_collect=lambda additional: self.rework_budget(additional=additional)['can_collect'])
            if decision.kind == 'protocol_stop':
                self.event('rework_denied', 'reviewer', code='PROTOCOL_FINALIZATION_ONLY')
                self.extra_gaps.append('调度协议失败后仅收尾；复核提出的补查仍未执行：' + request.missing_observation)
                self.finish_review()
                # A pending executable check must not be disguised as completion.
                self.stop = 'REVIEW_INCOMPLETE'
            elif decision.kind == 'limit_stop':
                self.event("rework_denied", "supervisor", code="REWORK_LIMIT")
                self.stop = "REWORK_LIMIT"
            elif decision.kind == 'budget_stop':
                self.event('rework_denied', code='BUDGET_EXCEEDED')
                self.extra_gaps.append('复核缺口未补查：预算或调度额度不足；' + request.missing_observation)
                self.finish_review()
                self.stop = 'REVIEW_INCOMPLETE'
            else:
                if decision.authorize_additional:
                    self.additional_rework_authorized = True
                    self.event('additional_rework_authorized', need_id=request.need_id,
                               target_id=request.target_id, maximum_total_reworks=2, total_budget_changed=False)
                self.pending = {"challenge_id": f"C{self.reworks + 1}", **request.model_dump(),
                                'planning_required': True, 'hint_status': self.review_hint_status}
                self.note("request_evidence", "reviewer", "supervisor", hypothesis_id=request.hypothesis_id,
                          evidence_ids=request.evidence_ids, reason=request.missing_observation, check=request.proposed_check)
                self.phase = "rework"
                self.next_node = 'supervisor'
        else:
            self.pending = None
            self.finish_review()
            if self.limited_handoff:
                self.stop = 'NEEDS_INFORMATION'
            self.note("accept" if self.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} else "escalate", "reviewer", "supervisor")

    def diagnose_and_review(self):
        """Compatibility helper for direct callers; graph uses separate role nodes."""
        self.diagnose()
        if not self.stop and self.draft is not None:
            self.review_draft()

    def prepare_dispatch(self, decision):
        if not decision.tasks or len(self.tasks) + len(decision.tasks) > self.policy.task_limit(self.additional_rework_authorized):
            raise ProtocolError("TASK_LIMIT")
        if self.phase == "rework" and len(decision.tasks) != 1:
            raise ProtocolError("INVALID_REWORK_DISPATCH")
        proposed = []
        for task in decision.tasks:
            self.evidence_ids(task.evidence_ids)
            if self.phase != 'rework' and self.reuse_task(task):
                continue
            proposed.append(task)
        if not proposed:
            self.finish_collection(reused_only=any(completion(
                [c.model_dump(mode='json') for c in task.checks], self.executors[task.role])['limitations']
                for task in decision.tasks))
            return
        keys = []
        for task in proposed:
            self.evidence_ids(task.evidence_ids)
            key = (task.role, " ".join(task.goal.casefold().split()), tuple(sorted(task.evidence_ids)))
            mode = self.investigation_mode if task.role == 'investigation' else self.knowledge_mode
            if mode == 'programmatic':
                key += (tuple(sorted((check.tool, json.dumps(check.args, sort_keys=True)) for check in task.checks)),)
            if key in self.seen_tasks or key in keys:
                raise ProtocolError("DUPLICATE_TASK")
            keys.append(key)
        capacity = self.dispatch_capacity()
        if capacity < 1 or not self.context.can_research():
            self.event("rework_denied" if self.phase == "rework" else "dispatch_denied", code="BUDGET_EXCEEDED",
                       requested_roles=[t.role for t in decision.tasks], available_tasks=capacity)
            if self.phase == "collect" and any(e.kind == "observation" for e in self.evidence.values()):
                self.event("finalization_started", reason="research_budget_unavailable")
                self.next_node = 'diagnosis'
                return
            raise ExecutionError("BUDGET_EXCEEDED")
        selected = list(zip(proposed, keys))
        if self.ticket.purpose == 'diagnosis' and self.phase != 'rework':
            selected.sort(key=lambda pair: (pair[0].role != 'investigation',
                not any(self.evidence_needs[n].purpose in {'symptom', 'mechanism'}
                        for n in pair[0].need_ids)))
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
            task = selected[0][0]
            self.pending = {**self.pending, 'planning_required': False,
                'suggested_checks': self.pending['checks'],
                'checks': [c.model_dump(mode='json') for c in task.checks],
                'target_role': task.role, 'expected_value': task.expected_value}
            self.event('rework_plan_approved', need_id=self.pending.get('need_id'),
                       checks=self.pending['checks'], expected_value=task.expected_value,
                       target_role=task.role)
            self.reworks += 1
        self.dispatch_previous = set(self.evidence)
        self.dispatch_reads = {(role, key) for role, ex in self.executors.items() for key in ex.check_results}
        self.dispatch_follow_up = follow_up
        self.dispatch_queue = [task for task, _ in selected]
        self.next_node = ('collect_parallel' if self.parallel_collection and not follow_up and
            self.context.limits.tool_calls - self.context.counts['tool_calls'] >= 4 and
            {t.role for t in self.dispatch_queue} == {'investigation', 'knowledge'}
            else self.dispatch_queue[0].role)

    def reuse_task(self, task):
        checks = [c.model_dump(mode='json') for c in task.checks]
        executor = self.executors[task.role]
        if not checks or adds_information(checks, executor):
            return False
        coverage = completion(checks, executor)
        self.event('task_reused', task.role, goal=task.goal, reason='no_new_read',
                   completion_status=coverage['status'], check_statuses=[c['status'] for c in coverage['checks']])
        self.reused_requests.append({'role': task.role, 'goal': task.goal, 'checks': checks})
        return True

    def finish_collection(self, *, reused_only=False):
        if self.dispatch_queue:
            self.next_node = self.dispatch_queue[0].role
        elif (reused_only and self.phase == 'collect' and self.ticket.purpose == 'diagnosis'
              and not self.collection_follow_up_used
              and any(is_blocking(n) and n.status == 'pending' for n in self.evidence_needs.values())
              and self.can_replan()):
            self.collection_follow_up_used = True
            self.event('collection_follow_up', reason='no_new_read',
                       need_ids=[n.need_id for n in self.evidence_needs.values()
                                 if is_blocking(n) and n.status == 'pending'])
            self.next_node = 'supervisor'
        elif any(e.kind == 'observation' for e in self.evidence.values()):
            self.next_node = 'diagnosis'
        else:
            self.stop = 'NEEDS_INFORMATION'

    def task_objective(self, task, follow_up):
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
        approved_checks = [c for c in self.pending['checks']] if follow_up else None
        required_checks = approved_checks or [c.model_dump(mode='json') for c in task.checks]
        need_ids = list(dict.fromkeys([*task.need_ids,
            *([self.pending['need_id']] if follow_up and self.pending.get('need_id') else [])]))
        return {"goal": ('Perform the approved read-only checks.' if follow_up else task.goal),
            'need_ids': need_ids, 'evidence_needs': self.need_input(),
            "expected_value": self.pending['expected_value'] if follow_up else task.expected_value,
            "read_only_capabilities": self.executors[task.role].capabilities(),
            "approved_checks": approved_checks, "required_checks": required_checks,
            "check_completion": completion(required_checks, self.executors[task.role]),
            "evidence": prior, "focused_evidence_ids": ids,
            "collection": self.collection()[-12:], "observation_gaps": self.observation_gaps(),
            "challenge": self.pending if follow_up else None}

    def collection_runner(self):
        return CollectionRunner(self.models, self.provider, self.principal, self.repairs,
            modes={'investigation': self.investigation_mode, 'knowledge': self.knowledge_mode})

    def collect_task(self, task, objective, executor, context, events, steps):
        mode = self.investigation_mode if task.role == 'investigation' else self.knowledge_mode
        request = CollectionRequest(task, objective, mode, steps)
        return self.collection_runner().collect(request, executor=executor, context=context,
                                                 event_log=events).result

    def execute_planned_reads(self, objective, executor, context, *, role='investigation'):
        return execute_planned_reads(objective, executor, context, role=role)

    def record_task(self, task, objective, result):
        mode = self.investigation_mode if task.role == 'investigation' else self.knowledge_mode
        outcome = CollectionOutcome(CollectionRequest(task, objective, mode, 0), result, self.executors[task.role])
        self.record_collection(outcome)

    def record_collection(self, outcome):
        role, result = outcome.request.task.role, outcome.result
        challenge_id = self.pending['challenge_id'] if self.dispatch_follow_up else None
        self.tasks.append(task_record(outcome, challenge_id))
        coverage = outcome.coverage
        self.event('task_coverage', role, task_id=result['task_id'],
                   execution_status=result['status'], completion_status=coverage['status'],
                   check_statuses=[c['status'] for c in coverage['checks']])
        self.evidence.update({k: v.model_copy(deep=True) for k, v in outcome.evidence.items()})
        self.references.update({k: v.model_copy(deep=True) for k, v in outcome.references.items()})
        if self.diagnostic_pack_result is not None:
            from workflow.diagnostic_pack import refresh
            refresh(self)

    def execute_task(self, role):
        task = self.dispatch_queue.pop(0)
        if task.role != role:
            raise ProtocolError('INVALID_TASK_ROUTE')
        follow_up = self.dispatch_follow_up
        if not follow_up and self.reuse_task(task):
            self.finish_collection()
            return
        if not self.context.can_research():
            self.event('task_deferred', task.role, reason='research_budget_unavailable')
            if not follow_up and any(e.kind == 'observation' for e in self.evidence.values()):
                self.event('finalization_started', reason='research_budget_unavailable')
                self.dispatch_queue.clear()
                self.next_node = 'diagnosis'
                return
            raise ExecutionError('BUDGET_EXCEEDED')
        objective = self.task_objective(task, follow_up)
        reserve = self.context.limits.reserve_model_calls if follow_up else self.collection_reserve()
        available_steps = self.context.limits.model_calls - reserve - self.context.counts['model_calls']
        steps = min(self.policy.collector_steps, available_steps - 2 * len(self.dispatch_queue))
        collector = self.collection_runner()
        request = CollectionRequest(task, objective, collector.modes[role], steps)
        outcome = collector.collect(request, executor=self.executors[role], context=self.context,
                                    event_log=self.events)
        result = outcome.result
        self.record_collection(outcome)
        if result['run_summary']['termination_reason'] == 'NO_TOOL_PROGRESS':
            self.event('collection_stalled', task.role, reason='declared_checks_not_executed', task_id=result['task_id'])
            for deferred in self.dispatch_queue:
                self.event('task_deferred', deferred.role, reason='collection_stalled')
            self.dispatch_queue.clear()
            if any(e.kind == 'observation' for e in self.evidence.values()):
                self.event('finalization_started', reason='leaf_no_tool_progress')
                self.next_node = 'diagnosis'
            else:
                self.stop = 'NO_TOOL_PROGRESS'
                self.extra_gaps.extend(result['output']['missing_information'])
            return
        if self.dispatch_queue:
            self.next_node = self.dispatch_queue[0].role
            return
        new_reads = {(role, key) for role, ex in self.executors.items() for key in ex.check_results} - self.dispatch_reads
        if not follow_up and self.dispatch_previous and not new_reads:
            self.event('collection_stalled', reason='no_new_evidence')
            self.next_node = 'diagnosis'
        else:
            self.next_node = 'diagnosis' if follow_up else 'supervisor'

    def prepare_parallel(self):
        if self.dispatch_follow_up:
            raise ProtocolError('INVALID_TASK_ROUTE')
        batch = self.collection_runner().prepare_parallel(self.dispatch_queue, context=self.context,
            executors=self.executors, model_reserve=self.collection_reserve(),
            objective_for=lambda task: self.task_objective(task, False))
        self.parallel_batch, self.parallel_plans = batch.batch_id, batch.plans
        self.parallel_batches += 1
        self.event('parallel_collection_started', batch_id=self.parallel_batch,
            roles=list(self.parallel_plans), allocations={role: {'model_calls': plan.request.steps,
                'tool_calls': plan.context.tool_cap} for role, plan in self.parallel_plans.items()})

    def collect_parallel_role(self, role):
        return self.collection_runner().collect_parallel(self.parallel_plans[role], self.parallel_batch)

    def merge_parallel(self, outcomes):
        joined = join_parallel(self.parallel_batch, self.parallel_plans, outcomes)
        for outcome in joined.collections:
            role, result = outcome.request.task.role, outcome.result
            # Only the join installs branch ledgers and commits cross-stage state.
            outcome.executor.context = self.context
            self.executors[role] = outcome.executor
            self.record_collection(outcome)
            if result['status'] != 'completed':
                code = result['run_summary']['termination_reason']
                self.extra_gaps.extend(result['output']['missing_information'])
                self.event('parallel_branch_incomplete', role, batch_id=self.parallel_batch, code=code)
        self.event('parallel_collection_joined', batch_id=self.parallel_batch,
                   roles=list(self.parallel_plans), incomplete=joined.incomplete)
        self.dispatch_queue.clear()
        self.parallel_batch = None
        self.parallel_plans = {}
        # Failed branches retain sibling evidence; history alone cannot establish a current diagnosis.
        if joined.fatal_code:
            self.stop = joined.fatal_code
        elif joined.incomplete:
            if any(e.kind == 'observation' for e in self.evidence.values()):
                self.next_node = 'diagnosis'
            else:
                self.stop = joined.stop_codes[0] if joined.stop_codes else 'NEEDS_INFORMATION'
        else:
            self.next_node = 'supervisor'

    def collection_reserve(self):
        return self.policy.collection_reserve(self.context.limits, self.ticket.purpose)

    def dispatch_capacity(self, pending_model_call=False):
        checks = 0
        if self.phase == 'rework':
            checks = 1 if self.pending.get('planning_required') else len(self.pending['checks'])
        return self.policy.dispatch_capacity(self.budget_view(), phase=self.phase,
            purpose=self.ticket.purpose, pending_checks=checks,
            has_observations=any(e.kind == 'observation' for e in self.evidence.values()),
            pending_model_call=pending_model_call)

    def prepare_limited_handoff(self):
        """One bounded closure after actual unavailable/empty business reads."""
        collection = self.collection()
        if (self.limited_handoff or not self.planning_only or self.phase != 'collect'
                or self.ticket.purpose != 'diagnosis' or self.draft is not None
                or has_business_observation(self.evidence)
                or not any(c['name'] in {'get_service_metrics', 'get_service_logs'} for c in collection)
                or fatal_collection_code(collection)):
            return False
        self.limited_handoff = True
        alias = self.ticket.scope.service
        choice = owner_choice(self.evidence, alias, self.executors['investigation'].check_results.values())
        executor = self.executors['investigation']
        check = {'tool': 'get_service_owner', 'args': {'alias': alias}}
        # Never spend the last judging turns on owner lookup, or retry an attempted read.
        if (choice.status == 'missing' and can_finalize(self.context)
                and completion([check], executor)['checks'][0]['status'] == 'not_executed'
                and self.context.can_research()):
            task = TaskRequest(role='investigation', goal='取得本工单登记负责人以交接观测缺口',
                checks=[check], expected_value='只确认接手团队，不支持业务故障原因')
            objective = {**self.task_objective(task, False), 'execution_source': 'limited_handoff_policy'}
            child = executor.fork(self.context)
            child.provider = SingleAttemptOwnerProvider(executor.provider)
            try:
                outcome = self.collection_runner().collect(CollectionRequest(task, objective, 'programmatic', 0),
                    executor=child, context=self.context, event_log=self.events)
            finally:
                child.provider = executor.provider
            self.executors['investigation'] = child
            self.record_collection(outcome)
            choice = owner_choice(self.evidence, alias, self.executors['investigation'].check_results.values())
        self.team = choice.team  # Exact current-service observation, or explicitly unknown.
        self.extra_gaps.extend(handoff_notes(self.ticket, choice))
        self.event('limited_handoff_prepared', owner_status=choice.status, team_observed=bool(choice.team),
                   can_finalize=can_finalize(self.context), cause_confirmation='not_performed')
        fatal = fatal_collection_code(self.collection())
        if fatal:
            self.stop = fatal
        elif can_finalize(self.context):
            self.next_node = 'diagnosis'
        else:
            self.extra_gaps.append('收尾预算不足，人工待办未经过模型复核，不作为已支持建议。')
            self.stop = 'NEEDS_INFORMATION'
        return True

    def supervise(self):
        if (self.phase == 'collect' and self.ticket.purpose == 'diagnosis'
                and any(e.kind == 'observation' for e in self.evidence.values())
                and self.collection_reserve() > self.context.limits.reserve_model_calls
                and (self.supervisor_calls >= self.policy.replan_supervisor_limit
                    or self.context.limits.model_calls - self.context.counts['model_calls']
                        <= self.collection_reserve())):
            self.event('collection_reserve_reached', remaining_model_calls=
                self.context.limits.model_calls - self.context.counts['model_calls'],
                protected_model_calls=self.collection_reserve(),
                supervisor_slot_reserved=True, tool_slot_reserved=True)
            self.next_node = 'diagnosis'
            return
        if self.supervisor_calls >= self.policy.supervisor_limit(self.additional_rework_authorized):
            self.stop = "SUPERVISOR_LIMIT"
            return
        reusable = self.pending_rework_selection()
        if reusable is not None:
            decision = self.supervisor_decision(reusable)
            self.event('rework_existing_checks_selected', need_id=self.pending.get('need_id'),
                       checks=[c.model_dump(mode='json') for c in reusable.tasks[0].checks],
                       planning_source='validated_pending_ledger', model_call_saved=True)
        else:
            decision = self.ask("supervisor", SupervisorSelection, {
                'diagnosis_policy': review_policy(),
                "ticket": self.ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
                "phase": self.phase, "pending_challenge": self.pending, "evidence": self.sources(),
                'evidence_needs': self.need_input(),
                "collection": self.collection(), "observation_gaps": self.observation_gaps(),
                "planning_guidance": self.planning_guidance(),
                'collection_coverage': self.collection_coverage(),
                'investigation_execution': self.investigation_mode,
                "read_only_capabilities": {r: ex.capabilities() for r, ex in self.executors.items()},
                "read_only_tools": {r: ex.schemas() for r, ex in self.executors.items()},
                "tasks": self.task_input(), "task_completion_gaps": self.task_gaps(),
                "budget": {"used": dict(self.context.counts), "limits": self.context.limits.__dict__,
                           "max_dispatch_tasks": self.dispatch_capacity(pending_model_call=True)}},
                terminal=bool(self.evidence and self.phase == "collect"), validate=self.supervisor_decision)
        self.supervisor_calls += 1
        self.evidence_needs = {need.need_id: need for need in decision.evidence_needs}
        self.event('evidence_needs_updated', evidence_needs=self.need_input(),
                   action=decision.action, reason=decision.reason)
        self.decisions.append({"round": self.supervisor_calls, **decision.model_dump(mode="json")})
        self.event("supervisor_decision", action=decision.action, round=self.supervisor_calls,
                   requested_roles=[t.role for t in decision.tasks])
        if decision.action != "dispatch" and decision.tasks:
            raise ProtocolError("UNEXPECTED_TASKS")
        if decision.action == "dispatch":
            self.prepare_dispatch(decision)
        elif decision.action == "diagnose":
            if not any(e.kind == "observation" for e in self.evidence.values()):
                raise ProtocolError("DIAGNOSIS_WITHOUT_OBSERVATIONS")
            if self.phase == "rework":
                # No new read means there is no new draft to diagnose/review.
                # Keep the previous review and its unresolved challenge instead
                # of misreporting that the collected observations do not exist.
                gap = (self.pending or {}).get('missing_observation', '')
                self.extra_gaps.append('复核补查未执行，保留原诊断与复核结果；' + gap)
                self.event('rework_denied', 'supervisor', code='REWORK_NOT_EXECUTED',
                           reason=decision.reason, missing_observation=gap)
                self.stop = 'REWORK_NOT_EXECUTED'
                return
            self.next_node = 'diagnosis'
        elif decision.action == "finish":
            if self.review is None:
                raise ProtocolError("UNREVIEWED_FINISH")
            self.finish_review()
        else:
            self.team = decision.escalation_team
            self.extra_gaps.extend(decision.missing_information or [decision.reason])
            self.note("escalate", "supervisor", "human", reason=decision.reason)
            if self.prepare_limited_handoff():
                return
            self.stop = "ESCALATED" if decision.action == "escalate" else "NEEDS_INFORMATION"

    def pending_rework_selection(self):
        """Reuse only unattempted checks already linked to the challenged need."""
        if self.phase != 'rework' or not self.pending or self.reworks >= (2 if self.additional_rework_authorized else 1):
            return None
        need_id = self.pending.get('need_id')
        need = self.evidence_needs.get(need_id)
        if need is None or need.status in {'supported', 'not_required'}:
            return None
        candidates = []
        for task in self.tasks:
            if need_id in task['need_ids'] and task.get('expected_value'):
                candidates.extend((task['role'], check, task['expected_value'])
                                  for check in task['required_checks'])
        if need.next_check and self.pending.get('expected_value'):
            candidates.append((need.role, need.next_check.model_dump(mode='json'), self.pending['expected_value']))
        selected, seen, selected_role, expected = [], set(), None, None
        for role, check, expectation in candidates:
            executor = self.executors[role]
            try:
                args = executor.validate_args(check['tool'], check['args']).model_dump(mode='json')
            except ExecutionError:
                continue
            normalized = {'tool': check['tool'], 'args': args}
            signature = (role, check['tool'], json.dumps(args, sort_keys=True))
            if (signature in seen or selected_role not in {None, role}
                    or completion([normalized], executor)['checks'][0]['status'] != 'not_executed'
                    or not adds_information([normalized], executor)):
                continue
            if not self.rework_budget(tool_calls=len(selected) + 1, supervisor_completed=True)['can_collect']:
                continue
            selected.append(normalized)
            seen.add(signature)
            selected_role, expected = role, expectation
            if len(selected) == 2:
                break
        if not selected:
            return None
        return SupervisorSelection.model_validate_json(json.dumps({
            'action': 'dispatch', 'reason': 'Reuse validated unexecuted checks for the challenged evidence need.',
            'tasks': [{'role': selected_role, 'goal': 'Complete previously planned checks for ' + self.pending['challenge_id'] + ': ' + need.question,
                       'need_ids': [need_id], 'checks': selected, 'expected_value': expected[:400]}]}))

    def recover_supervisor(self, exc):
        """One finalization route for protocol mistakes, never permission failures."""
        recoverable = {'REQUIRED_EVIDENCE_NEED', 'EVIDENCE_NEED_COVERAGE',
                       'EVIDENCE_NEED_PURPOSE_CHANGED', 'CONFLICTING_NEED_FORMATS',
                       'TASK_LIMIT', 'ROLE_OUTPUT_INVALID', 'schema_mismatch',
                       'invalid_json', 'invalid_output_type'}
        if (exc.code != 'MODEL_OUTPUT_INVALID' or self.last_supervisor_failure not in recoverable
                or self.phase != 'collect' or self.protocol_fallback_used
                or not any(e.kind == 'observation' for e in self.evidence.values())):
            return False
        remaining = self.context.limits.model_calls - self.context.counts['model_calls']
        seconds = self.context.limits.seconds - (time.monotonic() - self.context.started)
        if remaining < 2 or seconds <= self.context.limits.reserve_seconds:
            self.event('protocol_finalization_denied', code='BUDGET_EXCEEDED',
                       remaining_model_calls=remaining)
            return False
        self.protocol_fallback_used = True
        self.dispatch_queue.clear()
        self.next_node = 'diagnosis'
        self.extra_gaps.append('调度输出未通过协议校验，已保留原证据问题并转入诊断与独立复核；未执行失败决定中的任务。')
        self.event('protocol_finalization_started', code=self.last_supervisor_failure,
                   remaining_model_calls=remaining, finalization_only=True)
        return True

    def unreviewed_observations(self, approved_findings):
        return unreviewed_observations(approved_findings, completed=self.status() == 'completed',
            investigation_mode=self.investigation_mode, evidence=self.evidence,
            references=self.references, tasks=self.tasks)

    def graph_snapshot(self):
        with self.context.lock:
            counts = dict(self.context.counts)
        return self.state.graph_projection(counts)

    def run(self):
        from workflow.graph import build_graph
        with activate(self.context), self.context.span("workflow", "incident_collaboration") as span:
            self.workflow_span = span['span_id']
            build_graph(self).invoke({}, config={
                'recursion_limit': self.policy.recursion_limit,
                'max_concurrency': self.policy.max_parallel_roles})
        return self.result()

    def status(self):
        if self.limited_handoff and self.stop not in FATAL_CODES:
            return 'partial'
        if self.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} and self.review is not None:
            if self.diagnostic_pack is not None and self.ticket.purpose == 'diagnosis':
                from workflow.diagnostic_pack import refresh
                state = refresh(self)
                if (state is None or state['status'] in {'preflight', 'invalid_configuration'}
                        or state.get('blocking_check_ids')):
                    return 'partial'
            return "completed"
        return "partial" if self.evidence or self.stop in {"NEEDS_INFORMATION", "ESCALATED", "BUDGET_EXCEEDED", "SUPERVISOR_LIMIT", "NO_TOOL_PROGRESS"} else "failed"

    def status_check_coverage(self):
        return status_check_coverage(self.executors['investigation'].capabilities(),
                                     self.evidence, self.collection())

    def result(self):
        from workflow.graph import WORKFLOW_VERSION
        self.check_diagnostic_pack_exit('finalization')
        if self.diagnostic_pack is not None:
            self.context.stop_reason = self.stop
        status = self.status()
        needs_handoff = status != 'completed' or bool(self.draft and any(
            a.kind == 'manual_check' for a in self.draft.recommended_actions))
        if needs_handoff and not (self.team or self.draft and self.draft.escalation_team):
            choice = owner_choice(self.evidence, self.ticket.scope.service,
                                  self.executors['investigation'].check_results.values())
            self.team = choice.team
            if choice.status == 'ambiguous':
                self.extra_gaps.append('负责人信息存在冲突，未选择任何登记团队，需人工确认接手人。')
        publication = publish_diagnosis(PublicationInput(
            draft=self.draft, review=self.review, purpose=self.ticket.purpose, status=status, stop=self.stop,
            missing_information=[*self.extra_gaps, *self.task_gaps(), *limitations(self.evidence_needs)],
            team=self.team, coverage_gaps=self.observation_gaps()))
        # Coordinator is the only writer of cross-stage publication state.
        self.extra_gaps[:] = publication.missing_information
        self.team = publication.escalation_team
        metadata = {
            "run_id": self.context.run_id,
            "task_type": "incident_collaboration",
            "incident_id": self.ticket.incident_id,
            'purpose': self.ticket.purpose,
            'status_check_coverage': self.status_check_coverage() if self.ticket.purpose == 'status_check' else None,
            "scope": self.ticket.scope.model_dump(mode="json"),
            "data_source": self.provider.name,
            "status": status,
            "review_status": "passed" if self.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} else "needs_information" if self.review else "not_performed",
            'rejected_actions': self.draft.rejected_actions if self.draft else [],
            'unreviewed_observations': self.unreviewed_observations(publication.findings),
            'protocol_finalization_used': self.protocol_fallback_used,
            'diagnosis_readiness': {'policy_version': 'diagnosis_readiness_v2',
                            'report_status': publication.report_status,
                            'blocking_gaps': [*(self.reviewed_gaps() if self.draft and self.review else self.diagnosis_completion_gaps()),
                                *(self.diagnostic_pack_result.get('blocking_gaps', []) if self.diagnostic_pack_result else [])],
                            'meaning': '报告可供决策不等于确认唯一根因、条件已满足、批准修复或工单解决。'},
            'diagnostic_audit': diagnostic_audit(self.evidence_needs, self.review, self.draft,
                            self.executors, self.tasks, stop=self.stop, events=self.events),
            "evidence": [e.model_dump(mode="json") for e in self.evidence.values()],
            "task_results": self.task_results(),
            "drafts": self.drafts,
            "reviews": self.reviews,
            "negotiation": self.negotiation,
            'blind_review_enabled': self.blind_review,
            'observation_reviews': self.observation_reviews,
            'review_contract_version': 'bounded_refutation_v1' if self.review_protection == BOUNDED_POLICY else
                            'question_owned_blocker_v3_conservative' if conservative(self.review_protection) else 'question_owned_blocker_v2',
            'review_protection': self.review_protection,
            'review_protection_adjustments': [e for e in self.events if e['type'] in
                            {'review_protection_applied', 'review_scope_limitation', 'review_scope_reclassified'}],
            'refutation_audit': [e for e in self.events if e['type'] == 'refutation_basis_retained'],
            'deferred_review_requests': self.deferred_review_requests,
            'evidence_needs': self.need_input(),
            'evidence_needs_version': 'question_ledger_v1',
            'collection_exits': self.collection_exits,
            'evidence_needs_validation': 'model_semantics_with_program_guards',
            "events": self.events,
            "run_summary": self.context.summary(),
            "repairs": self.repairs["repairs"],
            'repair_policy': ROLE_REPAIR_POLICY if self.role_corrections else REVIEW_STAGE_POLICY if self.reviewer_repair_mode == 'split' else REPAIR_POLICY,
            'reviewer_repair_mode': self.reviewer_repair_mode,
            'repair_counts': {role: self.repairs.get('role_repairs', {}).get(role, 0)
                                           for role in ('supervisor', 'diagnosis', 'reviewer')} if self.role_corrections else
                                          {'collection': self.repairs['repairs'] - self.repairs.get('reviewer_repairs', 0),
                                           'reviewer': self.repairs.get('reviewer_repairs', 0)},
            "fact_rendering": FACT_RENDERING,
            "coverage_gaps": self.observation_gaps(),
            'model_input_view': MODEL_VIEW_VERSION,
            "rework_rounds": self.reworks,
            "supervisor_decisions": self.supervisor_calls,
            "scheduling_decisions": self.decisions,
            "workflow_engine": "langgraph",
            "workflow_version": WORKFLOW_VERSION,
            'collection_execution': {'mode': 'parallel_when_independent' if self.parallel_collection else 'serial',
                            'investigation_mode': self.investigation_mode,
                            'knowledge_mode': self.knowledge_mode, 'supervisor_planning_only': self.planning_only,
                            'parallel_batches': self.parallel_batches,
                            'max_parallel_roles': 2, 'rework_parallel': False,
                            **({'diagnostic_pack_policy': self.diagnostic_pack_result['policy'],
                                'diagnostic_pack_state_policy': self.diagnostic_pack_result['state_policy'],
                                'diagnostic_pack_exit_policy': self.diagnostic_pack_exit['policy'] if self.diagnostic_pack_exit else None,
                                'diagnostic_pack_fingerprint': self.diagnostic_pack_result['fingerprint']}
                               if self.diagnostic_pack_result is not None else {})},
            **({'diagnostic_pack': self.diagnostic_pack_result} if self.diagnostic_pack_result is not None else {}),
            **({'diagnostic_pack_exit': self.diagnostic_pack_exit} if self.diagnostic_pack_exit is not None else {}),
            "prompt_version": self.prompt_version(),
        }
        return build_report(ReportSnapshot(metadata, publication))

    def prompt_version(self):
        from agents.prompts import KNOWLEDGE
        from agents.investigation_prompt import SYSTEM
        schemas = [SupervisorSelection.model_json_schema(), DiagnosisSelection.model_json_schema(), ReviewDecision.model_json_schema()]
        payload = [ROLE_PROMPTS, PLANNER if self.planning_only else None, SYSTEM, INVESTIGATION_TASK, KNOWLEDGE,
                   OBSERVATION_REVIEW if self.blind_review else None, schemas,
                   ObservationReview.model_json_schema() if self.blind_review else None,
                   *[ex.schemas() for ex in self.executors.values()]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def collaborate(models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None, event_log=None,
                parallel_collection=True, blind_review=None, investigation_mode='programmatic', reviewer_repair_mode='shared', knowledge_mode=None, diagnostic_pack=None, review_protection=DEFAULT_REVIEW_PROTECTION):
    return Collaboration(models, provider, principal, limits=limits, emit=emit, context=context,
                         event_log=event_log, parallel_collection=parallel_collection, blind_review=blind_review,
                         investigation_mode=investigation_mode, reviewer_repair_mode=reviewer_repair_mode,
                         knowledge_mode=knowledge_mode, diagnostic_pack=diagnostic_pack, review_protection=review_protection).run()
