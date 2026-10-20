"""Render source-derived facts without adding interpretations or model calls."""
import json

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
        if payload.get('source_type') == 'user_submission':
            label = f"用户提供（未经独立核验，来源：{payload.get('source_name', '')}）{label}"
        parts.append(f"{label}={value} {ref.unit} @ {ref.observed_at.isoformat()}".strip())
    text = '; '.join(parts)
    return text if len(text) <= 500 else text[:475] + ' ... (see references)'
