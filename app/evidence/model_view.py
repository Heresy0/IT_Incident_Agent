"""Compact model inputs; complete snapshots and reference registries stay server-owned."""
FIELDS = {'id', 'metric', 'value', 'unit', 'aggregation', 'granularity', 'requested_granularity', 'category', 'level',
          'document_id',
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
