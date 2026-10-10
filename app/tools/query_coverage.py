"""Compare scoped query arguments; no model judgment, provider reads or gold."""
from datetime import datetime


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


def covering_empty(tool, args, results):
    """A complete empty superset cannot gain rows by narrowing its filters.

    Restricted, truncated, failed, or differently scoped reads do not qualify.
    Applies only within one executor's authorized run and immutable read ledger.
    """
    return next((row for row in results.values() if row['tool'] == tool
        and row['status'] == 'empty' and not row['truncated']
        and covers(tool, row['args'], args)), None)
