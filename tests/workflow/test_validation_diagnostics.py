"""Locate contract failures without persisting rejected text or credentials."""
import json
import unittest

from pydantic import ValidationError
from agents.contracts import ReviewDecision, EvidenceRequest
from agents.investigation import validation_details, literal_options
from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import collaborate


class ValidationDiagnosticsTests(unittest.TestCase):
    def details(self, actual, *, extra=False):
        value = {'assessments': [{'target_id': 'F1', 'verdict': 'supported', 'reason': '核查'}],
                 'need_assessments': [{'need_id': 'N1', 'verdict': actual, 'reason': '核查'}]}
        if extra:
            value['secret-canary-field'] = 'secret-canary-value'
        try:
            ReviewDecision.model_validate(value)
        except ValidationError as exc:
            return validation_details(exc, ReviewDecision)
        self.fail('Expected invalid contract')

    def test_known_wrong_state_has_path_actual_and_expected_literals(self):
        failure = self.details('pending')
        self.assertEqual(failure['details'], [{'path': 'need_assessments.0.verdict',
            'type': 'literal_error', 'actual': 'pending',
            'allowed': ['supported', 'uncertain', 'not_required']}])

    def test_unknown_values_and_extra_field_names_remain_redacted(self):
        failure = self.details('secret-canary-value', extra=True)
        encoded = json.dumps(failure)
        self.assertNotIn('secret-canary', encoded)
        self.assertEqual(failure['details'][0]['actual'], '<redacted>')
        self.assertEqual(failure['details'][1]['path'], '<extra>')

    def test_non_string_literal_input_is_not_serialized(self):
        failure = self.details({'token': 'secret-canary-value'})
        self.assertEqual(failure['details'][0]['actual'], '<redacted>')
        self.assertNotIn('secret-canary', json.dumps(failure))

    def test_optional_nested_contract_resolves_its_trusted_literal_options(self):
        self.assertEqual(literal_options(ReviewDecision, ('request_evidence', 'target_role')),
                         literal_options(EvidenceRequest, ('target_role',)))
        self.assertEqual(literal_options(ReviewDecision, ('unknown', 'verdict')), [])

    def test_reviewer_failures_are_linked_to_calls_and_one_correction(self):
        models = scripted_models()

        def invalid_verdict(response, messages, calls):
            value = json.loads(response.content)
            value['need_assessments'][0]['verdict'] = 'pending'
            return json_response(value)

        models['reviewer'] = AlterOutput(models['reviewer'], invalid_verdict)
        result = collaborate(models, FixtureProvider('case_001', P), P)
        failures = [e for e in result['events'] if e['type'] == 'validation_failure']
        self.assertEqual(len(failures), 2)
        self.assertEqual([e['attempt'] for e in failures], [1, 2])
        self.assertEqual([e['repair_count'] for e in failures], [0, 1])
        self.assertEqual(failures[1]['model_call_index'], failures[0]['model_call_index'] + 1)
        self.assertEqual(failures[1]['schema_version'], failures[0]['schema_version'])
        for failure in failures:
            self.assertEqual(failure['schema_name'], 'ReviewDecision')
            self.assertEqual(failure['role'], 'reviewer')
            self.assertEqual(failure['run_id'], result['run_id'])
            self.assertEqual(failure['details'][0]['path'], 'need_assessments.0.verdict')
            self.assertEqual(failure['details'][0]['actual'], 'pending')
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(result['repairs'], 1)


if __name__ == '__main__':
    unittest.main()
