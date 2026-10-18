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
        questions += [('mechanism', '当前观测支持哪个近端故障机制，能否解释现象？为何开始发生应另列触发问题，不混入此项。'),
                      ('alternative', '哪些合理的替代机制会改变当前机制判断或下一步，现有对照能区分到什么程度？仅触发来源未知应另列trigger。')]
    return {f'N{n}': EvidenceNeed(need_id=f'N{n}', question=question, purpose=purpose,
            reason='尚未取证或评估；查询完成不能自动解决这个问题。')
            for n, (purpose, question) in enumerate(questions, 1)}


def check_ids(ids, evidence, path='evidence_ids'):
    errors, seen = [], set()
    for index, eid in enumerate(ids):
        if eid not in evidence or eid in seen:
            errors.append({'path': f'{path}.{index}',
                'type': 'unknown_evidence_id' if eid not in evidence else 'duplicate_evidence_id',
                'allowed': list(evidence),
                'instruction': '逐字选择当前input.evidence中的EV编号；不猜测或近似匹配。REF/RD不能填入evidence_ids，同一列表不重复。'})
        seen.add(eid)
    if errors:
        raise ProtocolError('UNKNOWN_OR_DUPLICATE_EVIDENCE', errors[:5])


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
                    'role': need.role,
                    'allowed_tools': [s['function']['name'] for s in executors[need.role].schemas()],
                    'instruction': '显式给role并从read_only_tools[role]选择；start/end保持ticket.scope内。不可查询时next_check=null并说明缺口。'}]) from None
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
        raise ProtocolError('REVIEW_NEED_COVERAGE', [{'path': 'need_assessments.need_id',
            'allowed': list(needs),
            'instruction': '只评估已有N编号，每个编号至多一次；缺证据使用uncertain。'}])
    result = []
    violations = []
    for index, assessment in enumerate(assessments):
        check_ids(assessment.evidence_ids, evidence, f'need_assessments.{index}.evidence_ids')
        need = needs[assessment.need_id]
        if assessment.blocking is False and need.purpose in {'symptom', 'mechanism'}:
            violations.append({'need_id': need.need_id,
                'path': f'need_assessments.{index}.blocking', 'code': 'REQUIRED_EVIDENCE_NEED',
                'actual': False, 'allowed': [True, None],
                'instruction': '症状与机制是核心问题，不可关闭阻塞；证据不足时保持uncertain并交接缺口。'})
        if assessment.verdict == 'not_required' and need.purpose in {'symptom', 'mechanism'}:
            violations.append({'need_id': need.need_id,
                'path': f'need_assessments.{index}.verdict', 'code': 'REQUIRED_EVIDENCE_NEED',
                'actual': 'not_required', 'allowed': ['supported', 'uncertain'],
                'instruction': '症状与机制必须评估；有当前证据才supported，否则uncertain，不得为通过而编造支持。'})
        if assessment.verdict == 'supported' and not current_support(assessment.evidence_ids, evidence):
            assessment = assessment.model_copy(update={'verdict': 'uncertain',
                'reason': '程序门禁：关键证据问题没有当前观测引用，历史线索或任务完成不足以支持。'})
        result.append(assessment)
    if violations:
        raise ProtocolError('REQUIRED_EVIDENCE_NEED', violations)
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
