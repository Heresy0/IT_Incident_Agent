"""Role operations and guards reused by the incident LangGraph workflow."""
import hashlib
import json
import time
from runtime.context import RunContext, ExecutionError, activate
from agents.contracts import SupervisorSelection, DiagnosisSelection, Hypothesis, ReviewDecision, ObservationReview
from agents.prompts import ROLE_PROMPTS, INVESTIGATION_TASK, OBSERVATION_REVIEW, PLANNER
from agents.investigation import INCIDENT_LIMITS
from tools.executor import ToolExecutor
from agents.role_runner import RoleRequest, RoleRunner
from agents.protocol import RoleContractContext, role_output_schema, role_model_input, parse_role_output
from workflow.planning import PlanningContext, validate_plan
from workflow.collection import (CollectionRequest, CollectionOutcome, CollectionRunner,
                                 execute_planned_reads, task_record, join_parallel)
from workflow.reporting import (PublicationInput, ReportSnapshot, publish_diagnosis, build_report,
                                unreviewed_observations, status_check_coverage)


from evidence.diagnosis import ProtocolError, expand_refs, expand_diagnosis, preserve_diagnosis
from evidence.observations import FACT_RENDERING, pool_control_gaps, log_control_gaps, gap_message
from evidence.model_view import MODEL_VIEW_VERSION, evidence_view
from evidence.completion import diagnosis_gaps, required_sections
from workflow.task_checks import completion, adds_information, log_query_feedback
from workflow.evidence_needs import initial_needs, validate_assessments, reviewed_needs, limitations, is_blocking
from workflow.diagnostic_audit import validate_blockers, diagnostic_audit, blocker_follow_up
from workflow.review_outcome import review_policy, readiness_gaps, nonblocking_trigger_request
from agents.protocol import REPAIR_POLICY, REVIEW_STAGE_POLICY, ROLE_REPAIR_POLICY
from evidence.observations import source_statement
from workflow.review_protection import conservative, BOUNDED_POLICY, DEFAULT_REVIEW_PROTECTION


class Collaboration:
    def __init__(self, models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None,
                 event_log=None, parallel_collection=True, blind_review=None, investigation_mode='programmatic', reviewer_repair_mode='shared', knowledge_mode=None, diagnostic_pack=None, review_protection=DEFAULT_REVIEW_PROTECTION):
        from workflow.review_protection import MODES
        if review_protection not in MODES:
            raise ValueError('Unregistered review protection mode')
        self.review_protection = review_protection
        if investigation_mode not in {'agent', 'programmatic'}:
            raise ValueError('unregistered investigation mode')
        self.investigation_mode = investigation_mode
        self.knowledge_mode = investigation_mode if knowledge_mode is None else knowledge_mode
        if self.knowledge_mode not in {'agent', 'programmatic'}:
            raise ValueError('unregistered knowledge mode')
        self.planning_only = self.investigation_mode == self.knowledge_mode == 'programmatic'
        self.role_corrections = self.planning_only and reviewer_repair_mode == 'shared'
        if reviewer_repair_mode not in {'shared', 'split'}:
            raise ValueError('unregistered reviewer repair mode')
        self.reviewer_repair_mode = reviewer_repair_mode
        if set(models) != {"supervisor", "investigation", "knowledge", "diagnosis", "reviewer"}:
            raise ValueError("five registered models required")
        self.models, self.provider, self.principal = models, provider, principal
        self.ticket = provider.ticket()
        provider.authorize(principal, self.ticket.scope)
        self.diagnostic_pack, self.diagnostic_pack_result, self.diagnostic_pack_exit = None, None, None
        if diagnostic_pack is not None:
            from workflow.diagnostic_pack import DiagnosticPack
            if not self.planning_only:
                raise ValueError('Diagnostic pack experiment requires programmatic collection')
            raw = diagnostic_pack.model_dump() if isinstance(diagnostic_pack, DiagnosticPack) else diagnostic_pack
            self.diagnostic_pack = DiagnosticPack.model_validate_json(json.dumps(raw))
            self.diagnostic_pack.assert_scope(self.ticket)
        self.evidence_needs = initial_needs(self.ticket)
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
        self.review_follow_up_used = False
        self.collection_follow_up_used = False
        self.reused_requests = []
        self.evidence, self.references, self.seen_tasks = {}, {}, set()
        self.supervisor_calls = self.reworks = 0
        self.additional_rework_authorized = False
        self.pending, self.draft, self.review = None, None, None
        self.phase, self.stop, self.extra_gaps, self.team = "collect", "", [], None
        self.workflow_span = self.context.root_span
        self.dispatch_queue = []
        self.dispatch_follow_up = False
        self.dispatch_previous = set()
        self.dispatch_reads = set()
        self.next_node = 'supervisor'
        self.protocol_fallback_used = False
        self.last_supervisor_failure = None
        self.parallel_batch = None
        self.parallel_plans = {}
        self.parallel_batches = 0
        self.parallel_collection = parallel_collection
        self.collection_exits = []
        self.blind_review = getattr(models['reviewer'], 'blind_review', False) if blind_review is None else blind_review
        self.observation_reviews = []
        self.deferred_review_requests = []

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
        return expand_diagnosis(selection, self)

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
        if gaps:
            self.stop = 'DIAGNOSIS_INCOMPLETE'
            self.event('diagnosis_gate', 'diagnosis', code=self.stop, missing_information=gaps)
        elif (readiness_gaps(self.draft, self.review, self.ticket.purpose)
                or self.observation_gaps() or self.task_gaps()
                or limitations(self.evidence_needs, blocking_only=True)):
            self.stop = 'REVIEW_INCOMPLETE'
        else:
            self.stop = 'STATUS_CHECK_COMPLETED' if self.ticket.purpose == 'status_check' else 'FINISHED'
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
            value.pop('evidence_explanation', None)
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
            if need['purpose'] in scopes:
                need['review_scope'] = scopes[need['purpose']]
        tasks = [{k: row[k] for k in ('task_id', 'role', 'goal', 'need_ids', 'completion',
                                    'query_state_source', 'model_summary_verified')}
                 for row in self.task_input()]
        rework_budget = self.rework_budget(pending_model_calls=1, additional=self.reworks == 1)
        return {"targets": targets, "required_target_ids": list(targets),
                **self.diagnostic_pack_input(),
                'evidence_needs': needs, 'required_need_ids': list(self.evidence_needs),
                'core_need_ids': [n.need_id for n in self.evidence_needs.values()
                                  if n.purpose in {'symptom', 'mechanism'}],
                'independent_observation_review': self.observation_reviews[-1]['review']
                    if self.observation_reviews and self.observation_reviews[-1]['revision'] == len(self.drafts) else None,
                "purpose": self.ticket.purpose,
                "diagnosis_completion_gaps": self.diagnosis_completion_gaps(),
                "ticket": self.ticket.model_dump(mode='json', exclude={'scope': {'tenant_id', 'user_id'}}),
                "task_results": tasks, "task_completion_gaps": self.task_gaps(),
                "read_only_capabilities": {r: ex.capabilities() for r, ex in self.executors.items()},
                "repair_capabilities": getattr(self.provider, 'repair_capabilities', []),
                "evidence": self.sources(), "collection": self.collection(), "rework_used": self.reworks,
                "prior_challenge": self.pending, "observation_gaps": self.observation_gaps(),
                "budget": {key: rework_budget[key] for key in
                           ('can_collect', 'remaining_model_calls', 'remaining_tools')}}

    def rework_budget(self, *, pending_model_calls=0, tool_calls=1, supervisor_completed=False, additional=False):
        # Approved checks run before one interpretation call. Count future steps
        # once, while retaining the original finalization and time reserves.
        remaining = self.context.limits.model_calls - self.context.counts['model_calls']
        steps = {'pending_model_calls': pending_model_calls, 'supervisor': int(not supervisor_completed),
                 'interpretation': 1, 'finalization_reserve': max(2, self.context.limits.reserve_model_calls)}
        extended = additional or self.additional_rework_authorized
        if extended:
            steps['finalization_reserve'] = max(2 + int(self.blind_review), self.context.limits.reserve_model_calls)
            steps['correction_reserve'] = self.context.limits.reserve_model_calls
        needed = sum(steps.values())
        tools = self.context.limits.tool_calls - self.context.counts['tool_calls']
        model_turns = int(not supervisor_completed) + 3 + int(self.blind_review)
        time_required = (30 * model_turns + self.context.limits.reserve_seconds if extended
                         else self.context.limits.reserve_seconds)
        affordable = (remaining >= needed and tools >= tool_calls
            and self.supervisor_calls < (5 if extended else 4) and len(self.tasks) < (7 if extended else 6)
            and self.reworks < (2 if extended else 1)
            and time.monotonic() - self.context.started < self.context.limits.seconds - time_required)
        return {'can_collect': affordable, 'model_steps': steps, 'required_model_calls': needed,
                'remaining_model_calls': remaining, 'required_tool_calls': tool_calls, 'remaining_tools': tools}

    def additional_rework_eligible(self):
        if self.reworks != 1 or self.ticket.purpose != 'diagnosis' or self.protocol_fallback_used or not self.review:
            return False
        request = self.review.request_evidence
        if not request or not request.need_id or not request.target_id:
            return False
        claim = self.targets().get(request.target_id)
        if claim is None or (isinstance(claim, Hypothesis) and claim.level not in {'mechanism', 'alternative'}):
            return False
        target = next((a for a in self.review.assessments if a.target_id == request.target_id), None)
        assessment = next((a for a in self.review.need_assessments if a.need_id == request.need_id), None)
        if target is None or target.verdict != 'uncertain' or assessment is None or assessment.verdict != 'uncertain':
            return False
        blocker = assessment.blocker
        return bool(blocker and request.target_id in blocker.affected_targets
                    and blocker.cause_level in {'observation', 'mechanism', 'alternative', 'action_condition'}
                    and blocker.resolution != 'external_source' and blocker.decision_impact.strip()
                    and is_blocking(self.evidence_needs[request.need_id].model_copy(update={'blocking': assessment.blocking})))

    def can_replan(self):
        return (self.supervisor_calls < 3 and len(self.tasks) < 6
            and self.dispatch_capacity(pending_model_call=1) > 0
            and time.monotonic() - self.context.started < self.context.limits.seconds - self.context.limits.reserve_seconds)

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
        # One normal-budget clarification, with room for dispatch, interpretation,
        # a revised diagnosis and its review. It never authorizes a tool by itself.
        return (not self.protocol_fallback_used and self.ticket.purpose == 'diagnosis' and not self.review_follow_up_used
            and not self.reworks and not review.request_evidence and not review.follow_up_reason
            and ((any(a.verdict == 'uncertain' and a.target_id.startswith(('H', 'A')) for a in review.assessments)
                  and readiness_gaps(self.draft, review, self.ticket.purpose))
                 or any(a.verdict == 'uncertain' and is_blocking(self.evidence_needs[a.need_id].model_copy(
                     update={} if a.blocking is None else {'blocking': a.blocking})) for a in review.need_assessments))
            and self.rework_budget(pending_model_calls=1)['can_collect'])

    def collection(self):
        # Empty/truncated/error results remain visible to the tool-free judging roles.
        return [{k: event[k] for k in ("role", "name", "status", "query_profile", "truncated", "evidence_ids", "error")}
                for event in self.events if event["type"] == "tool_result"]

    def validate_review(self, review):
        # Immutable core materiality is server-owned, independent of model wording.
        core_flags = [a for a in review.need_assessments if a.need_id in self.evidence_needs
                      and self.evidence_needs[a.need_id].purpose in {'symptom', 'mechanism'}
                      and a.blocking is not None]
        assessments = [a.model_copy(update={'blocking': None}) if a in core_flags else a
                       for a in review.need_assessments]
        need_assessments = validate_assessments(self.evidence_needs, assessments, self.evidence)
        for assessment in core_flags:
            self.event('reviewer_fields_ignored', 'reviewer', code='CORE_BLOCKING_RETAINED',
                       need_id=assessment.need_id, path='blocking', actual=assessment.blocking)
        targets = self.targets()
        ids = [a.target_id for a in review.assessments]
        if len(ids) != len(set(ids)) or set(ids) != set(targets):
            raise ProtocolError("REVIEW_TARGET_COVERAGE", [{
                'required_target_ids': list(targets),
                'missing_target_ids': sorted(set(targets) - set(ids)),
                'unexpected_target_ids': sorted(set(ids) - set(targets)),
                'duplicate_target_ids': sorted({key for key in ids if ids.count(key) > 1}),
            }])
        checked = []
        for assessment in review.assessments:
            self.evidence_ids(assessment.evidence_ids)
            target = targets[assessment.target_id]
            if assessment.verdict == "supported" and assessment.target_id.startswith("H"):
                if not any(self.evidence[r.evidence_id].kind == "observation" for r in target.support_refs):
                    assessment = assessment.model_copy(update={"verdict": "uncertain",
                        "reason": "Program gate: historical knowledge alone cannot establish a current cause."})
                    self.event("review_gate", "reviewer", target_id=assessment.target_id, code="NO_CURRENT_SUPPORT")
                elif self.observation_gaps():
                    assessment = assessment.model_copy(update={'verdict': 'uncertain', 'reason': gap_message(self.observation_gaps())})
                    self.event('review_gate', 'reviewer', target_id=assessment.target_id, code='MISSING_HEALTH_CONTROL')
            checked.append(assessment)
        if conservative(self.review_protection):
            from workflow.review_protection import protect_refutations
            checked = protect_refutations(review.model_copy(update={'assessments': checked}),
                self.evidence, self.event, bounded=self.review_protection == BOUNDED_POLICY,
                scope=self.ticket.scope).assessments
        request = review.request_evidence
        self.review_hint_status = []
        if request:
            self.evidence_ids(request.evidence_ids)
            if request.need_id is not None and request.need_id not in self.evidence_needs:
                raise ProtocolError('UNKNOWN_OR_DUPLICATE_NEED')
            disputed = request.target_id or request.hypothesis_id
            supported_check_request = (conservative(self.review_protection)
                and disputed in targets and disputed.startswith('A')
                and targets[disputed].kind in {'manual_check', 'read_only_check'})
            if (request.hypothesis_id is not None and request.hypothesis_id not in targets
                    or disputed is not None and (disputed not in targets
                        or disputed.startswith('H') and request.hypothesis_id not in {None, disputed}
                        or any(a.target_id == disputed and a.verdict == 'supported' for a in checked)
                            and not supported_check_request)
                    or disputed is None and request.need_id is None):
                raise ProtocolError('INVALID_REWORK_TARGET')
            if request.need_id and disputed and disputed.startswith('H'):
                purpose = self.evidence_needs[request.need_id].purpose
                level = targets[disputed].level
                if (level == 'trigger' and purpose in {'symptom', 'mechanism', 'alternative'}
                        or purpose == 'trigger' and level in {'mechanism', 'alternative'}):
                    # The target and gap remain valid. Detach only the conflicting
                    # edge; never promote a verdict or discard a missing observation.
                    self.event('review_request_scope_detached', 'reviewer', code='REVIEW_REQUEST_SCOPE',
                        need_id=request.need_id, target_id=disputed,
                        need_purpose=purpose, target_level=level, verdicts_changed=False)
                    request = request.model_copy(update={'need_id': None})
            # Hints never authorize a call. Discard unsafe/redundant suggestions,
            # but preserve the legitimate evidence gap for Supervisor to plan.
            normalized, signatures = [], set()
            executor = self.executors[request.target_role]
            for check in request.checks:
                code = None
                try:
                    args = executor.validate_args(check.tool, check.args)
                    signature = (check.tool, args.model_dump_json())
                    candidate = check.model_copy(update={'args': args.model_dump(mode='json')})
                    if signature in signatures or not adds_information([candidate.model_dump(mode='json')], executor):
                        code = 'DUPLICATE_TOOL'
                    signatures.add(signature)
                except ExecutionError as exc:
                    code = exc.code
                if code:
                    self.review_hint_status.append({'tool': check.tool, 'code': code})
                    self.event('review_hint_ignored', 'reviewer', code=code, name=check.tool)
                else:
                    normalized.append(candidate)
            request = request.model_copy(update={'checks': normalized})
            reopened = []
            for assessment in need_assessments:
                if assessment.need_id == request.need_id and assessment.verdict != 'uncertain':
                    self.event('review_need_reopened', 'reviewer', need_id=assessment.need_id,
                               target_id=disputed, previous_verdict=assessment.verdict,
                               verdict='uncertain', code='EVIDENCE_GAP_PENDING')
                    assessment = assessment.model_copy(update={'verdict': 'uncertain',
                        'reason': ('复核提出证据缺口，此问题暂缓关闭：' + request.missing_observation)[:400]})
                reopened.append(assessment)
            need_assessments = reopened
        normalized = review.model_copy(update={'assessments': checked, 'need_assessments': need_assessments,
                                               'request_evidence': request})
        normalized = validate_blockers(self.evidence_needs, normalized, self.executors,
            required=self.blind_review or getattr(self.models['reviewer'], 'json_output', False), targets=targets,
            conservative=conservative(self.review_protection), event=self.event)
        if conservative(self.review_protection):
            for assessment in normalized.need_assessments:
                if assessment.blocker and not is_blocking(self.evidence_needs[assessment.need_id].model_copy(
                        update={} if assessment.blocking is None else {'blocking': assessment.blocking})):
                    gap = '复核保留范围限制：' + assessment.blocker.missing_observation
                    if gap not in self.extra_gaps:
                        self.extra_gaps.append(gap)
        handoff = blocker_follow_up(self.evidence_needs, normalized, self.executors)
        if handoff:
            self.event('review_blocker_handoff', 'reviewer', need_id=handoff.need_id,
                       target_id=handoff.target_id, planning_required=True)
            normalized = normalized.model_copy(update={'request_evidence': handoff})
            # A legacy blocker-derived request must pass the same ownership gate.
            return self.validate_review(normalized)
        return normalized

    def diagnose(self):
        if not self.prepare_diagnosis_entry():
            return
        self.review = None  # A previous revision's review never approves a new draft.
        payload = {"ticket": self.ticket.model_dump(mode="json", exclude={"scope": {"tenant_id", "user_id"}}),
                   **self.diagnostic_pack_input(),
                   'diagnosis_policy': review_policy(),
                   'evidence_needs': self.need_input(),
                   "purpose": self.ticket.purpose,
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
        if self.blind_review and self.ticket.purpose == 'diagnosis':
            remaining = self.context.limits.model_calls - self.context.counts['model_calls']
            if remaining >= 2:
                def validate_observations(value):
                    claims = []
                    for claim in value.directly_supported:
                        refs = self.expand_refs(claim.refs)
                        if any(self.evidence[ref.evidence_id].kind != 'observation' for ref in refs):
                            raise ProtocolError('REVIEW_CURRENT_EVIDENCE_REQUIRED')
                        claims.append(claim.model_copy(update={'statement': source_statement(refs, self.evidence)}))
                    return value.model_copy(update={'directly_supported': claims})
                self.event('observation_review_started', 'reviewer', revision=revision, draft_visible=False)
                observed = self.ask('reviewer', ObservationReview, {
                    'ticket': self.ticket.model_dump(mode='json', exclude={'scope': {'tenant_id', 'user_id'}}),
                    'evidence': self.sources(), 'collection': self.collection()},
                    terminal=True, validate=validate_observations, system_prompt=OBSERVATION_REVIEW)
                self.observation_reviews.append({'revision': revision, 'review': observed.model_dump(mode='json')})
                self.event('observation_review_completed', 'reviewer', revision=revision, draft_visible=False)
            else:
                self.event('observation_review_skipped', 'reviewer', code='BUDGET_EXCEEDED', revision=revision)
        self.review = self.ask("reviewer", ReviewDecision, self.reviewer_input(), terminal=True, validate=self.validate_review)
        self.evidence_needs = reviewed_needs(self.evidence_needs, self.review.need_assessments)
        request = self.review.request_evidence
        if (request and self.ticket.purpose == 'diagnosis' and not self.reviewed_gaps()
                and nonblocking_trigger_request(self.draft, self.review, self.evidence_needs,
                    conservative=conservative(self.review_protection))):
            # Keep the actual request and unresolved verdict in the audit record.
            # Only the automatic routing is deferred to human follow-up.
            deferred = {'revision': revision, 'request': request.model_dump(mode='json'),
                        'reason': '机制和有效下一步已复核；纯触发未知作为人工待核实限制保留。'}
            self.deferred_review_requests.append(deferred)
            self.extra_gaps.append(('已支持核查待办的结果待人工取得：' if
                (request.target_id or '').startswith('A') else '非关键触发原因待人工核实：') + request.missing_observation)
            self.event('review_request_deferred', 'reviewer', **deferred, verdicts_changed=False)
            request = None
        self.event('evidence_needs_reviewed', 'reviewer', revision=revision,
                   evidence_needs=self.need_input(), blocking_gaps=limitations(self.evidence_needs, blocking_only=True))
        self.reviews.append({"revision": revision, "review": self.review.model_dump(mode="json")})
        self.event("review_completed", "reviewer", revision=revision,
                   follow_up_reason=self.review.follow_up_reason,
                   status="needs_rework" if request else
                   "passed" if not self.reviewed_gaps() else "needs_information")
        if request:
            self.note("challenge", "reviewer", "diagnosis", hypothesis_id=request.hypothesis_id,
                      evidence_ids=request.evidence_ids, reason=request.missing_observation)
            if self.protocol_fallback_used:
                self.event('rework_denied', 'reviewer', code='PROTOCOL_FINALIZATION_ONLY')
                self.extra_gaps.append('调度协议失败后仅收尾；复核提出的补查仍未执行：' + request.missing_observation)
                self.finish_review()
                # A pending executable check must not be disguised as completion.
                self.stop = 'REVIEW_INCOMPLETE'
            elif self.reworks >= 2 or (self.reworks == 1 and not self.additional_rework_eligible()):
                self.event("rework_denied", "supervisor", code="REWORK_LIMIT")
                self.stop = "REWORK_LIMIT"
            elif not self.rework_budget(additional=self.reworks == 1)['can_collect']:
                self.event('rework_denied', code='BUDGET_EXCEEDED')
                self.extra_gaps.append('复核缺口未补查：预算或调度额度不足；' + request.missing_observation)
                self.finish_review()
                self.stop = 'REVIEW_INCOMPLETE'
            else:
                if self.reworks == 1:
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
            self.note("accept" if self.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} else "escalate", "reviewer", "supervisor")

    def diagnose_and_review(self):
        """Compatibility helper for direct callers; graph uses separate role nodes."""
        self.diagnose()
        if not self.stop and self.draft is not None:
            self.review_draft()

    def prepare_dispatch(self, decision):
        if not decision.tasks or len(self.tasks) + len(decision.tasks) > (7 if self.additional_rework_authorized else 6):
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
        steps = min(4, available_steps - 2 * len(self.dispatch_queue))
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
        # First diagnosis/review, then one Supervisor-planned read/interpretation
        # and a revised diagnosis/review. No extra calls or budget enlargement.
        base = self.context.limits.reserve_model_calls
        if self.ticket.purpose != 'diagnosis':
            return base
        required = 2 * (2 + int(self.blind_review)) + 2
        # Tiny budgets still permit the initial collection and finalization;
        # they cannot promise a full rework path.
        return max(base, required) if self.context.limits.model_calls >= required + 3 else base

    def dispatch_capacity(self, pending_model_call=False):
        available = self.context.limits.model_calls - self.context.counts["model_calls"] - int(pending_model_call)
        tools = self.context.limits.tool_calls - self.context.counts["tool_calls"]
        if self.phase == 'rework':
            checks = 1 if self.pending.get('planning_required') else len(self.pending['checks'])
            return int(tools >= checks and available >= max(2, self.context.limits.reserve_model_calls) + 1)
        diagnosis = self.ticket.purpose == 'diagnosis'
        keep_tool = (diagnosis and self.collection_reserve() > self.context.limits.reserve_model_calls
                     and any(e.kind == 'observation' for e in self.evidence.values()))
        return max(0, min(2, tools - int(keep_tool),
            (available - self.collection_reserve()) // 2,
            (5 if diagnosis else 6) - len(self.tasks)))

    def supervise(self):
        if (self.phase == 'collect' and self.ticket.purpose == 'diagnosis'
                and any(e.kind == 'observation' for e in self.evidence.values())
                and self.collection_reserve() > self.context.limits.reserve_model_calls
                and (self.supervisor_calls >= 3
                    or self.context.limits.model_calls - self.context.counts['model_calls']
                        <= self.collection_reserve())):
            self.event('collection_reserve_reached', remaining_model_calls=
                self.context.limits.model_calls - self.context.counts['model_calls'],
                protected_model_calls=self.collection_reserve(),
                supervisor_slot_reserved=True, tool_slot_reserved=True)
            self.next_node = 'diagnosis'
            return
        if self.supervisor_calls >= (5 if self.additional_rework_authorized else 4):
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

    def run(self):
        from workflow.graph import build_graph
        with activate(self.context), self.context.span("workflow", "incident_collaboration") as span:
            self.workflow_span = span['span_id']
            build_graph(self).invoke({}, config={"recursion_limit": 40, 'max_concurrency': 2})
        return self.result()

    def status(self):
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
