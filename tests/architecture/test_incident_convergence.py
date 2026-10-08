"""Backend convergence: real role nodes, default IT routes, no research imports."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app_main import create_app
from api.auth import Principal, get_current_principal
from config.settings import AppSettings
from config.incident import IncidentConfig
from workflow.dependencies import get_workflow_service
from workflow.service import WorkflowService
from storage.runs import MemoryRunStore
from providers.bound import catalog
from workflow.coordinator import Collaboration
from agents.scripted import scripted_models
from workflow.graph import build_graph
from providers.fixtures import FixtureProvider


class IncidentConvergenceTests(unittest.TestCase):
    def test_actual_graph_visits_individual_roles_and_one_rework(self):
        principal = Principal('synthetic_demo', 'cli_reader')
        coordinator = Collaboration(scripted_models(), FixtureProvider('case_003', principal), principal)
        self.assertEqual(set(build_graph(coordinator).get_graph().nodes),
            {'__start__', '__end__', 'supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer', 'finish'})
        result = coordinator.run()
        self.assertEqual(result['workflow_engine'], 'langgraph')
        self.assertEqual(result['status'], 'completed')
        nodes = [e['node'] for e in result['events'] if e['type'] == 'phase']
        self.assertEqual(nodes, ['supervisor', 'investigation', 'knowledge', 'supervisor',
            'diagnosis', 'reviewer', 'supervisor', 'investigation', 'diagnosis', 'reviewer'])
        self.assertEqual(result['rework_rounds'], 1)

    def test_default_api_canonical_and_legacy_readers_share_owned_run(self):
        service = WorkflowService('unused', store=MemoryRunStore(), config=IncidentConfig())
        self.addCleanup(service.close)
        owner = Principal('team_a', 'alice')
        app = create_app(AppSettings())
        app.dependency_overrides[get_workflow_service] = lambda: service
        app.dependency_overrides[get_current_principal] = lambda: owner
        # Avoid startup touching a real DB; the injected service already has an in-memory store.
        client = TestClient(app)
        self.addCleanup(client.close)
        paths = client.get('/openapi.json').json()['paths']
        self.assertIn('/api/v1/runs/{run_id}', paths)
        self.assertNotIn('/api/v1/research/run', paths)
        self.assertEqual(client.post('/api/v1/research/run', json={'query':'unused'}).status_code, 404)
        self.assertEqual(client.get('/api/v1/memory').status_code, 404)
        self.assertFalse(any('/memory' in p for p in paths))
        incident = client.post('/api/v1/incidents', json=catalog(owner)[0]['fields']).json()
        started = client.post(f'/api/v1/incidents/{incident["id"]}/diagnoses', json={'request_key':'convergence'}).json()
        run_id = started['run_id']
        asyncio.run(service.wait_result(run_id, owner))
        self.assertEqual(started['result_url'], f'/api/v1/runs/{run_id}')
        canonical = client.get(started['result_url'])
        self.assertEqual(canonical.json(), client.get(f'/api/v1/research/runs/{run_id}').json())
        events = service._store.events(run_id)
        replay = client.get(started['event_url'], headers={'Last-Event-ID':str(events[-2]['seq'])})
        self.assertEqual(replay.headers['X-Run-ID'], run_id)
        self.assertEqual(replay.text.count('"type": "final"'), 1)
        self.assertEqual(client.get(started['event_url'], headers={'Last-Event-ID':'invalid'}).status_code, 400)
        app.dependency_overrides[get_current_principal] = lambda: Principal('team_a','bob')
        for url in (started['result_url'], started['event_url'], f'/api/v1/research/runs/{run_id}'):
            self.assertEqual(client.get(url).status_code, 404)
        self.assertFalse(hasattr(service, 'start_run'))
        self.assertFalse(hasattr(service, '_research_result'))
        self.assertFalse(hasattr(AppSettings(), 'enable_legacy_research'))

    def test_fresh_process_runs_without_research_or_vector_dependencies(self):
        with patch('config.incident.load_dotenv'), patch.dict(os.environ, {}, clear=True):
            config = IncidentConfig.from_file('unused')
            self.assertEqual(config.api_key, '')
            self.assertEqual(config.model, 'qwen-turbo')
            self.assertNotIn('postgresql://', repr(config))
        code = '''
import builtins
original = builtins.__import__
blocked = ('mult_agents', 'backend', 'archive', 'pymilvus', 'langchain_milvus')
def guarded(name, *args, **kwargs):
    if any(name == p or name.startswith(p + '.') for p in blocked):
        raise AssertionError('IT imported legacy module: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from app_main import create_app
from config.incident import IncidentConfig
from workflow.service import WorkflowService
from storage.runs import MemoryRunStore
from api.auth import Principal
from providers.bound import catalog
import asyncio
service = WorkflowService('unused', store=MemoryRunStore(), config=IncidentConfig())
try:
    owner = Principal('team_a', 'alice')
    incident = service.create_incident(catalog(owner)[0]['fields'], owner)
    run_id, _ = service.start_diagnosis(incident['id'], {'request_key':'isolated',
        'execution_mode':'scripted_control_only', 'model_budget':None, 'tool_budget':None}, owner)
    result = asyncio.run(service.wait_result(run_id, owner))
    assert result['status'] == 'completed', result['run_summary']
    assert result['workflow_engine'] == 'langgraph'
    print('IT isolated run completed')
finally:
    service.close()
'''
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, '-c', code], cwd=root,
            env={**os.environ, 'PYTHONPATH':str(root/'app'), 'ENABLE_LEGACY_RESEARCH':'true'},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('IT isolated run completed', result.stdout)


if __name__ == '__main__':
    unittest.main()
