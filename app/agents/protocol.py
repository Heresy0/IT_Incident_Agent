"""Model-facing schemas and auditable correction accounting."""
from copy import deepcopy
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


def reviewer_schema(schema, needs, targets):
    schema = deepcopy(schema)
    core = [n.need_id for n in needs.values() if n.purpose in {'symptom', 'mechanism'}]
    schema['$defs']['NeedAssessment']['allOf'] = [{
        'if': {'properties': {'need_id': {'enum': core}}, 'required': ['need_id']},
        'then': {'properties': {'verdict': {'enum': ['supported', 'uncertain']},
                                'blocking': {'enum': [None]}}}}]
    # Expose the existing blocker ownership guard before generation. This only
    # restricts contradictory edges; it does not decide any semantic verdict.
    for purpose in ('mechanism', 'alternative'):
        need_ids = [n.need_id for n in needs.values() if n.purpose == purpose]
        eligible = [key for key, target in targets.items()
                    if not (key.startswith('H') and target.level == 'trigger')]
        if need_ids:
            schema['$defs']['NeedAssessment']['allOf'].append({
                'if': {'properties': {'need_id': {'enum': need_ids}}, 'required': ['need_id']},
                'then': {'properties': {'blocker': {'anyOf': [{'type': 'null'}, {
                    'type': 'object', 'properties': {'affected_targets': {
                        'contains': {'type': 'string', 'enum': eligible}, 'minContains': 1}}}]}}}})
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


def parse_review_selection(content, needs):
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
    return ReviewDecision.model_validate(data)


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
