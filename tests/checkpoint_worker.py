"""Child-process fault injector. Used only by isolated recovery acceptance."""
import json
import logging
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
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

run_id, audit_path, fault_point, fault_stage, ordinal = sys.argv[1:6]
store = PostgresRunStore(os.environ['INCIDENT_TEST_POSTGRES_DSN'])
store.setup_checkpoints()
service = service_for(store, audit_path, repair='repair' in sys.argv[6:])
def fault(point, journal):
    if point == fault_point and (fault_stage == '*' or journal.stage_name == fault_stage) and (
            int(ordinal) == 0 or journal.operation_cursor == int(ordinal)):
        os._exit(73)
store._recovery_fault = fault
class CutAfterResponseModel(RecoveryModel):
    def invoke(self, messages):
        response = super().invoke(messages)
        from runtime.context import current
        fault('after_external_response', current().journal)
        return response
service._incident.model_factory = lambda *args: CutAfterResponseModel(audit_path, 'repair' in sys.argv[6:])
original = BoundIncidentProvider.query
def query(self, name, args, scope):
    audit(audit_path, {'kind': 'tool', 'name': name, 'args': args.model_dump(mode='json')})
    result = original(self, name, args, scope)
    from runtime.context import current
    fault('after_external_response', current().journal)
    return result
try:
    with patch.object(BoundIncidentProvider, 'query', query):
        result = execute(service, run_id, execution_request(store.get(run_id), service))
    print(json.dumps({'status': result['status'] if result else 'not_owned'}))
finally:
    service.close()
