"""Compare scoped query arguments; no model judgment, provider reads or gold."""
from datetime import datetime
import json
from pydantic import ValidationError
from evidence.contracts import TOOL_ARGS


def covers(tool, actual, required):
    if tool not in {'get_service_metrics', 'get_service_logs', 'get_recent_changes'}:
        return actual == required
    if (datetime.fromisoformat(actual['start']) > datetime.fromisoformat(required['start'])
            or datetime.fromisoformat(actual['end']) < datetime.fromisoformat(required['end'])):
        return False
    if tool == 'get_service_metrics':
        return (set(required['metrics']) <= set(actual['metrics'])
                and actual['granularity'] == required['granularity'])
    if tool == 'get_service_logs' and actual['category'] != required['category']:
        return False
    filters = ('level', 'error_code') if tool == 'get_service_logs' else ('category',)
    return all(actual.get(key) is None or actual.get(key) == required.get(key) for key in filters)


def complete_read(row):
    """Only a complete source response may cover a different query.

    A source error and a bounded/truncated sample describe an attempt, not the
    unobserved remainder of its window. Exact attempts remain visible so a
    caller can avoid automatically retrying the same request.
    """
    return row['status'] in {'ok', 'empty'} and not row.get('truncated', False)


def matching_reads(tool, args, results):
    """Return exact attempts or complete supersets, in ledger order."""
    if tool not in TOOL_ARGS:
        return []
    try:
        # Ledger arguments already use these defaults and UTC serialization.
        # A syntactically different spelling of one request is still one attempt.
        args = TOOL_ARGS[tool].model_validate_json(json.dumps(args)).model_dump(mode='json')
    except (ValidationError, ValueError, TypeError):
        return []
    return [row for row in results.values() if row['tool'] == tool
            and (row['args'] == args or complete_read(row) and covers(tool, row['args'], args))]


def read_state(row):
    """Describe acquisition quality without judging a diagnostic claim."""
    if row['status'] == 'error':
        return ('channel_unavailable' if row.get('error') in {
            'SOURCE_NOT_CONFIGURED', 'LOG_CATEGORY_NOT_REGISTERED', 'METRIC_NOT_REGISTERED',
            'RAG_INDEX_NOT_READY', 'RAG_UNAVAILABLE', 'RAG_DATABASE_NOT_CONFIGURED'} else 'failed')
    if row.get('truncated', False):
        return 'truncated'
    return 'complete_empty' if row['status'] == 'empty' else 'complete'


def covering_empty(tool, args, results):
    """A complete empty superset cannot gain rows by narrowing its filters.

    Restricted, truncated, failed, or differently scoped reads do not qualify.
    Applies only within one executor's authorized run and immutable read ledger.
    """
    return next((row for row in results.values() if row['tool'] == tool
        and row['status'] == 'empty' and complete_read(row)
        and covers(tool, row['args'], args)), None)
