"""Compact model inputs; complete snapshots and reference registries stay server-owned."""
import json
from langchain_core.messages import HumanMessage

MODEL_VIEW_VERSION = 'compact_sources_v1'
FIELDS = {'id', 'metric', 'value', 'unit', 'aggregation', 'granularity', 'category', 'level',
          'error_code', 'message', 'summary', 'config_summary', 'team', 'escalation', 'text',
          'resolution', 'versions', 'updated_at', 'confirmed_at', 'validity'}


def substantive(path):
    return path in {'value', 'message', 'error_code', 'summary', 'text', 'resolution', 'team', 'escalation'} or path.startswith('config_summary.')


def evidence_view(item):
    return {'evidence_id': item.evidence_id, 'kind': item.kind, 'service': item.service,
            'environment': item.environment, 'observed_at': item.observed_from.isoformat(),
            'data_version': item.data_version,
            'payload': {k: v for k, v in item.payload.items() if k in FIELDS},
            'reference_options': [o.model_dump() for o in item.reference_options if substantive(o.field_path)]}


def result_view(result):
    return json.dumps({'status': result.status, 'evidence': [evidence_view(e) for e in result.evidence],
        'truncated': result.truncated, 'sample_order': result.sample_order,
        'error': result.error.model_dump() if result.error else None}, ensure_ascii=False, separators=(',', ':'))


def fit_messages(messages, executor, collection, input_chars):
    """Fold complete old turns into registered observations before exceeding the input cap."""
    before = sum(len(str(m.content)) for m in messages)
    ceiling = input_chars - min(4000, input_chars // 4)
    if before <= ceiling:
        return messages, None
    base = messages[:2]  # Never drop the system instructions, ticket, schema or scope.
    hint = next((m.content for m in reversed(messages[2:]) if isinstance(m, HumanMessage)), '')
    if len(hint) > 2000:
        hint = 'Repair using the output schema and the registered reference IDs below; never invent source fields.'
    state = {'conversation_compacted': True, 'evidence': [], 'collection': collection[-12:],
             'omitted_evidence_count': 0,
             'instruction': 'Reuse these observed sources. Omitted rows are not evidence of absence. ' + hint}
    room = ceiling - sum(len(str(m.content)) for m in base)
    for item in reversed(list(executor.evidence.values())):
        row = evidence_view(item)
        state['evidence'].append(row)
        if len(json.dumps(state, ensure_ascii=False, separators=(',', ':'))) > room:
            state['evidence'].pop()
            state['omitted_evidence_count'] += 1
    rebuilt = [*base, HumanMessage(content=json.dumps(state, ensure_ascii=False, separators=(',', ':')))]
    after = sum(len(str(m.content)) for m in rebuilt)
    # If even the mandatory context cannot fit, retain the existing hard budget failure.
    if after > input_chars:
        return messages, None
    return rebuilt, {'before_chars': before, 'after_chars': after,
                     'omitted_evidence_count': state['omitted_evidence_count']}
