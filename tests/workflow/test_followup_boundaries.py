"""Deterministic boundary controls, never semantic accuracy or paid calls."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from api.auth import Principal
from providers.bound import catalog, ticket_for
from providers.registry import ServiceRegistry
from runtime.context import ExecutionError, Limits, RunContext
from storage.runs import MemoryRunStore
from tests.support import TestConfig
from tests.workflow.test_autonomous_closed_engine import ClosedModel
from workflow.execution import IncidentAdapter
from workflow.v2 import engine
from workflow.v2.scripted import ScriptedModel


class NoReadScript(ScriptedModel):
    def invoke(self, messages):
        if json.loads(messages[-1].content).get('stage') == 'investigate':
            return AIMessage(content='{"kind":"finish"}', response_metadata={'finish_reason': 'stop'})
        return super().invoke(messages)


class OwnerOnlyScript(ScriptedModel):
    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        if request.get('stage') == 'investigate':
            value = ({'kind': 'query', 'tool': 'get_service_owner', 'args': {'alias': 'checkout-api'}}
                if not request['input']['collection'] else {'kind': 'finish'})
            return AIMessage(content=json.dumps(value), response_metadata={'finish_reason': 'stop'})
        return super().invoke(messages)


class FollowupBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.owner = Principal('team_a', 'alice')
        self.snapshot = {**catalog(self.owner)[0]['fields'], 'id': 'followup-boundary-control', 'revision': 2}
        self.context = {'parent_run_id': 'parent-control', 'round': 1, 'previous_claims': [],
            'submissions': [{'submission_id': 'control', 'submitted_by': 'alice',
                'submitted_at': '2026-10-20T00:00:00Z', 'note': 'UNIQUE_NOTE: rollback did not remove timeout',
                'observations': [], 'verification_status': 'unverified'}]}
        self.adapter = IncidentAdapter(SimpleNamespace(_store=MemoryRunStore(),
            _base_config=TestConfig(), services=ServiceRegistry([])))
        self.limits = Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20)

    def run_case(self, *, model=None, mode='scripted_control_only', spec=None, snapshot=None):
        snapshot = snapshot or self.snapshot
        request = {'incident_snapshot': snapshot, 'tenant_id': 'team_a', 'user_id': 'alice',
            'execution': {'execution_mode': mode, 'model_budget': 16, 'tool_budget': 8},
            'run_spec': {'engine_id': 'autonomous_v2', 'enable_rework': True, 'enable_challenge': False, **(spec or {})},
            'followup_context': deepcopy(self.context)}
        events = []
        runtime = RunContext(limits=self.limits, scope=ticket_for(snapshot, self.owner).scope, emit=events.append)
        with patch('workflow.v2.scripted.scripted_model', side_effect=lambda provider: model or ScriptedModel(provider)):
            result = self.adapter.execute(request, runtime, events)
        self.assertLessEqual(result['run_summary']['model_calls'], 16)
        self.assertLessEqual(result['run_summary']['tool_calls'], 8)
        return result

    def add_record(self):
        self.context['submissions'][0]['observations'] = [{'source_name': 'synthetic operator record',
            'service': self.snapshot['service'], 'observed_at': self.snapshot['end'],
            'content': 'operator statement; not independently verified'}]

    def test_revision_receives_exact_frozen_followup_context(self):
        model = ClosedModel()
        result = self.run_case(model=model)
        revisions = [m['input'] for m in model.messages if m.get('stage') == 'revise']
        self.assertEqual(result['rework_rounds'], 1)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].get('user_followup'), self.context)

    def test_no_review_does_not_claim_review_completed(self):
        with patch.object(engine, 'call_model', side_effect=ExecutionError('MODEL_ERROR')):
            result = self.run_case()
        self.assertEqual(result['review_status'], 'not_performed')
        self.assertEqual(result['reviews'][0]['review']['attempts'], 0)
        self.assertNotIn('本轮结论已重新审阅', result['final'])
        self.assertIn('本轮审阅未完成', result['final'])
        self.assertEqual(result['followup'], self.context)

    def test_auth_failure_preserves_submitted_context_without_spending(self):
        result = self.run_case(mode='live')
        self.assertEqual(result['run_summary']['termination_reason'], 'AUTH_ERROR')
        self.assertEqual(result.get('followup'), self.context)
        self.assertIn('本轮审阅未完成', result['final'])
        self.assertEqual(result['run_summary']['model_calls'], 0)

    def test_source_failure_preserves_submitted_context_without_spending(self):
        result = self.run_case(spec={'source_version_full': 'unavailable-source'})
        self.assertEqual(result['run_summary']['termination_reason'], 'ENGINE_SOURCE_CHANGED')
        self.assertEqual(result.get('followup'), self.context)
        self.assertEqual(result['run_summary']['model_calls'], 0)

    def test_unavailable_provider_keeps_failure_text_and_submitted_context(self):
        result = self.run_case(mode='live', snapshot={**self.snapshot, 'demo_case_id': None})
        self.assertEqual(result['run_summary']['termination_reason'], 'OBSERVATION_PROVIDER_UNAVAILABLE')
        self.assertEqual(result.get('followup'), self.context)
        self.assertIn('尚未绑定真实观测 Provider', result['final'])
        self.assertIn('本轮审阅未完成', result['final'])

    def test_user_only_material_cannot_be_an_independently_observed_completion(self):
        self.add_record()
        from providers.fixtures import FixtureProvider
        result = self.run_case(model=NoReadScript(FixtureProvider('case_001', self.owner)))
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        self.assertEqual(result['review_status'], 'passed')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['termination_reason'], 'USER_SOURCES_UNVERIFIED')
        self.assertEqual(result['source_basis']['status'], 'user_only')
        self.assertEqual(result['source_basis']['independent_observations'], 0)
        self.assertIn('仅基于用户材料', result['final'])
        self.assertFalse(result['repair_eligible'])

    def test_status_check_with_user_only_material_is_also_unverified(self):
        self.add_record()
        from providers.fixtures import FixtureProvider
        result = self.run_case(model=NoReadScript(FixtureProvider('case_001', self.owner)),
            snapshot={**self.snapshot, 'purpose': 'status_check'})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['termination_reason'], 'USER_SOURCES_UNVERIFIED')

    def test_independent_observations_do_not_certify_user_material(self):
        self.add_record()
        result = self.run_case()
        self.assertEqual(result['source_basis']['status'], 'mixed_sources')
        self.assertGreater(result['source_basis']['independent_observations'], 0)
        self.assertFalse(result['source_basis']['user_sources_independently_verified'])
        self.assertEqual(result['evidence'][0]['payload']['verification_status'], 'unverified')

    def test_owner_metadata_does_not_independently_verify_user_material(self):
        self.add_record()
        from providers.fixtures import FixtureProvider
        result = self.run_case(model=OwnerOnlyScript(FixtureProvider('case_001', self.owner)))
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertEqual(result['source_basis']['independent_observations'], 0)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['termination_reason'], 'USER_SOURCES_UNVERIFIED')


if __name__ == '__main__':
    unittest.main()
