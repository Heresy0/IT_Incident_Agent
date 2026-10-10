"""Shared questions and mechanical guards; neither gold nor a semantic judge."""
from agents.contracts import EvidenceNeed, NeedAssessment
from evidence.diagnosis import ProtocolError
from runtime.context import ExecutionError


CORE = {'symptom', 'mechanism', 'alternative'}


def is_blocking(need):
    if need.purpose in {'symptom', 'mechanism'}:
        return True
    return need.blocking if need.blocking is not None else need.purpose in CORE


def initial_needs(ticket):
    questions = [('symptom', '工单所述现象有哪些当前观测支持，时间和范围是否一致？')]
    if ticket.purpose == 'diagnosis':
        questions += [('mechanism', '候选故障机制如何解释现象，关键因果环节有哪些观测或缺口？'),
                      ('alternative', '哪些合理的替代解释会改变判断，现有对照能区分到什么程度？')]
    return {f'N{n}': EvidenceNeed(need_id=f'N{n}', question=question, purpose=purpose,
            reason='尚未取证或评估；查询完成不能自动解决这个问题。')
            for n, (purpose, question) in enumerate(questions, 1)}


def check_ids(ids, evidence):
    if len(ids) != len(set(ids)) or any(eid not in evidence for eid in ids):
        raise ProtocolError('UNKNOWN_OR_DUPLICATE_EVIDENCE')


def current_support(ids, evidence):
    return any(evidence[eid].kind == 'observation' for eid in ids)


def update_needs(previous, proposed, evidence, executors, purpose):
    if not proposed:
        return dict(previous)  # Older responses cannot erase server-seeded questions.
    ids = [need.need_id for need in proposed]
    violations, normalized_checks = [], {}
    for need in proposed:
        # Check permissions and references before collecting recoverable ledger
        # errors, so an invalid core flag cannot mask an unsafe proposal.
        check_ids(need.evidence_ids, evidence)
        if need.next_check:
            try:
                args = executors[need.role].validate_args(need.next_check.tool, need.next_check.args)
            except ExecutionError as exc:
                raise ProtocolError('INVALID_NEED_CHECK', [{'need_id': need.need_id,
                    'path': 'next_check', 'code': exc.code,
                    'instruction': '下一项检查须使用已登记且在工单范围内的只读能力；不可查询时省略检查并说明缺口。'}]) from None
            normalized_checks[need.need_id] = need.next_check.model_copy(
                update={'args': args.model_dump(mode='json')})
        if need.need_id in previous and need.purpose != previous[need.need_id].purpose:
            violations.append({'need_id': need.need_id, 'path': 'purpose',
                'code': 'EVIDENCE_NEED_PURPOSE_CHANGED', 'allowed': [previous[need.need_id].purpose]})
        if need.purpose in {'symptom', 'mechanism'}:
            if need.status == 'not_required':
                violations.append({'need_id': need.need_id, 'path': 'status',
                    'code': 'REQUIRED_EVIDENCE_NEED', 'allowed': ['pending', 'supported', 'unavailable', 'deferred']})
            if need.blocking is False:
                violations.append({'need_id': need.need_id, 'path': 'blocking',
                    'code': 'REQUIRED_EVIDENCE_NEED', 'allowed': [True, None]})
    if len(ids) != len(set(ids)) or not set(previous) <= set(ids):
        violations.insert(0, {'path': 'evidence_needs', 'code': 'EVIDENCE_NEED_COVERAGE',
            'required_need_ids': list(previous),
            'instruction': '保留全部已有N编号及用途；待核实问题不能因任务执行完而被删除。'})
    if violations:
        raise ProtocolError(violations[0]['code'], violations)
    result = {}
    for need in proposed:
        if purpose == 'status_check' and need.purpose != 'symptom':
            raise ProtocolError('STATUS_CHECK_SCOPE')
        if need.status == 'supported' and not current_support(need.evidence_ids, evidence):
            raise ProtocolError('EVIDENCE_NEED_NO_CURRENT_SUPPORT')
        changes = {'question': previous[need.need_id].question} if need.need_id in previous else {}
        if need.next_check:
            changes['next_check'] = normalized_checks[need.need_id]
        need = need.model_copy(update=changes)
        result[need.need_id] = need
    return result


def apply_need_updates(previous, updates, additions, evidence, executors, purpose):
    """Build a full ledger on the server, validate atomically, never delete questions."""
    ids = [n.need_id for n in updates]
    new_ids = [n.need_id for n in additions]
    for update in updates:
        check_ids(update.evidence_ids, evidence)
        if update.next_check:
            try:
                executors[update.role].validate_args(update.next_check.tool, update.next_check.args)
            except ExecutionError as exc:
                raise ProtocolError('INVALID_NEED_CHECK', [{'need_id': update.need_id,
                    'path': 'next_check', 'code': exc.code}]) from None
    if len(ids) != len(set(ids)) or not set(ids) <= set(previous):
        raise ProtocolError('UNKNOWN_OR_DUPLICATE_NEED', [{'path': 'need_updates.need_id',
            'allowed': list(previous)}])
    if (len(new_ids) != len(set(new_ids)) or set(new_ids) & set(previous)
            or len(previous) + len(additions) > 6):
        raise ProtocolError('EVIDENCE_NEED_COVERAGE', [{'path': 'additional_needs.need_id',
            'instruction': '新增问题须使用未占用的N编号，总数不超过六；已有问题由程序保留。'}])
    result = dict(previous)
    for update in updates:
        changes = {key: getattr(update, key) for key in
                   ('status', 'evidence_ids', 'reason', 'next_check', 'role')}
        if update.blocking is not None:
            # This scheduling flag cannot weaken the core completion gate.
            if result[update.need_id].purpose not in {'symptom', 'mechanism'}:
                changes['blocking'] = update.blocking
        result[update.need_id] = result[update.need_id].model_copy(update=changes)
    for addition in additions:
        result[addition.need_id] = EvidenceNeed(**addition.model_dump())
    return update_needs(previous, list(result.values()), evidence, executors, purpose)


def validate_assessments(needs, assessments, evidence):
    ids = [a.need_id for a in assessments]
    if len(ids) != len(set(ids)) or not set(ids) <= set(needs):
        raise ProtocolError('REVIEW_NEED_COVERAGE')
    result = []
    for assessment in assessments:
        check_ids(assessment.evidence_ids, evidence)
        need = needs[assessment.need_id]
        if assessment.blocking is False and need.purpose in {'symptom', 'mechanism'}:
            raise ProtocolError('REQUIRED_EVIDENCE_NEED')
        if assessment.verdict == 'not_required' and need.purpose in {'symptom', 'mechanism'}:
            raise ProtocolError('REQUIRED_EVIDENCE_NEED')
        if assessment.verdict == 'supported' and not current_support(assessment.evidence_ids, evidence):
            assessment = assessment.model_copy(update={'verdict': 'uncertain',
                'reason': '程序门禁：关键证据问题没有当前观测引用，历史线索或任务完成不足以支持。'})
        result.append(assessment)
    for need_id in needs:
        if need_id not in ids:
            result.append(NeedAssessment(need_id=need_id, verdict='uncertain',
                reason='复核未评估此证据问题；不能凭局部任务或F/H/A通过宣告完整。'))
    return result


def reviewed_needs(needs, assessments):
    result = dict(needs)
    for assessment in assessments:
        need = needs[assessment.need_id]
        status = {'supported': 'supported', 'not_required': 'not_required'}.get(assessment.verdict)
        if status is None:
            status = need.status if need.status in {'unavailable', 'deferred'} else 'pending'
        result[need.need_id] = need.model_copy(update={'status': status,
            'reason': assessment.reason, 'evidence_ids': assessment.evidence_ids,
            'blocking': need.blocking if assessment.blocking is None else assessment.blocking,
            'next_check': None if status in {'supported', 'not_required'} else need.next_check})
    return result


def limitations(needs, *, blocking_only=False):
    return [f'证据需求{need.need_id}（{need.question}）：{need.reason}' for need in needs.values()
            if need.status not in {'supported', 'not_required'}
            and (not blocking_only or is_blocking(need))]
