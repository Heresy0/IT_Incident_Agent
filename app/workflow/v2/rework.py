"""One semantic revision round, distinct from protocol correction and approval.

Only current, explicit review objections select targets. The parser can replace
or withdraw those server-owned slots; it never invents IDs, discards siblings,
or turns a revised statement into an approved fact. The caller must review the
entire resulting draft against the current evidence pack before publishing.
"""
import json
import time
from typing import Literal

from pydantic import Field, ValidationError

from agents.protocol import compact_schema
from evidence.contracts import Contract
from workflow.v2.contracts import (ClaimInput, Draft, NextStep, NextStepInput,
                                   parse_draft, source_facts, _error, _validation_errors)
from workflow.v2.review import evidence_pack_version, review_matches

REWORK_POLICY_VERSION = 'one_semantic_revision_v1'


class ClaimRevisionInput(Contract):
    target_id: str = Field(pattern=r'^C[1-3]$')
    revision: ClaimInput
    change_kind: Literal['narrow', 'replace'] = 'replace'
    reason: str = Field(min_length=1, max_length=400)


class CheckRevisionInput(Contract):
    target_id: str = Field(pattern=r'^A[1-2]$')
    revision: NextStepInput
    change_kind: Literal['narrow', 'replace'] = 'replace'
    reason: str = Field(min_length=1, max_length=400)


class WithdrawalInput(Contract):
    target_id: str = Field(pattern=r'^[CA][1-3]$')
    reason: str = Field(min_length=1, max_length=400)


class RevisionInput(Contract):
    items: list[ClaimRevisionInput | CheckRevisionInput] = Field(default_factory=list, max_length=5)
    withdraw: list[WithdrawalInput] = Field(default_factory=list, max_length=5)
    gaps: list[str] = Field(default_factory=list, max_length=8)


class RevisionResult(Contract):
    draft: Draft
    errors: list[dict] = Field(default_factory=list)
    audit: list[dict] = Field(default_factory=list)
    changed_ids: list[str] = Field(default_factory=list)
    withdrawn_ids: list[str] = Field(default_factory=list)
    policy_version: str = REWORK_POLICY_VERSION
    semantic_approval_inherited: Literal[False] = False


def _targets(draft):
    return {target.id: target for target in [*draft.claims, *draft.next_steps]}


def _allowed(previous, target_ids):
    targets = _targets(previous)
    if not isinstance(target_ids, (list, tuple)) or len(set(target_ids)) != len(target_ids):
        raise ValueError('invalid server revision targets')
    if any(not isinstance(key, str) or key not in targets for key in target_ids):
        raise ValueError('unknown server revision target')
    return {key: targets[key] for key in target_ids}


def rework_feedback(draft, reviews, executor, *, collection=None):
    """Extract genuine current objections, never manufacture objections to passes."""
    current_pack = evidence_pack_version(executor, collection=collection)
    payload = {'policy_version': REWORK_POLICY_VERSION, 'targets': [], 'allowed_target_ids': [],
               'evidence_pack_version': current_pack, 'reason': 'NO_CURRENT_OBJECTIONS',
               'instruction': 'Only revise or withdraw the listed targets. A revision is not evidence or approval; '
                              'retain unresolved scope and do not rewrite merely to obtain a pass.'}
    if reviews.evidence_pack_version != current_pack:
        payload['reason'] = 'EVIDENCE_CHANGED_REVIEW_REQUIRED'
        return payload
    targets = _targets(draft)
    counts = {}
    for item in reviews.items:
        counts[item.target_id] = counts.get(item.target_id, 0) + 1
    for item in reviews.items:
        target = targets.get(item.target_id)
        if (target is None or counts[item.target_id] != 1 or item.protocol_status != 'valid'
                or item.relation not in {'partial', 'insufficient'}
                or not review_matches(item, target, current_pack)):
            continue
        payload['targets'].append({'target_id': target.id, 'target_version': target.version,
            'review_kind': item.judgment_kind, 'relation': item.relation,
            'decision': item.submitted_decision, 'issue': item.issue,
            'review_refs': [ref.model_dump(mode='json') for ref in item.refs],
            'target': target.model_dump(mode='json')})
        payload['allowed_target_ids'].append(target.id)
    if payload['targets']:
        payload['reason'] = 'CURRENT_REVIEW_OBJECTIONS'
    return payload


def claim_rework(context, state, *, needs_query=False):
    """Claim one round without granting model/tool calls beyond the run total.

    Two calls cover revision and full review; three cover one investigation
    decision/read followed by revision and review. The engine must quote the
    remaining stages again before every optional additional read.
    """
    with context.lock:
        if state.get('semantic_reworks', 0) >= 1:
            return False
        needed = 3 if needs_query else 2
        if (context.limits.model_calls - context.counts['model_calls'] < needed
                or context.limits.seconds - (time.monotonic() - context.started)
                    <= max(1, context.limits.reserve_seconds)
                or needs_query and context.counts['tool_calls'] >= context.limits.tool_calls):
            return False
        state['semantic_reworks'] = 1
    context.emit({'type': 'semantic_rework_claimed', 'role': 'workflow',
                  'round': 1, 'minimum_model_calls': needed, 'policy_version': REWORK_POLICY_VERSION})
    return True


def revision_schema(target_ids):
    """Only server-selected stable slots are advertised as writable."""
    schema = compact_schema(RevisionInput.model_json_schema())
    target_ids = list(target_ids)
    for name, prefix in [('ClaimRevisionInput', 'C'), ('CheckRevisionInput', 'A')]:
        allowed = [key for key in target_ids if key.startswith(prefix)]
        if allowed:
            schema['$defs'][name]['properties']['target_id']['enum'] = allowed
    choices = []
    if any(key.startswith('C') for key in target_ids):
        choices.append({'$ref': '#/$defs/ClaimRevisionInput'})
    if any(key.startswith('A') for key in target_ids):
        choices.append({'$ref': '#/$defs/CheckRevisionInput'})
    if choices:
        schema['properties']['items']['items'] = {'anyOf': choices}
        schema['$defs']['WithdrawalInput']['properties']['target_id']['enum'] = target_ids
    else:
        schema['properties']['items']['maxItems'] = 0
        schema['properties']['withdraw']['maxItems'] = 0
    schema['properties']['items']['maxItems'] = len(target_ids)
    schema['properties']['withdraw']['maxItems'] = len(target_ids)
    return schema


def _base(previous, executor):
    return previous.model_copy(update={'facts': source_facts(executor)})


def parse_revision(raw, previous, executor, target_ids):
    """Replace/withdraw authorized slots independently; return audit, never a pass.

    Omitted entries retain their original statement and unresolved review.
    A model's 'narrow' label is recorded as its proposal, not certified here.
    Gaps are append-only so removing a hypothesis cannot erase its uncertainty.
    """
    allowed = _allowed(previous, target_ids)
    preserved = _base(previous, executor)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return RevisionResult(draft=preserved, errors=[_error('INVALID_JSON', '$', category='syntax')])
    if not isinstance(data, dict):
        return RevisionResult(draft=preserved, errors=[_error('OBJECT_REQUIRED', '$')])
    errors, audit, candidates = [], [], {}
    if set(data) - {'items', 'withdraw', 'gaps'}:
        errors.append(_error('EXTRA_FIELDS', '$.<extra>'))
    for field in ('items', 'withdraw'):
        values = data.get(field, [])
        if not isinstance(values, list):
            errors.append(_error('LIST_REQUIRED', field))
            continue
        if len(values) > 5:
            errors.append(_error('ITEM_LIMIT', field))
        for index, candidate in enumerate(values[:5]):
            target_id = candidate.get('target_id') if isinstance(candidate, dict) else None
            if not isinstance(target_id, str) or target_id not in allowed:
                errors.append(_error('REVISION_TARGET_DENIED', f'{field}.{index}.target_id', category='policy'))
                continue
            candidates.setdefault(target_id, []).append((field, index, candidate))
    changed, withdrawn, replacements = [], [], {}
    gap_additions = []
    for key, proposals in candidates.items():
        original = allowed[key]
        if len(proposals) != 1:
            errors.append(_error('DUPLICATE_REVISION_TARGET', 'items/withdraw', target_id=key))
            continue
        field, index, candidate = proposals[0]
        path = f'{field}.{index}'
        try:
            model = WithdrawalInput if field == 'withdraw' else (
                CheckRevisionInput if isinstance(original, NextStep) else ClaimRevisionInput)
            selected = model.model_validate(candidate)
        except ValidationError as exc:
            errors.extend(_validation_errors(exc, path, key))
            continue
        if field == 'withdraw':
            withdrawn.append(key)
            gap_additions.append((f'已撤回{key}原条目：' + selected.reason
                                  + '；撤回不等于反证成立、原因确认或问题已解决。')[:500])
            audit.append({'target_id': key, 'operation': 'withdrawn', 'reason': selected.reason,
                          'before_version': original.version, 'after_version': None,
                          'before': original.model_dump(mode='json'), 'after': None,
                          'semantic_approval_inherited': False})
            continue
        draft_field = 'next_steps' if isinstance(original, NextStep) else 'claims'
        slots = {'claims': [], 'next_steps': [], draft_field: [key]}
        revised, failures = parse_draft({draft_field: [selected.revision.model_dump(mode='json')]},
                                       executor, target_ids=slots)
        if failures:
            errors.extend({**failure, 'path': path + '.revision.' + failure['path']} for failure in failures)
            continue
        replacement = getattr(revised, draft_field)[0]
        replacements[key] = replacement
        differs = replacement.version != original.version
        if differs:
            changed.append(key)
        audit.append({'target_id': key, 'operation': 'revised' if differs else 'unchanged',
                      'claimed_change_kind': selected.change_kind, 'reason': selected.reason,
                      'before_version': original.version, 'after_version': replacement.version,
                      'before': original.model_dump(mode='json'), 'after': replacement.model_dump(mode='json'),
                      'narrowing_verified_by_program': False, 'semantic_approval_inherited': False})
    gaps = data.get('gaps', [])
    if not isinstance(gaps, list):
        errors.append(_error('LIST_REQUIRED', 'gaps'))
        gaps = []
    if len(gaps) > 8:
        errors.append(_error('ITEM_LIMIT', 'gaps'))
    for index, gap in enumerate(gaps[:8]):
        if not isinstance(gap, str) or not 0 < len(gap) <= 500:
            errors.append(_error('INVALID_GAP', f'gaps.{index}'))
        else:
            gap_additions.append(gap)
    updates = {field: [replacements.get(item.id, item) for item in getattr(previous, field)
                      if item.id not in withdrawn] for field in ('claims', 'next_steps')}
    updates['gaps'] = list(dict.fromkeys([*previous.gaps, *gap_additions]))[:8]
    return RevisionResult(draft=preserved.model_copy(update=updates), errors=errors, audit=audit,
                          changed_ids=changed, withdrawn_ids=withdrawn)
