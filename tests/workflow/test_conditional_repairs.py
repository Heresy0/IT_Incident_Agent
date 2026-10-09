"""Free contract/graph regressions with fixture data and scripted role outputs.

These tests verify context delivery and gates, not a live model's reasoning quality.
No executor operation, provider HTTP request or model API call is performed.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agents.scripted import scripted_models
from providers.registry import ServiceBinding
from repairs.service import RepairService
from workflow.coordinator import collaborate
from tests.tools.test_incident_tools import P
from tests.workflow.test_diagnosis_completion import WorkerDiagnosisModel, WorkerObservationModel, worker_provider
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


def worker_binding():
    return ServiceBinding(P.tenant_id, (P.user_id,), 'enterprise-indexing', 'staging', 'local-v1',
        observations={
            'prometheus': {'url': 'http://private-prometheus.invalid', 'authorization_env': 'PRIVATE_TOKEN'},
            'metrics': {'workers_active': {'query': 'private_registered_query',
                'freshness_query': 'private_freshness_query', 'unit': 'workers'}}},
        repairs={'worker': {'executor': 'docker_cli', 'context': 'private-context',
            'container_id': 'a' * 64, 'actions': ['restart_service'],
            'symptom_checks': [{'metric': 'workers_active', 'operator': 'gte', 'threshold': 1}]}})


class ConditionalRepairTests(unittest.TestCase):
    def test_capabilities_share_validated_recovery_checks_without_execution_details(self):
        binding, service = worker_binding(), RepairService(None)
        items = service.capabilities(binding)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['symptom_checks'], [{'metric': 'workers_active', 'operator': 'gte',
            'threshold': 1, 'unit': 'workers', 'max_age_seconds': 60}])
        text = json.dumps(items)
        for hidden in ('private-prometheus', 'PRIVATE_TOKEN', 'private_registered_query',
                       'private_freshness_query', 'private-context', 'container_id', 'authorization_env'):
            self.assertNotIn(hidden, text)
        self.assertTrue(items[0]['requires_approval'])
        items[0]['symptom_checks'][0]['threshold'] = 99
        self.assertEqual(service.capabilities(binding)[0]['symptom_checks'][0]['threshold'], 1)

    def test_invalid_or_missing_live_recovery_checks_hide_the_capability(self):
        for broken in ('no_checks', 'no_freshness', 'unknown_metric'):
            with self.subTest(broken=broken):
                binding = worker_binding()
                if broken == 'no_checks':
                    binding.repairs['worker']['symptom_checks'] = []
                elif broken == 'no_freshness':
                    del binding.observations['metrics']['workers_active']['freshness_query']
                else:
                    binding.repairs['worker']['symptom_checks'][0]['metric'] = 'unregistered'
                self.assertEqual(RepairService(None).capabilities(binding), [])

    def run_worker(self, *, verdict='supported', intent_change=None):
        provider = worker_provider()
        provider.repair_capabilities = RepairService(None).capabilities(worker_binding())
        models = scripted_models()
        models['investigation'] = WorkerObservationModel()
        diagnosis = WorkerDiagnosisModel(False)

        def attach_candidate(response, messages, _):
            draft = json.loads(response.content)
            data = json.loads(messages[1].content)['input']
            evidence_ids = [item['evidence_id'] for item in data['evidence']
                if item['payload'].get('metric') == 'workers_active' and item['payload'].get('value') == 0
                or item['payload'].get('metric') == 'queue_depth' and item['payload'].get('value') == 1]
            intent = {'action': 'restart_service', 'target': 'worker', 'parameters': {},
                      'evidence_ids': evidence_ids}
            intent.update(intent_change or {})
            draft['recommended_actions'][0]['repair'] = intent
            return json_response(draft)

        models['diagnosis'] = AlterOutput(diagnosis, attach_candidate)
        reviewer = models['reviewer']

        def assess_action(response, messages, _):
            decision = json.loads(response.content)
            for assessment in decision['assessments']:
                if assessment['target_id'].startswith('A'):
                    assessment.update(verdict=verdict, reason='有条件恢复建议适用，人工确认条件尚待执行。'
                        if verdict == 'supported' else '维护安排存在待核实的冲突，暂不能支持恢复建议。')
            return json_response(decision)

        models['reviewer'] = AlterOutput(reviewer, assess_action)
        with patch('repairs.executors.DockerCLIExecutor.execute', side_effect=AssertionError('no writes')):
            result = collaborate(models, provider, P)
        return result, diagnosis, reviewer, deepcopy(provider.repair_capabilities)

    def test_both_roles_receive_same_capabilities_and_conditional_candidate_survives_review(self):
        result, diagnosis, reviewer, capabilities = self.run_worker()
        for history in (diagnosis.history, reviewer.histories):
            payload = json.loads(history[0][1].content)['input']
            self.assertEqual(payload['repair_capabilities'], capabilities)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        action = result['output']['recommended_actions'][0]
        self.assertEqual((action['repair']['action'], action['repair']['target']), ('restart_service', 'worker'))
        self.assertEqual(action['repair']['parameters'], {})
        self.assertTrue(action['requires_approval'])
        self.assertIn('不是计划维护', action['condition'])
        self.assertIn('触发原因尚未核实', result['output']['missing_information'][0])
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (6, 2))
        self.assertEqual((result['repairs'], result['rework_rounds']), (0, 0))

    def test_supported_mechanism_does_not_override_an_uncertain_action(self):
        result, *_ = self.run_worker(verdict='uncertain')
        self.assertEqual((result['status'], result['review_status']), ('partial', 'needs_information'))
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'supported')
        self.assertEqual(result['output']['recommended_actions'], [])
        self.assertEqual(result['run_summary']['termination_reason'], 'REVIEW_INCOMPLETE')
        self.assertEqual((result['repairs'], result['rework_rounds']), (0, 0))

    def test_shared_capabilities_do_not_authorize_an_unregistered_target(self):
        result, _, reviewer, _ = self.run_worker(intent_change={'target': 'unregistered-worker'})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(result['output']['recommended_actions'], [])
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(reviewer.histories, [])
