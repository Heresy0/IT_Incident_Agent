"""Model-facing schemas and auditable correction accounting."""
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping
from agents.contracts import EvidenceNeed
from evidence.contracts import Evidence, EvidenceRef
import json

REPAIR_POLICY = 'collection_one_review_one_v1'
REVIEW_STAGE_POLICY = 'collection_one_review_stages_one_v2'
ROLE_REPAIR_POLICY = 'supervisor_diagnosis_reviewer_one_v3'


def compact_schema(schema):
    """Remove presentation metadata, preserving validation constraints."""
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {key: compact_schema(value) for key, value in schema.items()
            if key not in {'title', 'examples'}}


def bind_evidence_ids(schema, evidence_ids, *, nested=False):
    """Advertise only observed EV identifiers; an empty registry means []."""
    if isinstance(schema, list):
        return [bind_evidence_ids(item, evidence_ids, nested=True) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: bind_evidence_ids(value, evidence_ids, nested=True) for key, value in schema.items()}
    for field in ('evidence_ids',):
        definition = result.get('properties', {}).get(field)
        if definition and definition.get('type') == 'array':
            if evidence_ids:
                definition['items'] = {'$ref': '#/$defs/ObservedEvidenceId'}
            else:
                definition['maxItems'] = 0
    if not nested and evidence_ids:
        result.setdefault('$defs', {})['ObservedEvidenceId'] = {'type': 'string', 'enum': list(evidence_ids)}
    return result


def bind_reference_ids(schema, executor):
    """Refresh the leaf's field-reference choices after each actual tool read."""
    from evidence.model_view import substantive
    schema = deepcopy(schema)
    ids = [option.reference_id for e in executor.evidence.values()
           for option in e.reference_options if substantive(option.field_path)]
    if ids and 'ReferenceSelection' in schema.get('$defs', {}):
        schema['$defs']['ReferenceSelection']['properties']['reference_id']['enum'] = ids
    elif not ids:
        for field in ('findings', 'directly_supported'):
            if field in schema.get('properties', {}):
                schema['properties'][field]['maxItems'] = 0
    return compact_schema(schema)


def supervisor_schema(schema, executors):
    """Show role/tool compatibility without duplicating the argument schemas."""
    schema = deepcopy(schema)
    for definition, check_field in (('TaskRequest', 'checks'), ('EvidenceNeedUpdate', 'next_check')):
        if definition not in schema['$defs']:
            continue
        constraints = []
        for role, executor in executors.items():
            names = [s['function']['name'] for s in executor.schemas()]
            check = {'properties': {'tool': {'enum': names}}}
            field = {'items': check} if check_field == 'checks' else {
                'anyOf': [{'type': 'null'}, {'type': 'object', **check}]}
            constraints.append({
                'if': {'properties': {'role': {'const': role}}, 'required': ['role']},
                'then': {'properties': {check_field: field}}})
        schema['$defs'][definition].setdefault('allOf', []).extend(constraints)
    if 'EvidenceNeedUpdate' in schema['$defs']:
        schema['$defs']['EvidenceNeedUpdate'].setdefault('required', []).append('role')
    return schema


def diagnosis_schema(schema, *, canonical_checks=False):
    schema = deepcopy(schema)
    schema['$defs']['RecommendedAction']['allOf'] = [{
        'if': {'properties': {'kind': {'const': 'read_only_check'}}, 'required': ['kind']},
        'then': {'properties': {'checks': {'minItems': 1}, 'repair': {'type': 'null'}}}}, {
        'if': {'properties': {'kind': {'const': 'manual_check'}}, 'required': ['kind']},
        'then': {'properties': {'checks': {'maxItems': 0}, 'repair': {'type': 'null'}}}}]
    if canonical_checks:
        from agents.contracts import CHECK_EXPECTED_RESULTS
        for constraint in schema['$defs']['RecommendedAction']['allOf']:
            kind = constraint['if']['properties']['kind']['const']
            constraint['then']['properties']['expected_result'] = {'const': CHECK_EXPECTED_RESULTS[kind]}
    return schema


def reviewer_schema(schema, needs, targets, *, conservative=False, bounded_refutation=False):
    schema = deepcopy(schema)
    need_definition = 'RevisionNeedAssessment' if 'RevisionNeedAssessment' in schema['$defs'] else 'NeedAssessment'
    assessment = schema['$defs']['Assessment']
    if bounded_refutation:
        assessment.setdefault('allOf', []).append({
            'if': {'properties': {'target_id': {'enum': [k for k in targets if k.startswith('H')]},
                                  'verdict': {'const': 'not_supported'}},
                   'required': ['target_id', 'verdict']},
            'then': {'properties': {'refutation_basis': {'$ref': '#/$defs/RefutationBasis'}},
                     'required': ['refutation_basis']}})
    else:
        assessment['properties'].pop('refutation_basis', None)
    core = [n.need_id for n in needs.values() if n.purpose in {'symptom', 'mechanism'}]
    schema['$defs'][need_definition]['allOf'] = [{
        'if': {'properties': {'need_id': {'enum': core}}, 'required': ['need_id']},
        'then': {'properties': {'verdict': {'enum': ['supported', 'uncertain']},
                                'blocking': {'enum': [None]}}}}]
    # Expose the existing blocker ownership guard before generation. This only
    # restricts contradictory edges; it does not decide any semantic verdict.
    strict_scope = 'RevisionRequest' in schema['$defs']
    for purpose in ('mechanism', 'alternative', *(['symptom'] if strict_scope else [])):
        need_ids = [n.need_id for n in needs.values() if n.purpose == purpose]
        eligible = [key for key, target in targets.items()
                    if not (key.startswith('H') and target.level == 'trigger')]
        if need_ids and (not conservative or strict_scope):
            schema['$defs'][need_definition]['allOf'].append({
                'if': {'properties': {'need_id': {'enum': need_ids}}, 'required': ['need_id']},
                'then': {'properties': {'blocker': {'anyOf': [{'type': 'null'}, {
                    'type': 'object', 'properties': {'affected_targets': {
                        'contains': {'type': 'string', 'enum': eligible}, 'minContains': 1}}}]}}}})
    if 'RevisionRequest' in schema['$defs']:
        schema['$defs']['RevisionRequest']['properties']['target_id']['enum'] = [key for key in targets if key.startswith(('H', 'A'))]
    if need_definition == 'RevisionNeedAssessment':
        definition = schema['$defs'][need_definition]
        definition['properties']['revision_targets']['items'] = {'enum': [key for key in targets if key.startswith(('H', 'A'))]}
        definition['allOf'].append({
            'if': {'properties': {'gap_kind': {'const': 'revision'}}, 'required': ['gap_kind']},
            'then': {'properties': {'verdict': {'const': 'uncertain'}, 'blocker': {'type': 'null'},
                'revision_targets': {'minItems': 1}, 'evidence_ids': {'minItems': 1}},
                'required': ['revision_targets', 'evidence_ids']}})
    request = schema['$defs']['EvidenceRequest']['properties']
    request['need_id'] = {'enum': [*needs, None]}
    request['hypothesis_id'] = {'enum': [k for k in targets if k.startswith('H')] + [None]}
    request['target_id'] = {'enum': [k for k in targets if k.startswith(('H', 'A'))] + [None]}
    for level, excluded in (('trigger', {'symptom', 'mechanism', 'alternative'}),
                            ('mechanism', {'trigger'}), ('alternative', {'trigger'})):
        target_ids = [key for key, target in targets.items()
                      if key.startswith('H') and target.level == level]
        if target_ids:
            schema['$defs']['EvidenceRequest'].setdefault('allOf', []).append({
                'if': {'properties': {'target_id': {'enum': target_ids}}, 'required': ['target_id']},
                'then': {'properties': {'need_id': {'enum': [
                    key for key, need in needs.items() if need.purpose not in excluded] + [None]}}}})
    if 'ReviewBlocker' in schema['$defs']:
        schema['$defs']['ReviewBlocker']['properties']['affected_targets']['items'] = {
            'type': 'string', 'enum': list(targets)}
    # The reviewer describes evidence gaps; only Supervisor plans executable
    # checks. Keep the public runtime contract compatible with older reports.
    request.pop('hypothesis_id', None)
    request.pop('proposed_check', None)
    request.pop('target_role', None)
    request['checks'] = {'type': 'array', 'maxItems': 0, 'default': []}
    blocker = schema['$defs']['ReviewBlocker']['properties']
    # A blocker belongs to an existing question. Its category is derived from
    # that question, rather than asking the model to classify it a second time.
    blocker.pop('cause_level', None)
    schema['$defs']['ReviewBlocker']['required'].remove('cause_level')
    blocker.pop('target_role', None)
    blocker['checks'] = {'type': 'array', 'maxItems': 0, 'default': []}
    blocker['resolution']['enum'] = ['external_source', 'not_planned']
    return review_contract_view(schema)


def revision_review_generation(schema, needs, targets, evidence):
    """Present mutually exclusive need rows; never supply semantic verdicts."""
    schema = deepcopy(schema)
    observation_ids = [key for key, item in evidence.items() if item.kind == 'observation']
    source_rule = {'minItems': 1, 'uniqueItems': True,
                   'contains': {'enum': observation_ids}, 'minContains': 1}
    properties = deepcopy(schema['$defs']['RevisionNeedAssessment']['properties'])
    rows = []
    for need in needs.values():
        eligible = [key for key, target in targets.items()
                    if not (need.purpose in {'symptom', 'mechanism', 'alternative'}
                             and key.startswith('H') and target.level == 'trigger')]
        editable = [key for key in eligible if key.startswith(('H', 'A'))]
        # The causal target boundary is owned by the registered question. This
        # does not establish whether any eligible target is supported.
        row_properties = {**properties, 'need_id': {'const': need.need_id}}
        if need.purpose in {'symptom', 'mechanism'}:
            row_properties.pop('blocking', None)
        settled = {'properties': {'verdict': {'enum': ['supported', 'not_required']},
            'gap_kind': {'const': 'evidence'}, 'blocker': {'type': 'null'},
            'revision_targets': {'maxItems': 0}}}
        if need.purpose in {'symptom', 'mechanism'}:
            settled['properties']['verdict'] = {'const': 'supported'}
        settled['allOf'] = [{'if': {'properties': {'verdict': {'const': 'supported'}}, 'required': ['verdict']},
                            'then': {'properties': {'evidence_ids': source_rule}, 'required': ['evidence_ids']}}]
        pending_edit = {'properties': {'verdict': {'const': 'uncertain'}, 'gap_kind': {'const': 'revision'},
            'blocker': {'type': 'null'}, 'revision_targets': {'minItems': 1, 'uniqueItems': True, 'items': {'enum': editable}},
            'evidence_ids': source_rule}, 'required': ['gap_kind', 'revision_targets', 'evidence_ids']}
        pending_evidence = {'properties': {'verdict': {'const': 'uncertain'}, 'gap_kind': {'const': 'evidence'},
            'revision_targets': {'maxItems': 0}, 'blocker': {'allOf': [{'$ref': '#/$defs/ReviewBlocker'},
                {'properties': {'affected_targets': {'contains': {'enum': eligible}, 'minContains': 1}}}]}},
            'required': ['blocker']}
        rows.append({'type': 'object', 'properties': row_properties, 'required': ['need_id', 'verdict', 'reason'],
                     'additionalProperties': False, 'oneOf': [settled, pending_edit, pending_evidence]})
    schema['properties']['need_assessments'].update(prefixItems=rows, items=False,
                                                    minItems=len(rows), maxItems=len(rows))
    return review_contract_view(schema)


def revision_review_focus(needs, targets):
    """Expose ownership of uncertainty without classifying its materiality."""
    trigger_ids = [key for key, target in targets.items() if key.startswith('H') and target.level == 'trigger']
    return [{'need_id': need.need_id, 'purpose': need.purpose,
             'excluded_trigger_targets': trigger_ids if need.purpose in {'symptom', 'mechanism', 'alternative'} else [],
             'decision_test': '判断缺口是否会改变当前机制或下一步；仅解释为何开始的未知保留为限制。',
             'output_forms': {'settled': '独立有支持或有理由不要求；无blocker、无revision_targets。',
                 'revision': '已有当前EV、稿中论证待改：uncertain＋revision_targets，无blocker／相关补查。',
                 'evidence': '确实缺影响判断的新观测：uncertain＋实际blocker，无revision_targets。'},
             'core_blocking': '程序控制，省略blocking' if need.purpose in {'symptom', 'mechanism'} else '独立评估重要性'}
            for need in needs.values()]


def parse_review_selection(content, needs, schema=None):
    """Expand only omitted question metadata; never manufacture a verdict/ref."""
    from agents.contracts import ReviewDecision
    data = json.loads(content)
    if isinstance(data, dict) and isinstance(data.get('need_assessments'), list):
        categories = {'symptom': 'observation', 'mechanism': 'mechanism',
                      'alternative': 'alternative', 'trigger': 'trigger',
                      'action_condition': 'action_condition'}
        for assessment in data['need_assessments']:
            if not isinstance(assessment, dict):
                continue
            blocker = assessment.get('blocker')
            need = needs.get(assessment.get('need_id'))
            if isinstance(blocker, dict) and 'cause_level' not in blocker and need is not None:
                blocker['cause_level'] = categories[need.purpose]
    # Explicit legacy categories still pass the original strict validators.
    return (schema or ReviewDecision).model_validate(data)


def review_contract_view(schema):
    """Remove duplicate prose and unreachable definitions, not validation rules."""
    if isinstance(schema, list):
        return [review_contract_view(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: review_contract_view(value) for key, value in schema.items()
              if key not in {'description', 'title', 'examples'}}
    if '$defs' in result:
        definitions = result['$defs']
        used = set()
        def collect(value):
            if isinstance(value, dict):
                reference = value.get('$ref', '')
                if reference.startswith('#/$defs/'):
                    used.add(reference.removeprefix('#/$defs/'))
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect({k: v for k, v in result.items() if k != '$defs'})
        pending = list(used)
        while pending:
            key = pending.pop()
            previous = set(used)
            collect(definitions[key])
            pending.extend(used - previous)
        result['$defs'] = {key: value for key, value in definitions.items() if key in used}
    return result


def corrections_within_policy(report):
    """Historical reports keep their cap; new counts must match repair events."""
    if report.get('repair_policy') == ROLE_REPAIR_POLICY:
        counts = report.get('repair_counts', {})
        events = [e for e in report['events'] if e['type'] == 'validation_repair']
        roles = {'supervisor', 'diagnosis', 'reviewer'}
        actual = {role: sum(e.get('role', e.get('node')) == role for e in events) for role in roles}
        return (set(counts) == roles and counts == actual
                and all(type(v) is int and 0 <= v <= 1 for v in counts.values())
                and all(e.get('repair_role') == e.get('role', e.get('node')) in roles for e in events)
                and report['repairs'] == sum(counts.values()))
    if report.get('repair_policy') not in {REPAIR_POLICY, REVIEW_STAGE_POLICY}:
        return report['repairs'] <= 1
    counts = report.get('repair_counts', {})
    events = [e for e in report['events'] if e['type'] == 'validation_repair']
    actual = {'collection': sum(e.get('role', e.get('node')) != 'reviewer' for e in events),
              'reviewer': sum(e.get('role', e.get('node')) == 'reviewer' for e in events)}
    if report.get('repair_policy') == REVIEW_STAGE_POLICY:
        stages = {name: sum(e.get('review_stage') == name for e in events if e.get('role', e.get('node')) == 'reviewer')
                  for name in ('ObservationReview', 'ReviewDecision')}
        return (counts == actual and type(counts.get('collection')) is int and 0 <= counts['collection'] <= 1
                and type(counts.get('reviewer')) is int and counts['reviewer'] == sum(stages.values())
                and all(v <= 1 for v in stages.values()) and report['repairs'] == sum(counts.values()))
    return (counts == actual and all(type(v) is int and 0 <= v <= 1 for v in counts.values())
            and report['repairs'] == sum(counts.values()))


@dataclass(frozen=True)
class RoleContractContext:
    """Borrowed read view for one request; schema builders do not mutate ledgers."""
    evidence: Mapping[str, Evidence]
    references: Mapping[str, EvidenceRef]
    executors: Mapping[str, Any]
    evidence_needs: Mapping[str, EvidenceNeed]
    targets: Mapping[str, Any]
    purpose: str
    phase: str
    planning_only: bool
    investigation_mode: str
    knowledge_mode: str
    review_protection: str


def role_output_schema(role, schema, view: RoleContractContext):
    """Bind the role contract to currently visible sources and capabilities."""
    from agents.contracts import SupervisorRequest, ReviewDecision, ObservationReview
    from evidence.diagnosis import diagnosis_reference_options
    from evidence.ownership import owner_schema
    from workflow.review_protection import conservative, BOUNDED_POLICY
    output_schema = owner_schema(SupervisorRequest if role == 'supervisor' else schema, view)
    reference_options = diagnosis_reference_options(view) if role == 'diagnosis' else []
    if role == 'diagnosis':
        reference_ids = [option['reference_id'] for option in reference_options]
        if reference_ids:
            output_schema['$defs']['ReferenceSelection']['properties']['reference_id']['enum'] = reference_ids
        output_schema['$defs']['SelectedFinding']['properties']['refs']['uniqueItems'] = True
        for field in ('support_refs', 'counter_refs'):
            output_schema['$defs']['HypothesisSelection']['properties'][field]['uniqueItems'] = True
    if role == 'diagnosis' and view.purpose == 'status_check':
        for field in ('hypotheses', 'recommended_actions'):
            output_schema['properties'][field]['maxItems'] = 0
    if role == 'diagnosis':
        output_schema = diagnosis_schema(output_schema, canonical_checks=view.planning_only)
    if role in {'supervisor', 'reviewer', 'diagnosis'} and 'ReadOnlyCheck' in output_schema.get('$defs', {}):
        tools = [s['function'] for ex in view.executors.values() for s in ex.schemas()]
        if tools:
            output_schema['$defs']['ReadOnlyCheck']['oneOf'] = [
                {'properties': {'tool': {'const': tool['name']}, 'args': tool['parameters']},
                 'required': ['tool', 'args']} for tool in tools]
    if role == 'supervisor':
        if view.planning_only:
            output_schema['properties'].pop('need_updates', None)
            output_schema['$defs'].pop('EvidenceNeedUpdate', None)
        else:
            output_schema['$defs']['EvidenceNeedUpdate']['properties']['need_id']['enum'] = list(view.evidence_needs)
        core_ids = [n.need_id for n in view.evidence_needs.values() if n.purpose in {'symptom', 'mechanism'}]
        if not view.planning_only:
            output_schema['$defs']['EvidenceNeedUpdate']['allOf'] = [{
                'if': {'properties': {'need_id': {'enum': core_ids}}, 'required': ['need_id']},
                'then': {'properties': {'status': {'enum': ['pending', 'supported', 'unavailable', 'deferred']}}}}]
        if view.phase == 'rework':
            output_schema['properties']['tasks']['maxItems'] = 1
        output_schema['oneOf'] = [
            {'properties': {'action': {'const': 'dispatch'}, 'tasks': {'minItems': 1}},
             'required': ['action', 'tasks']},
            {'properties': {'action': {'enum': ['diagnose', 'request_info', 'escalate', 'finish']},
                            'tasks': {'maxItems': 0}}, 'required': ['action']}]
        output_schema = supervisor_schema(output_schema, view.executors)
        for task_role, mode in (('investigation', view.investigation_mode), ('knowledge', view.knowledge_mode)):
            if mode == 'programmatic':
                output_schema['$defs']['TaskRequest'].setdefault('allOf', []).append({
                    'if': {'properties': {'role': {'const': task_role}}, 'required': ['role']},
                    'then': {'properties': {'checks': {'minItems': 1}}, 'required': ['checks']}})
    if role == 'reviewer' and issubclass(schema, ReviewDecision):
        required = list(view.targets)
        output_schema['$defs']['Assessment']['properties']['target_id']['enum'] = required
        output_schema['properties']['assessments'].update(minItems=len(required), maxItems=len(required))
        need_definition = 'RevisionNeedAssessment' if 'RevisionNeedAssessment' in output_schema['$defs'] else 'NeedAssessment'
        output_schema['$defs'][need_definition]['properties']['need_id']['enum'] = list(view.evidence_needs)
        output_schema['properties']['need_assessments'].update(
            minItems=len(view.evidence_needs), maxItems=len(view.evidence_needs))
        output_schema = reviewer_schema(output_schema, view.evidence_needs, view.targets,
            conservative=conservative(view.review_protection),
            bounded_refutation=view.review_protection == BOUNDED_POLICY)
    if schema is ObservationReview:
        output_schema = bind_reference_ids(output_schema, view)
    output_schema = compact_schema(bind_evidence_ids(output_schema, view.evidence))
    return output_schema


def role_model_input(role, schema, payload, view: RoleContractContext):
    """Remove redundant schema prose without removing evidence or changing prompts."""
    from agents.contracts import ReviewDecision
    from evidence.ownership import owner_options
    if role == 'supervisor' or role == 'reviewer' and issubclass(schema, ReviewDecision):
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
            for item in executor.schemas()] for r, executor in view.executors.items()}}
    model_input = payload if role == 'reviewer' and issubclass(schema, ReviewDecision) else {
        **payload, 'owner_options': owner_options(view)}
    return model_input


def parse_role_output(role, schema, content, view: RoleContractContext, *, emit):
    """Normalize compatibility fields, then parse against the original runtime contract."""
    from agents.contracts import ReviewDecision
    from evidence.contracts import EscalationSelection
    from evidence.ownership import parse_owner_selection
    if role == 'supervisor':
        data = json.loads(content)
        if (isinstance(data, dict) and data.get('action') in
                {'diagnose', 'request_info', 'escalate', 'finish'} and data.get('tasks')):
            emit('supervisor_fields_ignored', code='UNEXPECTED_TASKS',
                 action=data['action'], path='tasks',
                 ignored_count=len(data['tasks']) if isinstance(data['tasks'], list) else 1)
            content = json.dumps({**data, 'tasks': []})
    return (parse_owner_selection(schema, content, view) if issubclass(schema, EscalationSelection)
            else parse_review_selection(content, view.evidence_needs, schema) if issubclass(schema, ReviewDecision)
            else schema.model_validate_json(content))


def correction_instruction(role, schema, details, purpose):
    """Keep role-specific correction text together with its output contract."""
    from agents.contracts import ObservationReview
    instruction = '按提供的契约和已观测编号纠正一次。' + json.dumps(details, ensure_ascii=False)
    if role == 'reviewer':
        instruction += ('。refs只选实际观测字段REF，未确认原因保留在unverified_causes。'
            if schema is ObservationReview else
            '。必须完整覆盖 required_target_ids，每个编号恰好一次；证据不足时评估为 uncertain，不得省略条目')
        if 'RevisionNeedAssessment' in schema.model_json_schema().get('$defs', {}):
            if details['reason'] in {'REVIEW_BLOCKER_REQUIRED', 'REVIEW_BLOCKER_SCOPE', 'REVIEW_NEED_SCOPE', 'REVISION_NEED_CONFLICT'}:
                instruction += ('。先重新独立判断错误问题的purpose及decision_test，再选择互斥形态；'
                    '不要只给旧uncertain机械补blocker。机制问题评估当前如何发生，不要求证明触发来源；'
                    '仅触发未知保留限制，不能关联触发H来阻塞机制。已有证据待修稿使用revision且blocker=null；'
                    '确有缺观测仍用evidence并保留真实缺口。不得为通过自动改判supported。')
    elif role == 'diagnosis':
        if details['reason'] in {'UNKNOWN_REFERENCE', 'DUPLICATE_REFERENCE', 'reference_mismatch'}:
            instruction += ('。按 details.path 修正引用；REF_ 是字段引用编号，EV_ 是证据编号，不能混用。'
                '同一编号可分别用于不同观测或假设，但每个 refs/support_refs/counter_refs 列表内不得重复。'
                '可用引用保持在原input.evidence.reference_options与输出Schema中，不重复列出。'
                '选择与陈述含义相符的字段，不能用任意有效编号替换；证据不足则保留信息缺口。')
        if purpose == 'status_check':
            instruction += '。当前是 status_check；hypotheses 和 recommended_actions 必须为空列表，只输出可引用的观测和信息缺口'
    return instruction
