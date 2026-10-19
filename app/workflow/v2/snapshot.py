"""Explicit JSON snapshots; never serialize clients, locks or credentials."""
from copy import deepcopy
from datetime import datetime
import json
from pydantic import BaseModel

from evidence.contracts import Evidence
from workflow.v2.contracts import Draft
from workflow.v2.review import ReviewBatch


def json_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    return value


def capture(state, run):
    executor, context = run.executor, run.context
    return {'state': json_value(state), 'context': {
        'counts': dict(context.counts), 'tokens': dict(context.tokens), 'errors': deepcopy(context.errors),
        'blocked_providers': sorted(context.blocked_providers), 'stop_reason': context.stop_reason,
        'root_span': context.root_span},
        'executor': {'evidence': [item.model_dump(mode='json') for item in executor.evidence.values()],
            'seen': sorted([list(key) for key in executor.seen]),
            'check_results': [[list(key), row] for key, row in executor.check_results.items()],
            'last_query_profile': deepcopy(executor.last_query_profile)},
        'questions': json_value(run.questions.__dict__) if run.questions else None,
        'events': deepcopy(run.events)}


def restore(payload, run):
    state = deepcopy(payload['state'])
    if 'draft' in state:
        state['draft'] = Draft.model_validate_json(json.dumps(state['draft']))
    if 'reviews' in state:
        state['reviews'] = ReviewBatch.model_validate_json(json.dumps(state['reviews']))
    context, executor = run.context, run.executor
    data = payload['context']
    context.counts, context.tokens = dict(data['counts']), dict(data['tokens'])
    context.errors = deepcopy(data['errors'])
    context.blocked_providers = set(data['blocked_providers'])
    context.stop_reason, context.root_span = data['stop_reason'], data['root_span']
    executor.evidence.clear()
    executor.references.clear()
    for row in payload['executor']['evidence']:
        item = Evidence.model_validate_json(json.dumps(row))
        prepared = executor.prepare_snapshot(item.locator.split('/', 1)[0], item.payload)
        if (prepared.hash != item.hash or prepared.evidence_id != item.evidence_id
                or prepared.reference_options != item.reference_options):
            from storage.recovery import RecoveryConflict
            raise RecoveryConflict('Restored evidence content/identity/reference mismatch')
        executor.register_snapshot(item)
    executor.seen = {tuple(key) for key in payload['executor']['seen']}
    executor.check_results = {tuple(key): deepcopy(row) for key, row in payload['executor']['check_results']}
    executor.last_query_profile = deepcopy(payload['executor']['last_query_profile'])
    if run.questions is not None and payload['questions'] is not None:
        run.questions.__dict__.clear()
        run.questions.__dict__.update(deepcopy(payload['questions']))
        for question in run.questions.questions:
            for key in ('_baseline_observations', '_baseline_queries'):
                question[key] = set(question[key])
            question['_baseline_sources'] = {tuple(item) for item in question['_baseline_sources']}
    run.events[:] = deepcopy(payload['events'])
    return state
