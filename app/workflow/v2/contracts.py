"""Small model contracts and independently salvageable diagnosis items.

Aliases select registered source fields. They never let a model supply source
values, ownership, approval or a workflow state. The public facts are rendered
from the immutable observations even when every model-authored item is invalid.
"""
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, ValidationError

from runtime.schema import compact_schema
from evidence.contracts import Contract, EvidenceRef, Finding, ReferenceSelection
from evidence.references import ProtocolError, diagnosis_reference_options, expand_refs
from evidence.model_view import evidence_view
from evidence.observations import source_statement

CONTRACT_VERSION = 'atomic_draft_v1'
REFERENCE_DESCRIPTION = ('Select exact R1/R2 aliases from reference_options, or an exact registered REF_ id. '
                         'Never supply values or invent identifiers.')


class RefSelection(Contract):
    reference_id: str = Field(min_length=1, max_length=64, description=REFERENCE_DESCRIPTION)


ReferenceInput = str | RefSelection


class ClaimInput(Contract):
    statement: str = Field(min_length=1, max_length=500)
    refs: list[ReferenceInput] = Field(default_factory=list, max_length=8, description=REFERENCE_DESCRIPTION)
    counter_refs: list[ReferenceInput] = Field(default_factory=list, max_length=8)
    level: Literal['mechanism', 'trigger', 'alternative'] = 'mechanism'


class NextStepInput(Contract):
    action: str = Field(min_length=1, max_length=500)
    condition: str = Field(default='', max_length=300)
    refs: list[ReferenceInput] = Field(default_factory=list, max_length=8, description=REFERENCE_DESCRIPTION)
    kind: Literal['manual_check'] = 'manual_check'


class DraftInput(Contract):
    claims: list[ClaimInput] = Field(default_factory=list, max_length=3)
    next_steps: list[NextStepInput] = Field(default_factory=list, max_length=2)
    gaps: list[str] = Field(default_factory=list, max_length=8)


class Claim(Contract):
    id: str
    version: str
    statement: str
    refs: list[EvidenceRef]
    counter_refs: list[EvidenceRef]
    level: Literal['mechanism', 'trigger', 'alternative']


class NextStep(Contract):
    id: str
    version: str
    action: str
    condition: str
    refs: list[EvidenceRef]
    kind: Literal['manual_check'] = 'manual_check'


class Draft(Contract):
    facts: list[Finding] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list, max_length=3)
    next_steps: list[NextStep] = Field(default_factory=list, max_length=2)
    gaps: list[str] = Field(default_factory=list, max_length=8)


def draft_schema(*, target_ids=None):
    schema = compact_schema(DraftInput.model_json_schema())
    if target_ids is not None:
        for field in ('claims', 'next_steps'):
            slots = target_ids.get(field, [])
            schema['properties'][field]['maxItems'] = len(slots)
            schema['properties'][field]['description'] = (
                'Only these failed server slots, in this exact array order: ' + ', '.join(slots))
    return schema


def reference_aliases(executor):
    """Deterministic aliases for the current immutable server registry."""
    return {f'R{index}': executor.references[option['reference_id']].model_copy(deep=True)
            for index, option in enumerate(diagnosis_reference_options(executor), 1)}


def reference_options(executor):
    return [{'reference_id': f'R{index}', 'evidence_id': option['evidence_id'],
             'field_path': option['field_path']}
            for index, option in enumerate(diagnosis_reference_options(executor), 1)]


def draft_input(executor, *, collection=None):
    """All compact sources, including uncited counterevidence and query quality.

    Source capacity is bounded by the executor. Consumers must preflight the
    complete pack against their input budget; silently removing sources could
    remove a contradiction and cannot produce a valid semantic approval.
    """
    sources = []
    for evidence in executor.evidence.values():
        view = evidence_view(evidence)
        view.pop('reference_options', None)
        sources.append(view)
    queries = list(executor.check_results.values()) if collection is None else list(collection)
    return {'evidence': sources, 'reference_options': reference_options(executor),
            'collection': queries, 'evidence_pack_complete': True,
            'source_count': len(sources), 'omitted_evidence_count': 0,
            'instruction': 'Use only source-field references. Empty, failed or truncated reads do not establish health.'}


def _error(code, path, *, target_id=None, category='shape'):
    result = {'category': category, 'code': code, 'path': path}
    if target_id is not None:
        result['target_id'] = target_id
    return result


def _validation_errors(exc, path, target_id):
    # Never echo rejected values, credentials, arbitrary field names or model text.
    known = {'statement', 'refs', 'counter_refs', 'level', 'action', 'condition', 'kind',
             'reference_id', 'target_id', 'relation', 'issue'}
    return [_error(item['type'], '.'.join([path, *[str(part) if isinstance(part, int) or part in known
                      else '<extra>' for part in item['loc']]]), target_id=target_id)
            for item in exc.errors(include_input=False, include_context=False, include_url=False)[:5]]


def resolve_refs(refs, executor, *, alias_map=None, path='refs'):
    """Resolve exact selectors through shared immutable-source validation."""
    aliases = reference_aliases(executor) if alias_map is None else alias_map
    registered = {ref.model_dump_json(): rid for rid, ref in executor.references.items()}
    selections = []
    for index, ref in enumerate(refs):
        selected = ref if isinstance(ref, str) else ref.reference_id
        if selected in aliases:
            selected = registered.get(aliases[selected].model_dump_json())
        if selected not in executor.references:
            raise ProtocolError('UNKNOWN_REFERENCE', [{'path': f'{path}.{index}'}])
        selections.append(ReferenceSelection(reference_id=selected))
    return expand_refs(selections, executor, path)


def _version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()[:16]


def source_facts(executor):
    """Facts contain source values only, never a model's interpretation."""
    grouped = {}
    for option in diagnosis_reference_options(executor):
        evidence = executor.evidence[option['evidence_id']]
        if evidence.kind != 'observation':
            continue
        grouped.setdefault(evidence.evidence_id, []).append(ReferenceSelection(reference_id=option['reference_id']))
    facts = []
    for selections in grouped.values():
        for offset in range(0, len(selections), 8):
            refs = expand_refs(selections[offset:offset + 8], executor)
            statement = source_statement(refs, executor.evidence)
            if statement:
                facts.append(Finding(statement=statement, refs=refs))
    return facts


def parse_draft(raw, executor, *, alias_map=None, target_ids=None):
    """Return valid siblings and errors instead of rejecting an entire draft."""
    facts = source_facts(executor)
    errors = []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return Draft(facts=facts), [_error('INVALID_JSON', '$', category='syntax')]
    if not isinstance(data, dict):
        return Draft(facts=facts), [_error('OBJECT_REQUIRED', '$')]
    if set(data) - {'claims', 'next_steps', 'gaps'}:
        errors.append(_error('EXTRA_FIELDS', '$.<extra>'))
    accepted = {'claims': [], 'next_steps': []}
    for field, model, limit, prefix in [('claims', ClaimInput, 3, 'C'),
                                       ('next_steps', NextStepInput, 2, 'A')]:
        slots = [f'{prefix}{index + 1}' for index in range(limit)]
        if target_ids is not None:
            selected_slots = target_ids.get(field, [])
            if (not isinstance(selected_slots, list) or len(selected_slots) != len(set(selected_slots))
                    or any(key not in slots for key in selected_slots)):
                raise ValueError('invalid server correction slots')
            slots = selected_slots
            limit = len(slots)
        items = data.get(field, [])
        if not isinstance(items, list):
            errors.append(_error('LIST_REQUIRED', field))
            continue
        if len(items) > limit:
            errors.append(_error('ITEM_LIMIT', field))
        for index, item in enumerate(items[:limit]):
            target_id, path = slots[index], f'{field}.{index}'
            try:
                selected = model.model_validate(item)
                refs = resolve_refs(selected.refs, executor, alias_map=alias_map, path=path + '.refs')
                attributes = {'id': target_id, 'refs': refs}
                if field == 'claims':
                    attributes.update(statement=selected.statement, level=selected.level,
                        counter_refs=resolve_refs(selected.counter_refs, executor, alias_map=alias_map,
                                                  path=path + '.counter_refs'))
                    attributes['version'] = _version({**selected.model_dump(mode='json'),
                        'refs': [ref.model_dump(mode='json') for ref in refs],
                        'counter_refs': [ref.model_dump(mode='json') for ref in attributes['counter_refs']]})
                    accepted[field].append(Claim(**attributes))
                else:
                    attributes['version'] = _version({**selected.model_dump(mode='json'),
                                                      'refs': [ref.model_dump(mode='json') for ref in refs]})
                    accepted[field].append(NextStep(**attributes, action=selected.action,
                                                  condition=selected.condition, kind=selected.kind))
            except ValidationError as exc:
                errors.extend(_validation_errors(exc, path, target_id))
            except (ProtocolError, ValueError, TypeError) as exc:
                errors.append(_error(getattr(exc, 'code', getattr(exc, 'reason', 'INVALID_REFERENCE')),
                                     path, target_id=target_id, category='reference'))
    gaps = []
    items = data.get('gaps', [])
    if not isinstance(items, list):
        errors.append(_error('LIST_REQUIRED', 'gaps'))
    else:
        if len(items) > 8:
            errors.append(_error('ITEM_LIMIT', 'gaps'))
        for index, gap in enumerate(items[:8]):
            if isinstance(gap, str) and 0 < len(gap) <= 500:
                gaps.append(gap)
            else:
                errors.append(_error('INVALID_GAP', f'gaps.{index}'))
    return Draft(facts=facts, gaps=gaps, **accepted), errors


def draft_correction_targets(previous, errors):
    """Server-owned slots for one failed-item retry; valid slots are immutable."""
    slots = {}
    for field, prefix, limit in [('claims', 'C', 3), ('next_steps', 'A', 2)]:
        occupied = {item.id for item in getattr(previous, field)}
        failed = {error.get('target_id') for error in errors
                  if isinstance(error.get('target_id'), str) and error['target_id'].startswith(prefix)}
        whole_field = any(error.get('category') == 'syntax'
                          or error.get('code') in {'OBJECT_REQUIRED', 'LIST_REQUIRED'}
                          and (error.get('path') == '$' or error.get('path') == field)
                          for error in errors)
        slots[field] = [f'{prefix}{index + 1}' for index in range(limit)
                        if f'{prefix}{index + 1}' not in occupied
                        and (whole_field or f'{prefix}{index + 1}' in failed)]
    return slots


def merge_drafts(previous, corrected):
    """Add corrected missing slots without replacing any valid original item."""
    merged = {}
    for field in ('claims', 'next_steps'):
        by_id = {item.id: item for item in getattr(corrected, field)}
        by_id.update({item.id: item for item in getattr(previous, field)})
        merged[field] = [by_id[key] for key in sorted(by_id, key=lambda key: int(key[1:]))]
    facts = {}
    for fact in [*previous.facts, *corrected.facts]:
        key = tuple(ref.model_dump_json() for ref in fact.refs)
        facts.setdefault(key, fact)
    return Draft(facts=list(facts.values()), gaps=list(dict.fromkeys([*previous.gaps, *corrected.gaps]))[:8], **merged)
