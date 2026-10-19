"""Resume owner-bound incident runs in the existing bounded worker pool."""
from runtime.context import RunContext, activate
from runtime.journal import Journal
from storage.recovery import RecoveryStore, RecoveryConflict, RecoveryInterrupted, SCHEMA_VERSION, digest


def enabled(request):
    return request.get('run_spec', {}).get('recovery', {}).get('mode') == 'postgres_v1'


def execution_request(row, service):
    from copy import deepcopy
    config = row['config']
    return {'task_type': 'incident', 'execution': config['execution'],
        'incident_snapshot': config['incident_snapshot'], 'query': row['query'],
        'thread_id': row['thread_id'], 'tenant_id': row['tenant_id'], 'user_id': row['user_id'],
        'run_spec': config['run_spec'], '_model_config': deepcopy(service._base_config),
        '_provider_registry': deepcopy(service.services)}


def execute(service, run_id, request):
    adapter, store = service._incident, service._store
    with RecoveryStore.own(store, run_id) as backend:
        if backend is None:
            return None
        row = backend.read()
        events, after = [], 0
        while True:
            batch = store.events(run_id, after)
            events.extend(batch)
            if len(batch) < 100:
                break
            after = batch[-1]['seq']
        context = RunContext(run_id, limits=adapter.limits(request), scope=adapter.scope(request))
        journal = Journal(backend, context)
        error = None
        if row['schema_version'] != SCHEMA_VERSION or row['spec_sha256'] != digest(request['run_spec']):
            error = 'RECOVERY_SPEC_MISMATCH'
        elif row['recoveries'] > 2:
            error = 'RECOVERY_ATTEMPTS_EXHAUSTED'
        else:
            error = adapter.resume_error(request)
        if error:
            journal.authoritative_counts()
            result = adapter.failure(request, context, error)
        elif row['candidate'] is not None:
            result = backend.prepare(row['candidate'])
        else:
            try:
                with activate(context):
                    result = adapter.execute(request, context, events)
            except RecoveryConflict:
                journal.stage_name = None
                journal.authoritative_counts()
                result = adapter.failure(request, context, 'RECOVERY_STATE_CONFLICT')
            except RecoveryInterrupted:
                raise
            except Exception:
                import psycopg
                import sys
                if isinstance(sys.exception(), psycopg.Error):
                    raise
                import logging
                logging.getLogger('incident.workflow').exception('recoverable_workflow_failed | run_id=%s', run_id)
                journal.stage_name = None
                journal.authoritative_counts()
                result = adapter.failure(request, context, 'INTERNAL_ERROR')
        result = {**result, 'recovery': {'mode': 'postgres_v1', 'recoveries': row['recoveries'],
            'unknown_response_policy': 'stop_without_resend', 'deadline_policy': 'wall_clock'}}
        candidate = backend.prepare(result)
        journal.fault('after_report_ready')
        store.finish(run_id, candidate, epoch=backend.epoch)
        journal.fault('after_report_committed')
        return store.get(run_id)['result']
