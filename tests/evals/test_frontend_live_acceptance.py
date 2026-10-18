"""Free controls for real-Flash demo admission and the persistent spend cap."""
import json
from pathlib import Path
import tempfile
import unittest
from langchain_core.messages import AIMessage, HumanMessage

from scripts.demo_frontend_live_acceptance import LiveLedger, RecordedFlash, configure_demo, MODEL, TENANT, validate_execution
from scripts.demo_frontend_acceptance import validate_dsn


class FrontendLiveGuards(unittest.TestCase):
    def temporary(self):
        folder = Path(__file__).resolve().parents[2] / 'output/frontend-live-guard-tests'
        folder.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix='unit-', dir=folder)

    def test_other_execution_envelopes_rejected_instead_of_silently_using_legacy(self):
        valid = {'execution_mode': 'live', 'model_budget': 16, 'tool_budget': 8}
        validate_execution(valid)
        for invalid in ({**valid, 'model_budget': 12}, {**valid, 'tool_budget': 24},
                        {**valid, 'execution_mode': 'scripted_control_only'}, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_execution(invalid)

    def test_local_service_passes_valid_request_unchanged_and_rejects_before_start(self):
        from unittest.mock import patch
        from scripts.demo_frontend_live_acceptance import ClosedLoopDemoService
        from workflow.service import WorkflowService
        from incidents.support import IncidentError
        # Only admission is exercised. No service, provider, database or model starts.
        service = object.__new__(ClosedLoopDemoService)
        principal = object()
        valid = {'execution_mode': 'live', 'model_budget': 16, 'tool_budget': 8, 'request_key': 'unit'}
        with patch.object(WorkflowService, 'start_diagnosis', return_value=('unit-run', False)) as start:
            self.assertEqual(service.start_diagnosis('unit', valid, principal), ('unit-run', False))
            start.assert_called_once_with('unit', valid, principal)
            with self.assertRaises(IncidentError) as rejected:
                service.start_diagnosis('unit', {**valid, 'model_budget': 12}, principal)
            self.assertEqual(rejected.exception.status, 400)
            self.assertEqual(start.call_count, 1)

    def test_isolated_database_required(self):
        for dsn in ('postgresql://localhost/production', 'postgresql://remote.example/demo_test'):
            with self.subTest(dsn=dsn), self.assertRaises(ValueError):
                validate_dsn(dsn)

    def test_call_cap_survives_restart_and_unknown_usage_is_retained(self):
        from runtime.context import ExecutionError
        with self.temporary() as folder:
            ledger = LiveLedger(folder, maximum=1)
            ledger.admit('run', 'investigation', [HumanMessage(content='synthetic')])
            restarted = LiveLedger(folder, maximum=1)
            with self.assertRaises(ExecutionError):
                restarted.admit('run2', 'reviewer', [])
            self.assertEqual(restarted.summary()['unknown_usage_calls'], 1)
            self.assertEqual(restarted.summary()['remaining_calls'], 0)
            with self.assertRaises(ValueError):
                LiveLedger(folder, maximum=2)

    def test_paid_wrapper_requires_run_context_and_records_measured_usage(self):
        from runtime.context import RunContext, activate, ExecutionError
        class Fake:
            def invoke(self, messages):
                return AIMessage(content='{"synthetic":true}', usage_metadata={
                    'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5},
                    response_metadata={'model_name': MODEL, 'finish_reason': 'stop'})
        with self.temporary() as folder:
            ledger = LiveLedger(folder)
            model = RecordedFlash(Fake(), ledger, 'reviewer')
            with self.assertRaises(ExecutionError):
                model.invoke([])
            with activate(RunContext('unit-run')):
                model.invoke([HumanMessage(content='synthetic-only')])
            self.assertEqual(ledger.summary()['known_usage_calls'], 1)
            self.assertEqual(ledger.summary()['input_tokens_known'], 3)
            self.assertEqual(ledger.summary()['output_tokens_known'], 2)
            saved = json.loads((Path(folder) / 'calls/0001.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['run_id'], 'unit-run')
            self.assertEqual(saved['response_model'], MODEL)

    def test_failed_request_is_not_retried_or_converted_to_zero_usage(self):
        from runtime.context import RunContext, activate, ExecutionError
        class Failing:
            attempts = 0
            def invoke(self, messages):
                self.attempts += 1
                raise ExecutionError('TIMEOUT')
        with self.temporary() as folder:
            ledger, inner = LiveLedger(folder), Failing()
            with activate(RunContext('unit-run')), self.assertRaises(ExecutionError):
                RecordedFlash(inner, ledger, 'diagnosis').invoke([])
            self.assertEqual(inner.attempts, 1)
            self.assertEqual(ledger.summary()['unknown_usage_calls'], 1)

    def test_exact_budget_admitted_with_closed_loop_and_other_budget_falls_back(self):
        from api.auth import Principal
        from config.incident import IncidentConfig
        from providers.registry import ServiceRegistry
        from storage.runs import MemoryRunStore
        from workflow.service import WorkflowService
        from workflow.rollout import RolloutPolicy
        principal = Principal(TENANT, 'operator', 'operator')
        service = WorkflowService('unused', config=IncidentConfig(api_key='unit-only', model=MODEL),
            store=MemoryRunStore(), service_registry=ServiceRegistry([]),
            incident_rollout=RolloutPolicy(mode='off'))
        try:
            with self.temporary() as folder:
                plan = configure_demo(service, Path(folder) / 'admission.json')
                from providers.bound import catalog
                fields = catalog(principal)[0]['fields']
                fields['purpose'] = 'diagnosis'
                incident = service.create_incident(fields, principal)
                execution = {'execution_mode': 'live', 'model_budget': 16, 'tool_budget': 8}
                metadata = service._incident.metadata(execution, incident=incident, principal=principal)
                spec = metadata['run_spec']
                self.assertEqual(spec['engine_id'], 'autonomous_v2')
                self.assertTrue(spec['enable_rework'])
                self.assertTrue(spec['enable_challenge'])
                self.assertEqual(spec['configuration_sha256'], plan['configuration_sha256'])
                changed = service._incident.metadata({**execution, 'model_budget': 12},
                    incident=incident, principal=principal)['run_spec']
                self.assertEqual(changed['engine_id'], 'legacy')
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
