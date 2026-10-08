"""Source-derived facts and observable collection gaps; no answers, model calls or gold."""
import json

FACT_RENDERING = 'source_fields_v1'


def source_statement(refs, evidence):
    parts = []
    for ref in refs:
        item = evidence[ref.evidence_id]
        payload, path = item.payload, ref.field_path
        value = json.dumps(ref.value, ensure_ascii=False)
        if path == 'value' and 'metric' in payload:
            label = f"{payload['metric']} ({payload.get('aggregation', 'observed')})"
        elif path in {'message', 'error_code'}:
            label = f"{payload.get('category', 'observed')} log [{payload.get('level', '')}] {path}"
        elif path == 'summary' or path.startswith('config_summary.'):
            label = f"{payload.get('category', 'observed')} change {path}"
        elif path in {'team', 'escalation'}:
            label = f"owner {path}"
        elif path in {'text', 'resolution'}:
            label = f"{item.kind} source {path}"
        else:
            # Metadata cannot add an interpretation to substantive source fields.
            continue
        parts.append(f"{label}={value} {ref.unit} @ {ref.observed_at.isoformat()}".strip())
    text = '; '.join(parts)
    return text if len(text) <= 500 else text[:475] + ' ... (see references)'


def pool_control_gaps(evidence):
    current = [e.payload for e in evidence.values() if e.kind == 'observation']
    pressure = any(p.get('metric') in {'pool_usage', 'pool_wait'} or p.get('error_code') == 'POOL_ACQUIRE_TIMEOUT' for p in current)
    return ['db_cpu'] if pressure and not any(p.get('metric') == 'db_cpu' for p in current) else []


def gap_message(gaps):
    return 'Current connection-pool attribution lacks observed controls: ' + ', '.join(gaps) + '; DB ping is not a CPU measurement.'
