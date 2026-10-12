"""Role operations and guards reused by the incident LangGraph workflow."""
import hashlib
import json
import time
from uuid import uuid4
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from runtime.context import RunContext, ExecutionError, activate, invoke_chat_model
from evidence.contracts import EscalationSelection, Finding, InvestigationOutput
from agents.contracts import SupervisorDecision, SupervisorSelection, SupervisorRequest, DiagnosisSelection, DiagnosisDraft, Hypothesis, ReviewDecision, ObservationReview
from agents.prompts import ROLE_PROMPTS, INVESTIGATION_TASK, OBSERVATION_REVIEW
from agents.investigation import INCIDENT_LIMITS, investigate, resolve_output, validate_output, validation_details
from tools.executor import ToolExecutor


from evidence.diagnosis import ProtocolError, expand_refs, expand_diagnosis, preserve_diagnosis, diagnosis_reference_options
from evidence.observations import FACT_RENDERING, pool_control_gaps, log_control_gaps, gap_message
from evidence.model_view import MODEL_VIEW_VERSION, evidence_view
from evidence.completion import diagnosis_gaps, required_sections
from evidence.ownership import parse_owner_selection, resolve_owner, owner_options, owner_schema
from workflow.task_checks import completion, adds_information, log_query_feedback
from workflow.evidence_needs import CORE, initial_needs, update_needs, apply_need_updates, validate_assessments, reviewed_needs, limitations, is_blocking
from workflow.diagnostic_audit import validate_blockers, diagnostic_audit, blocker_follow_up
from workflow.review_outcome import review_policy, readiness_gaps, action_projection, nonblocking_trigger_request
from agents.protocol import compact_schema, reviewer_schema, diagnosis_schema, supervisor_schema, bind_evidence_ids, bind_reference_ids, REPAIR_POLICY, REVIEW_STAGE_POLICY, parse_review_selection
from evidence.observations import source_statement


class Collaboration:
    def __init__(self, models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None,
                 event_log=None, parallel_collection=True, blind_review=None, investigation_mode='programmatic', reviewer_repair_mode='shared'):
        if investigation_mode not in {'agent', 'programmatic'}:
            raise ValueError('unregistered investigation mode')
        self.investigation_mode = investigation_mode
        if reviewer_repair_mode not in {'shared', 'split'}:
            raise ValueError('unregistered reviewer repair mode')
        self.reviewer_repair_mode = reviewer_repair_mode
        if set(models) != {"supervisor", "investigation", "knowledge", "diagnosis", "reviewer"}:
            raise ValueError("five registered models required")
        self.models, self.provider, self.principal = models, provider, principal
        self.ticket = provider.ticket()
        provider.authorize(principal, self.ticket.scope)
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
        # Explicit non-dispatch actions never execute attached tasks. This is
        # redundant data, not an instruction to change the selected action.
        if selection.action != 'dispatch' and selection.tasks:
            self.event('supervisor_fields_ignored', code='UNEXPECTED_TASKS',
                       action=selection.action, path='tasks', ignored_count=len(selection.tasks))
            selection = selection.model_copy(update={'tasks': []})
        if selection.action == 'dispatch' and not selection.tasks:
            raise ProtocolError('TASK_LIMIT', [{'path': 'tasks',
                'instruction': 'dispatch须提供一到两个有效任务；没有新检查时选择适用的收尾动作并使用tasks=[]。'}])
        if selection.evidence_needs and (selection.need_updates or selection.additional_needs):
            raise ProtocolError('CONFLICTING_NEED_FORMATS', [{'path': 'need_updates',
                'instruction': '使用need_updates局部更新；不要同时提供旧版evidence_needs清单。'}])
        possible_needs = set(self.evidence_needs) | {n.need_id for n in
            [*selection.evidence_needs, *selection.additional_needs]}
        tasks, redundant_checks = [], []
        for task in selection.tasks:
            self.evidence_ids(task.evidence_ids)
            if len(task.need_ids) != len(set(task.need_ids)) or not set(task.need_ids) <= possible_needs:
                raise ProtocolError('UNKNOWN_OR_DUPLICATE_NEED')
            if self.investigation_mode == 'programmatic' and task.role == 'investigation' and not task.checks:
                raise ProtocolError('INVALID_TASK_CHECK', [{'path': 'tasks.checks',
                    'instruction': '程序取证需要明确的checks；给出登记工具与精确参数，无法规划则request_info或escalate。'}])
            normalized, seen = [], set()
            for check in task.checks:
                try:
                    args = self.executors[task.role].validate_args(check.tool, check.args)
                except ExecutionError as exc:
                    raise ProtocolError('INVALID_TASK_CHECK', [{'code': exc.code,
                        'role': task.role,
                        'allowed_tools': [s['function']['name'] for s in self.executors[task.role].schemas()],
                        'instruction': '按read_only_tools[role]选工具；start/end复制ticket.scope窗口或在其内缩小，不能为对照外扩。无法查询则保留缺口。'}]) from None
                key = (check.tool, args.model_dump_json())
                if key in seen:
                    raise ProtocolError('DUPLICATE_TASK_CHECK')
                seen.add(key)
                normalized.append(check.model_copy(update={'args': args.model_dump(mode='json')}))
            if self.phase == 'rework':
                if not normalized or not task.expected_value:
                    raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'READ_ONLY_CHECK_REQUIRED',
                        'instruction': 'Supervisor须规划checks与expected_value；不可补查时request_info或escalate。'}])
                if self.investigation_mode == 'programmatic':
                    fresh = []
                    for check in normalized:
                        state = completion([check.model_dump(mode='json')], self.executors[task.role])['checks'][0]
                        if state['status'] != 'not_executed' or not adds_information(
                                [check.model_dump(mode='json')], self.executors[task.role]):
                            redundant_checks.append({'role': task.role, 'tool': check.tool, 'status': state['status']})
                        else:
                            fresh.append(check)
                    normalized = fresh
                    if not normalized:
                        continue
                if any(not adds_information([c.model_dump(mode='json')], self.executors[task.role]) for c in normalized):
                    raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'DUPLICATE_TOOL',
                        'instruction': '补查没有新增覆盖；换有价值的新查询，或保留缺口收尾。'}])
                if self.reworks == 1 and self.additional_rework_authorized:
                    if (self.pending.get('need_id') not in task.need_ids or
                            any(completion([c.model_dump(mode='json')], self.executors[task.role])['checks'][0]['status']
                                != 'not_executed' for c in normalized)):
                        raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'ADDITIONAL_CHECK_NOT_NEW',
                            'instruction': '追加补查须关联被质疑的问题，且仅使用未尝试的新检查；不重试失败、截断或已覆盖查询。'}])
                if not self.rework_budget(tool_calls=len(normalized), supervisor_completed=True)['can_collect']:
                    raise ProtocolError('INVALID_REWORK_DISPATCH', [{'code': 'BUDGET_EXCEEDED',
                        'instruction': '预算不足以完整执行补查并收尾；减少检查或保留限制。'}])
            tasks.append(task.model_copy(update={'checks': normalized}))
        # Validate executable proposals first; a recoverable ledger mistake
        # must not hide an unknown source or an out-of-scope tool proposal.
        needs = (update_needs(self.evidence_needs, selection.evidence_needs,
                    self.evidence, self.executors, self.ticket.purpose) if selection.evidence_needs
                 else apply_need_updates(self.evidence_needs, selection.need_updates, selection.additional_needs,
                    self.evidence, self.executors, self.ticket.purpose))
        for update in selection.need_updates:
            if update.blocking is False and needs[update.need_id].purpose in {'symptom', 'mechanism'}:
                self.event('supervisor_fields_ignored', code='CORE_BLOCKING_RETAINED',
                           need_id=update.need_id, path='blocking')
        if redundant_checks:
            self.event('planned_checks_reused', checks=redundant_checks, executed=False)
            if selection.action == 'dispatch' and not tasks:
                # An exhausted plan can hand off uncertainty, never approve a draft.
                selection = selection.model_copy(update={'action': 'request_info',
                    'reason': '程序台账：补查计划全部已尝试或已覆盖，没有新查询；保留证据缺口供人工核实。',
                    'missing_information': list(dict.fromkeys([*selection.missing_information,
                        self.pending['missing_observation']]))[:8]})
        return SupervisorDecision(**selection.model_dump(exclude={'escalation_ref', 'tasks', 'evidence_needs',
                                                                 'need_updates', 'additional_needs'}),
                                  tasks=tasks, evidence_needs=list(needs.values()),
                                  escalation_team=resolve_owner(selection.escalation_ref, self))

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
        output_schema = owner_schema(SupervisorRequest if role == 'supervisor' else schema, self)
        reference_options = diagnosis_reference_options(self) if role == 'diagnosis' else []
        if role == 'diagnosis':
            reference_ids = [option['reference_id'] for option in reference_options]
            if reference_ids:
                output_schema['$defs']['ReferenceSelection']['properties']['reference_id']['enum'] = reference_ids
            output_schema['$defs']['SelectedFinding']['properties']['refs']['uniqueItems'] = True
            for field in ('support_refs', 'counter_refs'):
                output_schema['$defs']['HypothesisSelection']['properties'][field]['uniqueItems'] = True
        if role == 'diagnosis' and self.ticket.purpose == 'status_check':
            for field in ('hypotheses', 'recommended_actions'):
                output_schema['properties'][field]['maxItems'] = 0
        if role == 'diagnosis':
            output_schema = diagnosis_schema(output_schema)
        if role in {'supervisor', 'reviewer', 'diagnosis'} and 'ReadOnlyCheck' in output_schema.get('$defs', {}):
            tools = [s['function'] for ex in self.executors.values() for s in ex.schemas()]
            if tools:
                output_schema['$defs']['ReadOnlyCheck']['oneOf'] = [
                    {'properties': {'tool': {'const': tool['name']}, 'args': tool['parameters']},
                     'required': ['tool', 'args']} for tool in tools]
        if role == 'supervisor':
            output_schema['$defs']['EvidenceNeedUpdate']['properties']['need_id']['enum'] = list(self.evidence_needs)
            core_ids = [n.need_id for n in self.evidence_needs.values() if n.purpose in {'symptom', 'mechanism'}]
            output_schema['$defs']['EvidenceNeedUpdate']['allOf'] = [{
                'if': {'properties': {'need_id': {'enum': core_ids}}, 'required': ['need_id']},
                'then': {'properties': {'status': {'enum': ['pending', 'supported', 'unavailable', 'deferred']}}}}]
            output_schema['oneOf'] = [
                {'properties': {'action': {'const': 'dispatch'}, 'tasks': {'minItems': 1}},
                 'required': ['action', 'tasks']},
                {'properties': {'action': {'enum': ['diagnose', 'request_info', 'escalate', 'finish']},
                                'tasks': {'maxItems': 0}}, 'required': ['action']}]
            output_schema = supervisor_schema(output_schema, self.executors)
            if self.investigation_mode == 'programmatic':
                output_schema['$defs']['TaskRequest'].setdefault('allOf', []).append({
                    'if': {'properties': {'role': {'const': 'investigation'}}, 'required': ['role']},
                    'then': {'properties': {'checks': {'minItems': 1}}, 'required': ['checks']}})
        if role == 'reviewer' and schema is ReviewDecision:
            required = list(self.targets())
            output_schema['$defs']['Assessment']['properties']['target_id']['enum'] = required
            output_schema['properties']['assessments'].update(minItems=len(required), maxItems=len(required))
            output_schema['$defs']['NeedAssessment']['properties']['need_id']['enum'] = list(self.evidence_needs)
            output_schema['properties']['need_assessments'].update(
                minItems=len(self.evidence_needs), maxItems=len(self.evidence_needs))
            output_schema = reviewer_schema(output_schema, self.evidence_needs, self.targets())
        if schema is ObservationReview:
            output_schema = bind_reference_ids(output_schema, self)
        output_schema = compact_schema(bind_evidence_ids(output_schema, self.evidence))
        if role == 'supervisor' or role == 'reviewer' and schema is ReviewDecision:
            # These contracts use EV IDs and field paths, not REF selectors.
            # Owner selectors remain available for legacy scripted responses.
            payload = {**payload, 'evidence': [{k: v for k, v in row.items()
                if k != 'reference_options' or 'team' in row['payload']}
                for row in payload.get('evidence', [])]}
        if 'read_only_tools' in payload:
            # Argument schemas already live in ReadOnlyCheck.oneOf. Advertising
            # them twice could exhaust input_chars before the call budget.
            payload = {**payload, 'read_only_tools': {r: [
                {'name': item['function']['name'], 'description': item['function']['description'],
                 'args_schema': 'output_schema.$defs.ReadOnlyCheck.oneOf (match tool)'}
                for item in executor.schemas()] for r, executor in self.executors.items()}}
        model_input = payload if role == 'reviewer' and schema is ReviewDecision else {
            **payload, 'owner_options': owner_options(self)}
        messages = [SystemMessage(content=system_prompt or ROLE_PROMPTS[role]), HumanMessage(content=json.dumps({
            "input": model_input, "output_schema": output_schema}, ensure_ascii=False, separators=(',', ':')))]
        attempt = 0
        schema_version = hashlib.sha256(json.dumps(schema.model_json_schema(), sort_keys=True).encode()).hexdigest()[:12]
        while True:
            attempt += 1
            if role == 'reviewer':
                self.event('model_input', role, schema_name=schema.__name__, attempt=attempt,
                    input_chars=sum(len(str(m.content)) for m in messages),
                    system_chars=len(messages[0].content),
                    schema_chars=len(json.dumps(output_schema, ensure_ascii=False, separators=(',', ':'))),
                    source_rows=len(payload.get('evidence', [])))
            response = invoke_chat_model(self.models[role], messages, role, terminal=terminal)
            deadline = self.context.limits.seconds - (0 if terminal else self.context.limits.reserve_seconds)
            if time.monotonic() - self.context.started >= deadline:
                raise ExecutionError("TIME_BUDGET_EXCEEDED")
            self.event("model_output", role, output_chars=len(str(response.content)))
            try:
                if response.tool_calls or getattr(response, "invalid_tool_calls", None) or len(str(response.content)) > 12000:
                    raise ProtocolError("ROLE_OUTPUT_INVALID")
                content = response.content
                if role == 'supervisor':
                    data = json.loads(content)
                    if (isinstance(data, dict) and data.get('action') in
                            {'diagnose', 'request_info', 'escalate', 'finish'} and data.get('tasks')):
                        self.event('supervisor_fields_ignored', code='UNEXPECTED_TASKS',
                            action=data['action'], path='tasks',
                            ignored_count=len(data['tasks']) if isinstance(data['tasks'], list) else 1)
                        content = json.dumps({**data, 'tasks': []})
                value = (parse_owner_selection(schema, content, self) if issubclass(schema, EscalationSelection)
                         else parse_review_selection(content, self.evidence_needs) if schema is ReviewDecision
                         else schema.model_validate_json(content))
                result = validate(value) if validate else value
                if role == 'supervisor' and self.needs_collection_follow_up(result):
                    self.collection_follow_up_used = True
                    pending = [n.model_dump(mode='json') for n in result.evidence_needs
                               if is_blocking(n) and n.status == 'pending']
                    self.event('collection_follow_up', role, reason='unresolved_core_needs',
                               need_ids=[n['need_id'] for n in pending])
                    messages.extend([response, HumanMessage(content=
                        '准备进入诊断，但这些关键问题仍待评估：' + json.dumps(pending, ensure_ascii=False)
                        + '。声明的检查缺口：' + json.dumps(self.task_gaps(), ensure_ascii=False)
                        + '。先用已有观测评估其含义；合理替代解释确实不影响判断时，可说明理由后not_required。'
                        '若还缺有区分价值的观测且预算允许，选择一到两个未执行的登记只读检查，'
                        '用任务need_ids关联缺口，expected_value说明不同结果如何改变判断。'
                        '参考next_check及collection，已完整查空不能当作未查询或再次派相同检查。'
                        '无法查询、需要人工操作或没有有价值的新检查时，明确原因并保留缺口收尾。'
                        '不得为了通过而标supported，不必遍历所有渠道。此选择提醒全运行最多一次，仍返回完整JSON。')])
                    continue
                if role == 'reviewer' and schema is ReviewDecision and self.needs_review_follow_up(result):
                    self.review_follow_up_used = True
                    self.event('review_follow_up_correction', role,
                        target_ids=[a.target_id for a in result.assessments
                            if a.target_id.startswith(('H', 'A')) and a.verdict == 'uncertain'])
                    remaining = self.context.limits.model_calls - self.context.counts['model_calls'] - 1
                    messages.extend([response, HumanMessage(content=
                        '你已指出H/A或共享证据需求的关键缺口，却没有提出补查或解释为何不能补查。'
                        '请用request_evidence说明缺少哪个因果环节或操作条件，以及什么证据能改变判断，'
                        '交给Supervisor规划具体补查；无需提供工具参数。关联已有need_id或H/A编号，'
                        '只涉及建议条件时target_id选择该A编号，已支持的H可以保持supported。'
                        '若没有有用的只读检查、需要人工变更实验或条件不影响当前结论，'
                        '保留原评估并填写follow_up_reason；不得为完成流程编造检查或强行通过。'
                        + '本轮程序校验后的缺口：' + json.dumps(result.missing_information, ensure_ascii=False)
                        + '。'
                        f'本次回复后最多还剩{remaining}次模型请求，补查仍受原收尾预留与一轮上限约束。仅返回完整JSON。')])
                    continue
                return result
            except (ValidationError, ValueError, TypeError) as exc:
                details = {"reason": exc.code, "details": exc.details} if isinstance(exc, ProtocolError) else validation_details(exc, schema)
                self.event("validation_failure", role, **details, attempt=attempt,
                           model_call_index=self.context.counts['model_calls'],
                           repair_count=self.repairs['repairs'], schema_name=schema.__name__,
                           schema_version=schema_version)
                review_stage = schema.__name__ if role == 'reviewer' and self.reviewer_repair_mode == 'split' else None
                if not self.context.claim_repair(self.repairs, reviewer=role == 'reviewer', review_stage=review_stage):
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
                    raise ExecutionError("MODEL_OUTPUT_INVALID") from None
                self.event("validation_repair", role, review_stage=review_stage)
                instruction = '按提供的契约和已观测编号纠正一次。' + json.dumps(details, ensure_ascii=False)
                if role == 'reviewer':
                        instruction += ('。refs只选实际观测字段REF，未确认原因保留在unverified_causes。'
                        if schema is ObservationReview else
                        '。必须完整覆盖 required_target_ids，每个编号恰好一次；证据不足时评估为 uncertain，不得省略条目')
                elif role == 'diagnosis':
                    if details['reason'] in {'UNKNOWN_REFERENCE', 'DUPLICATE_REFERENCE', 'reference_mismatch'}:
                        instruction += ('。按 details.path 修正引用；REF_ 是字段引用编号，EV_ 是证据编号，不能混用。'
                            '同一编号可分别用于不同观测或假设，但每个 refs/support_refs/counter_refs 列表内不得重复。'
                            '本次可用引用：' + json.dumps(reference_options, ensure_ascii=False)
                            + '。选择与陈述含义相符的字段，不能用任意有效编号替换；证据不足则保留信息缺口。')
                    if self.ticket.purpose == 'status_check':
                        instruction += '。当前是 status_check；hypotheses 和 recommended_actions 必须为空列表，只输出可引用的观测和信息缺口'
                messages.extend([response, HumanMessage(content=instruction
                    + '。仅返回 JSON，不调用工具，不增加身份字段。')])

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
        tasks = [{k: row[k] for k in ('task_id', 'role', 'goal', 'need_ids', 'completion',
                                    'query_state_source', 'model_summary_verified')}
                 for row in self.task_input()]
        rework_budget = self.rework_budget(pending_model_calls=1, additional=self.reworks == 1)
        return {"targets": targets, "required_target_ids": list(targets),
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
                'instruction': '已有观测先评估；待查问题用need_ids关联新检查，expected_value说明区分价值。'
                    '完整空查询保留范围限制，避免重复；无法获取新信息时明确原因收尾，替代解释可有理由地not_required。'}

    def collection_coverage(self):
        return {'questions': self.need_input(),
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
        coverage = self.collection_coverage()
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
        request = review.request_evidence
        self.review_hint_status = []
        if request:
            self.evidence_ids(request.evidence_ids)
            if request.need_id is not None and request.need_id not in self.evidence_needs:
                raise ProtocolError('UNKNOWN_OR_DUPLICATE_NEED')
            disputed = request.target_id or request.hypothesis_id
            if (request.hypothesis_id is not None and request.hypothesis_id not in targets
                    or disputed is not None and (disputed not in targets
                        or disputed.startswith('H') and request.hypothesis_id not in {None, disputed}
                        or any(a.target_id == disputed and a.verdict == 'supported' for a in checked))
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
            required=self.blind_review or getattr(self.models['reviewer'], 'json_output', False), targets=targets)
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
                and nonblocking_trigger_request(self.draft, self.review, self.evidence_needs)):
            # Keep the actual request and unresolved verdict in the audit record.
            # Only the automatic routing is deferred to human follow-up.
            deferred = {'revision': revision, 'request': request.model_dump(mode='json'),
                        'reason': '机制和有效下一步已复核；纯触发未知作为人工待核实限制保留。'}
            self.deferred_review_requests.append(deferred)
            self.extra_gaps.append('非关键触发原因待人工核实：' + request.missing_observation)
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

    def collect_task(self, task, objective, executor, context, events, steps):
        before = dict(context.counts)
        if task.role == 'investigation' and self.investigation_mode == 'programmatic':
            result = self.execute_planned_reads(objective, executor, context)
        else:
            result = investigate(self.models[task.role], self.provider, self.principal, context=context,
                event_log=events, executor=executor, objective=objective,
                role=task.role, repair_state=self.repairs, max_steps=max(1, steps),
                approved_checks=objective['approved_checks'],
                system_prompt=INVESTIGATION_TASK if task.role == 'investigation' else None)
        result['task_counts'] = {key: context.counts[key] - before[key] for key in before}
        return result

    def execute_planned_reads(self, objective, executor, context):
        """Execute validated planned reads; Diagnosis owns evidence interpretation."""
        task_id, stop = str(uuid4()), ''
        checks = objective['required_checks']
        with activate(context), context.span('task', 'investigation') as span:
            def event(kind, **fields):
                context.emit({'type': kind, 'role': 'investigation', 'task_id': task_id,
                              'span_id': span['span_id'], 'parent_span_id': span['parent_span_id'], **fields})
            event('task_created', execution_mode='programmatic')
            event('task_started')
            for check in checks:
                # Do not automatically retry failures/truncation or re-read a superset.
                if completion([check], executor)['checks'][0]['status'] != 'not_executed':
                    event('planned_check_reused', name=check['tool'])
                    continue
                if not context.can_research():
                    stop = 'BUDGET_EXCEEDED'
                    break
                event('tool_selected', name=check['tool'], execution_source='validated_supervisor_plan')
                result = executor.execute(check['tool'], check['args'])
                event('tool_result', name=check['tool'], status=result.status,
                      evidence_ids=[e.evidence_id for e in result.evidence],
                      query_profile=dict(executor.last_query_profile), sample_order=result.sample_order,
                      truncated=result.truncated, error=result.error.model_dump() if result.error else None,
                      execution_source='validated_supervisor_plan')
                if result.error and result.error.code == 'BUDGET_EXCEEDED':
                    stop = 'BUDGET_EXCEEDED'
                    break
            coverage = completion(checks, executor)
            gaps = [*coverage['missing_information'], *coverage['limitations']]
            if not checks:
                gaps.append('No validated concrete checks were supplied; the program cannot execute a goal alone.')
                stop = 'NO_TOOL_PROGRESS'
            status = 'partial' if stop or coverage['missing_information'] or not checks else 'completed'
            event('task_completed', status=status, cause_confirmation='not_performed')
        return {'task_id': task_id, 'status': status, 'prompt_version': 'programmatic_reads_v1',
                'output': InvestigationOutput(findings=[], tentative_hypotheses=[],
                    missing_information=gaps[:8], escalation_team=None).model_dump(mode='json'),
                'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
                'run_summary': {**context.summary(), 'termination_reason': stop or 'FINISHED'}}

    def record_task(self, task, objective, result):
        self.tasks.append({"task_id": result["task_id"], "role": task.role, "goal": task.goal,
            'need_ids': objective['need_ids'],
            "expected_value": objective['expected_value'],
            "challenge_id": self.pending["challenge_id"] if self.dispatch_follow_up else None,
            "status": result["status"], "execution_status": result['status'], "output": result["output"],
            "required_checks": objective['required_checks'],
            "model_calls": result['task_counts']['model_calls'],
            "tool_calls": result['task_counts']['tool_calls'],
            "prompt_version": result["prompt_version"], "termination_reason": result["run_summary"]["termination_reason"],
            "evidence_ids": [e["evidence_id"] for e in result["evidence"]]})
        coverage = completion(objective['required_checks'], self.executors[task.role])
        self.event('task_coverage', task.role, task_id=result['task_id'],
                   execution_status=result['status'], completion_status=coverage['status'],
                   check_statuses=[c['status'] for c in coverage['checks']])
        executor = self.executors[task.role]
        self.evidence.update({k: v.model_copy(deep=True) for k, v in executor.evidence.items()})
        self.references.update({k: v.model_copy(deep=True) for k, v in executor.references.items()})

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
        result = self.collect_task(task, objective, self.executors[role], self.context, self.events, steps)
        self.record_task(task, objective, result)
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
        """Freeze both objectives and independent ledgers before graph fan-out."""
        from runtime.branch import BranchContext
        if (self.dispatch_follow_up or len(self.dispatch_queue) != 2 or
                {t.role for t in self.dispatch_queue} != {'investigation', 'knowledge'}):
            raise ProtocolError('INVALID_TASK_ROUTE')
        with self.context.lock:
            available_models = self.context.limits.model_calls - self.collection_reserve() - self.context.counts['model_calls']
            available_tools = self.context.limits.tool_calls - self.context.counts['tool_calls']
        if available_models < 4 or available_tools < 2 or not self.context.can_research():
            raise ExecutionError('BUDGET_EXCEEDED')
        self.parallel_batch = str(uuid4())
        self.parallel_plans = {}
        # Stable ordering for budget slices and result publication, independent
        # of completion order. Unused slices are available in later rounds.
        tasks = sorted(self.dispatch_queue, key=lambda t: t.role != 'investigation')
        for index, task in enumerate(tasks):
            model_cap = min(4, available_models // 2 + (available_models % 2 if index == 0 else 0))
            tool_cap = available_tools // 2 + (available_tools % 2 if index == 0 else 0)
            child = BranchContext(self.context, model_calls=model_cap, tool_calls=tool_cap)
            self.parallel_plans[task.role] = {'task': task, 'objective': self.task_objective(task, False),
                'context': child, 'executor': self.executors[task.role].fork(child), 'steps': model_cap}
        self.parallel_batches += 1
        self.event('parallel_collection_started', batch_id=self.parallel_batch,
            roles=list(self.parallel_plans), allocations={role: {'model_calls': p['steps'],
                'tool_calls': p['context'].tool_cap} for role, p in self.parallel_plans.items()})

    def collect_parallel_role(self, role):
        plan = self.parallel_plans[role]
        child, executor = plan['context'], plan['executor']
        try:
            result = self.collect_task(plan['task'], plan['objective'], executor, child, child.events, plan['steps'])
        except Exception as exc:
            code = exc.code if isinstance(exc, (ExecutionError, ProtocolError)) else 'INTERNAL_ERROR'
            child.error(ExecutionError(code), role)
            task_id = next((e['task_id'] for e in child.events if e['type'] == 'task_started'), str(uuid4()))
            result = {'task_id': task_id, 'status': 'failed', 'prompt_version': 'parallel_branch_failure',
                'output': InvestigationOutput(missing_information=[f'{role}任务未完成（{code}）；保留已取得证据。']).model_dump(mode='json'),
                'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
                'task_counts': dict(child.counts), 'run_summary': {**child.summary(), 'termination_reason': code}}
            child.emit({'type': 'task_completed', 'role': role, 'task_id': task_id, 'status': 'failed', 'reason': code})
        return {'batch_id': self.parallel_batch, 'result': result}

    def merge_parallel(self, outcomes):
        if set(outcomes) != set(self.parallel_plans) or any(
                row['batch_id'] != self.parallel_batch for row in outcomes.values()):
            raise ProtocolError('INVALID_TASK_ROUTE')
        stops, incomplete = [], False
        for role, plan in self.parallel_plans.items():
            result = outcomes[role]['result']
            executor = plan['executor']
            executor.context = self.context
            self.executors[role] = executor
            self.record_task(plan['task'], plan['objective'], result)
            if result['status'] != 'completed':
                incomplete = True
                code = result['run_summary']['termination_reason']
                stops.append(code)
                self.extra_gaps.extend(result['output']['missing_information'])
                self.event('parallel_branch_incomplete', role, batch_id=self.parallel_batch, code=code)
        self.event('parallel_collection_joined', batch_id=self.parallel_batch,
                   roles=list(self.parallel_plans), incomplete=incomplete)
        self.dispatch_queue.clear()
        self.parallel_batch = None
        self.parallel_plans = {}
        # A failed branch never discards the sibling's observations. Current
        # observations still receive diagnosis/review; knowledge alone cannot.
        fatal = next((code for code in stops if code in {'AUTH_ERROR', 'QUOTA_EXCEEDED',
            'SCOPE_DENIED', 'TOOL_DENIED', 'WINDOW_DENIED'}), None)
        if fatal:
            self.stop = fatal
        elif incomplete:
            if any(e.kind == 'observation' for e in self.evidence.values()):
                self.next_node = 'diagnosis'
            else:
                self.stop = stops[0] if stops else 'NEEDS_INFORMATION'
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
        """Expose validated source facts separately, without claiming semantic review."""
        if self.status() == 'completed':
            return []
        seen = {(r['evidence_id'], r['field_path']) for f in approved_findings for r in f['refs']}
        result = []
        for task in self.tasks:
            if task['role'] != 'investigation':
                continue
            for data in task['output']['findings']:
                finding = Finding.model_validate_json(json.dumps(data))
                validate_output(InvestigationOutput(findings=[finding]), self.evidence)
                keys = {(r.evidence_id, r.field_path) for r in finding.refs}
                if keys - seen:
                    result.append(finding.model_dump(mode='json'))
                    seen.update(keys)
                if len(result) >= 8:
                    return result
        return result

    def run(self):
        from workflow.graph import build_graph
        with activate(self.context), self.context.span("workflow", "incident_collaboration") as span:
            self.workflow_span = span['span_id']
            build_graph(self).invoke({}, config={"recursion_limit": 40, 'max_concurrency': 2})
        return self.result()

    def status(self):
        if self.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} and self.review is not None:
            return "completed"
        return "partial" if self.evidence or self.stop in {"NEEDS_INFORMATION", "ESCALATED", "BUDGET_EXCEEDED", "SUPERVISOR_LIMIT", "NO_TOOL_PROGRESS"} else "failed"

    def status_check_coverage(self):
        capabilities = self.executors['investigation'].capabilities()
        metrics = sorted({e.payload['metric'] for e in self.evidence.values()
                          if e.kind == 'observation' and 'metric' in e.payload})
        log_categories = sorted({e['query_profile']['category'] for e in self.collection()
            if e['name'] == 'get_service_logs' and e['status'] in {'ok', 'empty'} and 'category' in e['query_profile']})
        notes = []
        missing_metrics = sorted(set(capabilities.get('registered_metrics', {})) - set(metrics))
        missing_logs = sorted(set(capabilities.get('registered_log_categories', [])) - set(log_categories))
        if missing_metrics:
            notes.append('尚未取得这些登记指标的样本：' + '、'.join(missing_metrics) + '。')
        if missing_logs:
            notes.append('尚未核查这些登记日志类别：' + '、'.join(missing_logs) + '。')
        for event in self.collection():
            if event['status'] == 'error':
                notes.append(event['name'] + ' 未完成，原因：' + (event.get('error') or {}).get('code', 'UNKNOWN') + '。')
            if event['truncated']:
                notes.append(event['name'] + ' 返回截断样本，不能代表完整查询窗口。')
            if event['status'] == 'empty':
                notes.append(event['name'] + ' 在所选窗口及过滤条件内无匹配记录，不能据此确认正常。')
        intervals = sorted({str(e.payload.get('granularity')) for e in self.evidence.values() if 'metric' in e.payload})
        if intervals:
            notes.append('指标实际采样间隔：' + '、'.join(intervals) + '；不能排除采样点之间的短暂异常。')
        notes.append('核查仅覆盖所列观测；未登记的健康、资源或业务指标不在本次覆盖范围内，整体健康尚未确认。')
        return {'checked_metrics': metrics, 'checked_log_categories': log_categories,
                'unchecked_metrics': missing_metrics, 'unchecked_log_categories': missing_logs,
                'limitations': notes}

    def result(self):
        from workflow.graph import WORKFLOW_VERSION
        self.extra_gaps.extend(self.task_gaps())
        self.extra_gaps.extend(limitations(self.evidence_needs))
        approved = {a.target_id for a in self.review.assessments if a.verdict == "supported"} if self.review else set()
        findings, hypotheses, actions, pending_actions, used = [], [], [], [], set()
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
            actions, pending_actions = action_projection(self.draft, self.review)
            self.extra_gaps.extend(self.draft.missing_information)
            self.team = self.draft.escalation_team or self.team
        if self.review:
            self.extra_gaps.extend(self.review.missing_information)
            for assessment in self.review.need_assessments:
                if assessment.verdict == 'supported':
                    used.update(assessment.evidence_ids)
            for a in self.review.assessments:
                if a.verdict == "supported":
                    used.update(a.evidence_ids)
                else:
                    self.extra_gaps.append(f"{a.target_id}: {a.reason}")
        if self.status() != "completed":
            if self.draft and self.review:
                self.extra_gaps.extend(readiness_gaps(self.draft, self.review, self.ticket.purpose))
            if self.observation_gaps():
                self.extra_gaps.append(gap_message(self.observation_gaps()))
            self.extra_gaps.append(f"协作尚有缺口：{self.stop}；需要补充信息或人工升级。")
        completed = self.status() == 'completed'
        business_result = ('status_checked' if self.ticket.purpose == 'status_check' else 'diagnosis_available') if completed else 'escalation_recommended' if self.team else 'needs_information'
        return {"run_id": self.context.run_id, "task_type": "incident_collaboration", "incident_id": self.ticket.incident_id,
            'purpose': self.ticket.purpose,
            'status_check_coverage': self.status_check_coverage() if self.ticket.purpose == 'status_check' else None,
            "scope": self.ticket.scope.model_dump(mode="json"), "data_source": self.provider.name, "status": self.status(),
            "review_status": "passed" if self.stop in {'FINISHED', 'STATUS_CHECK_COMPLETED'} else "needs_information" if self.review else "not_performed",
            "business_result": business_result,
            'pending_actions': pending_actions,
            'rejected_actions': self.draft.rejected_actions if self.draft else [],
            'unreviewed_observations': self.unreviewed_observations(findings),
            'protocol_finalization_used': self.protocol_fallback_used,
            'diagnosis_readiness': {'policy_version': 'diagnosis_readiness_v2',
                'report_status': ('reviewed_with_limitations' if self.extra_gaps else 'reviewed') if completed else 'incomplete',
                'blocking_gaps': self.reviewed_gaps() if self.draft and self.review else self.diagnosis_completion_gaps(),
                'meaning': '报告可供决策不等于确认唯一根因、条件已满足、批准修复或工单解决。'},
            'diagnostic_audit': diagnostic_audit(self.evidence_needs, self.review, self.draft,
                self.executors, self.tasks, stop=self.stop, events=self.events),
            "output": {"findings": findings, "hypotheses": hypotheses, "recommended_actions": actions,
                       "missing_information": list(dict.fromkeys(self.extra_gaps)), "escalation_team": self.team},
            "used_evidence_ids": sorted(used), "evidence": [e.model_dump(mode="json") for e in self.evidence.values()],
            "task_results": self.task_results(), "drafts": self.drafts, "reviews": self.reviews, "negotiation": self.negotiation,
            'blind_review_enabled': self.blind_review, 'observation_reviews': self.observation_reviews,
            'review_contract_version': 'question_owned_blocker_v2',
            'deferred_review_requests': self.deferred_review_requests,
            'evidence_needs': self.need_input(), 'evidence_needs_version': 'question_ledger_v1',
            'collection_exits': self.collection_exits,
            'evidence_needs_validation': 'model_semantics_with_program_guards',
            "events": self.events, "run_summary": self.context.summary(), "repairs": self.repairs["repairs"],
            'repair_policy': REVIEW_STAGE_POLICY if self.reviewer_repair_mode == 'split' else REPAIR_POLICY,
            'reviewer_repair_mode': self.reviewer_repair_mode,
            'repair_counts': {'collection': self.repairs['repairs'] - self.repairs.get('reviewer_repairs', 0),
                              'reviewer': self.repairs.get('reviewer_repairs', 0)},
            "fact_rendering": FACT_RENDERING, "coverage_gaps": self.observation_gaps(), 'model_input_view': MODEL_VIEW_VERSION,
            "rework_rounds": self.reworks, "supervisor_decisions": self.supervisor_calls,
            "scheduling_decisions": self.decisions,
            "workflow_engine": "langgraph", "workflow_version": WORKFLOW_VERSION,
            'collection_execution': {'mode': 'parallel_when_independent' if self.parallel_collection else 'serial',
                'investigation_mode': self.investigation_mode,
                'parallel_batches': self.parallel_batches,
                'max_parallel_roles': 2, 'rework_parallel': False},
            "prompt_version": self.prompt_version()}

    def prompt_version(self):
        from agents.prompts import KNOWLEDGE
        from agents.investigation_prompt import SYSTEM
        schemas = [SupervisorSelection.model_json_schema(), DiagnosisSelection.model_json_schema(), ReviewDecision.model_json_schema()]
        payload = [ROLE_PROMPTS, SYSTEM, INVESTIGATION_TASK, KNOWLEDGE,
                   OBSERVATION_REVIEW if self.blind_review else None, schemas,
                   ObservationReview.model_json_schema() if self.blind_review else None,
                   *[ex.schemas() for ex in self.executors.values()]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def collaborate(models, provider, principal, *, limits=INCIDENT_LIMITS, emit=None, context=None, event_log=None,
                parallel_collection=True, blind_review=None, investigation_mode='programmatic', reviewer_repair_mode='shared'):
    return Collaboration(models, provider, principal, limits=limits, emit=emit, context=context,
                         event_log=event_log, parallel_collection=parallel_collection, blind_review=blind_review,
                         investigation_mode=investigation_mode, reviewer_repair_mode=reviewer_repair_mode).run()
