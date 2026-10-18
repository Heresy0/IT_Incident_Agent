"""Free end-to-end simulation; no credentials, database, sockets or container operations."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from api.auth import Principal
from config.incident import IncidentConfig
from providers.bound import catalog
from providers.registry import ServiceBinding, ServiceRegistry
from storage.runs import MemoryRunStore
from workflow.service import WorkflowService


class DemonstrationExecutor:
    name = 'offline_demo'
    live = False
    actions = {'restart_service'}
    verification = '离线模拟：检查状态由unhealthy变为healthy。'

    def __init__(self):
        self.writes = 0

    def preflight(self, *args):
        return {'health': 'unhealthy', 'simulated': True}

    def execute(self, *args):
        self.writes += 1

    def verify(self, *args):
        return {'passed': True, 'health': 'healthy', 'simulated': True}


def main():
    principal = Principal('demo', 'operator', 'operator')
    fields = catalog(principal)[0]['fields']
    executor = DemonstrationExecutor()
    binding = ServiceBinding(principal.tenant_id, (principal.user_id,), fields['service'], fields['environment'],
        fields['service_version'], repairs={'api': {'executor': executor.name, 'actions': ['restart_service']}})
    service = WorkflowService('unused', store=MemoryRunStore(), config=IncidentConfig(postgres_dsn=''),
        service_registry=ServiceRegistry([binding]), repair_executors={executor.name: executor})
    try:
        incident = service.create_incident(fields, principal)
        run_id, _ = service.start_diagnosis(incident['id'], {'request_key': 'diagnose',
            'execution_mode': 'scripted_control_only', 'model_budget': None, 'tool_budget': None}, principal)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = service.get_run(run_id, principal)
            if run['status'] != 'running':
                break
            time.sleep(.02)
        if run['status'] != 'completed' or run['result']['review_status'] != 'passed':
            raise RuntimeError('offline diagnosis did not pass')
        plan = service.repairs.propose(incident['id'], {'request_key': 'propose', 'revision': 1, 'run_id': run_id,
            'action': 'restart_service', 'target': 'api', 'parameters': {}, 'reason': '离线流程演示，非业务诊断结论。',
            'evidence_ids': [run['result']['evidence'][0]['evidence_id']]}, principal)
        service.repairs.approve(incident['id'], plan['id'], {'digest': plan['digest'], 'decision': 'approve'}, principal)
        result = service.repairs.execute(incident['id'], plan['id'], {'request_key': 'execute'}, principal)
        replay = service.repairs.execute(incident['id'], plan['id'], {'request_key': 'execute'}, principal)
        assert result == replay and executor.writes == 1
        print(json.dumps({'evaluation_type': 'excluded_scripted', 'paid_calls': 0, 'live_writes': 0,
            'simulated_writes': executor.writes, 'status': result['status'], 'events': result['events'],
            'incident_status': service.get_incident(incident['id'], principal)['business_status']}, ensure_ascii=False, indent=2))
    finally:
        service.close()


if __name__ == '__main__':
    main()
