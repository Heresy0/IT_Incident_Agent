"""Child-process fault injector. Used only by isolated recovery acceptance."""
import json
import logging
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.getenv('CHECKPOINT_TEST_SOURCE_ROOT', str(ROOT)))
sys.path[:0] = [str(SOURCE / 'app'), str(ROOT)]
logging.disable(logging.CRITICAL)
connect = socket.socket.connect
def local_only(self, address):
    if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
        raise AssertionError('Paid/external network forbidden in checkpoint process tests')
    return connect(self, address)
socket.socket.connect = local_only

from storage.runs import PostgresRunStore
from workflow.recovery import execute, execution_request
from providers.bound import BoundIncidentProvider
from tests.checkpoint_support import audit, service_for, RecoveryModel
from storage.recovery import RecoveryStore, RecoveryInterrupted, storage_boundary
from runtime.journal import Journal
from runtime.context import ExecutionError
from providers.fixtures import ProviderError

scenario = next((arg.split('=', 1)[1] for arg in sys.argv[6:] if arg.startswith('scenario=')), '')
if 'fixed-clock' in sys.argv[6:]:
    Journal.real_elapsed = lambda self: 0.0
    import time
    time.time = lambda: 1700000000.0

run_id, audit_path, fault_point, fault_stage, ordinal = sys.argv[1:6]
store = PostgresRunStore(os.environ['INCIDENT_TEST_POSTGRES_DSN'])
store.setup_checkpoints()
service = service_for(store, audit_path, repair='repair' in sys.argv[6:])
def fault(point, journal):
    if point == fault_point and (fault_stage == '*' or journal.stage_name == fault_stage) and (
            int(ordinal) == 0 or journal.operation_cursor == int(ordinal)):
        os._exit(73)
store._recovery_fault = fault
injected = False
def disconnect(backend, point):
    global injected
    injected = True
    try:
        # Terminate only this test's own pooled connection in the isolated DB.
        # The real server drops it; storage_boundary classifies the driver error.
        with backend.pool.connection() as conn:
            conn.execute('SELECT pg_terminate_backend(pg_backend_pid())')
    except Exception as exc:
        audit(audit_path + '.faults', {'fault': point, 'driver_error': type(exc).__name__})
        raise

original_operation, original_emit, original_settle = RecoveryStore.operation, RecoveryStore.emit, RecoveryStore.settle
def selected(backend, stage, index):
    return not injected and stage == fault_stage and index == int(ordinal)

@storage_boundary
def operation(backend, stage, index):
    row = original_operation(backend, stage, index)
    if fault_point == 'pg_read_disconnect' and row and row['status'] == 'responded' and selected(backend, stage, index):
        disconnect(backend, fault_point)
    return row

@storage_boundary
def emit(backend, key, event, clocks):
    result = original_emit(backend, key, event, clocks)
    from runtime.context import current
    journal = current().journal if current() else None
    expected = {'pg_usage_disconnect': 'usage', 'pg_end_disconnect': 'call_end'}.get(fault_point)
    if journal and expected and event.get('type') == expected and selected(backend, journal.stage_name, journal.operation_cursor):
        disconnect(backend, fault_point)
    return result

def settle(backend, stage, index, **kwargs):
    result = original_settle(backend, stage, index, **kwargs)
    if kwargs.get('error'):
        from runtime.context import current
        fault('after_failure_saved', current().journal)
    return result

RecoveryStore.operation, RecoveryStore.emit, RecoveryStore.settle = operation, emit, settle
class CutAfterResponseModel(RecoveryModel):
    def invoke(self, messages):
        response = super().invoke(messages)
        request = json.loads(messages[-1].content)
        payload = request.get('input', request)
        if scenario in {'raw_provider', 'execution_timeout', 'provider_timeout'} and request.get('stage') == 'investigate':
            collection = payload.get('collection', [])
            if collection and all(row['status'] == 'error' for row in collection):
                response.content = json.dumps({'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': 'resource'}})
        if not request.get('stage') and not payload.get('challenge_policy_version'):
            if scenario == 'raw_model':
                class RawSDKError(Exception):
                    code = 'unclassified_upstream_code'
                raise RawSDKError('synthetic unexpected model failure')
            if scenario == 'model_auth':
                raise ExecutionError('AUTH_ERROR', 'model')
        from runtime.context import current
        fault('after_external_response', current().journal)
        return response
service._incident.model_factory = lambda *args: CutAfterResponseModel(audit_path, 'repair' in sys.argv[6:])
original = BoundIncidentProvider.query
def query(self, name, args, scope):
    audit(audit_path, {'kind': 'tool', 'name': name, 'args': args.model_dump(mode='json')})
    if name == 'get_service_logs' and args.category == 'dependency':
        if scenario == 'raw_provider':
            raise ValueError('synthetic unexpected provider failure')
        if scenario == 'execution_timeout':
            raise ExecutionError('TIMEOUT', 'embedding', True)
        if scenario == 'provider_timeout':
            raise ProviderError('TIMEOUT', True)
    result = original(self, name, args, scope)
    from runtime.context import current
    fault('after_external_response', current().journal)
    return result
try:
    with patch.object(BoundIncidentProvider, 'query', query):
        result = execute(service, run_id, execution_request(store.get(run_id), service))
    print(json.dumps({'status': result['status'] if result else 'not_owned',
        'reason': result.get('run_summary', {}).get('termination_reason') if result else None,
        'counts': {key: result.get('run_summary', {}).get(key) for key in ('model_calls', 'tool_calls')} if result else None}))
except RecoveryInterrupted as exc:
    if not injected:
        raise
    print(json.dumps({'status': 'paused', 'exception': type(exc).__name__, 'fault': fault_point}))
    sys.exit(74)
finally:
    service.close()
