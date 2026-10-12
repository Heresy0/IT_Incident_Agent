"""Reviewer input reduction retains observations and the planning boundary."""
import json
import unittest

from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from evidence.model_view import FIELDS
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import collaborate


class ReviewerFocusTests(unittest.TestCase):
    def test_all_observations_and_execution_gaps_remain_visible(self):
        models = scripted_models()
        report = collaborate(models, FixtureProvider('case_001', P), P, investigation_mode='agent')
        body = json.loads(models['reviewer'].histories[0][1].content)
        view = body['input']
        originals = {row['evidence_id']: row for row in report['evidence']}
        self.assertEqual(set(originals), {row['evidence_id'] for row in view['evidence']})
        for row in view['evidence']:
            self.assertEqual(row['payload'], {k: v for k, v in originals[row['evidence_id']]['payload'].items() if k in FIELDS})
        self.assertTrue(view['collection'])
        self.assertTrue(all('completion' in row for row in view['task_results']))
        self.assertIn('task_completion_gaps', view)
        self.assertIn('observation_gaps', view)
        self.assertNotIn('read_only_tools', view)
        self.assertNotIn('owner_options', view)
        self.assertTrue(all('status' not in row and 'evidence_explanation' not in row for row in view['targets'].values()))
        self.assertEqual(report['status'], 'completed')

    def test_schema_keeps_coverage_and_core_rules_without_executable_plans(self):
        models = scripted_models()
        collaborate(models, FixtureProvider('case_001', P), P, investigation_mode='agent')
        body = json.loads(models['reviewer'].histories[0][1].content)
        schema = body['output_schema']
        self.assertEqual(schema['properties']['assessments']['minItems'], len(body['input']['required_target_ids']))
        self.assertEqual(schema['properties']['need_assessments']['minItems'], len(body['input']['required_need_ids']))
        self.assertNotIn('ReadOnlyCheck', schema['$defs'])
        self.assertEqual(schema['$defs']['EvidenceRequest']['properties']['checks']['maxItems'], 0)
        self.assertEqual(schema['$defs']['ReviewBlocker']['properties']['resolution']['enum'], ['external_source', 'not_planned'])
        self.assertEqual(schema['$defs']['NeedAssessment']['allOf'][0]['then']['properties']['blocking']['enum'], [None])
        def check_refs(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    self.assertIn(value['$ref'].removeprefix('#/$defs/'), schema['$defs'])
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)
        check_refs(schema)

    def test_gap_without_tool_parameters_is_planned_only_by_supervisor(self):
        models = scripted_models()
        planned = []
        def gap(response, messages, calls):
            value = json.loads(response.content)
            request = value.get('request_evidence')
            if request:
                request['target_id'] = request.pop('hypothesis_id')
                for field in ('checks', 'proposed_check', 'target_role'):
                    request.pop(field, None)
            return json_response(value)
        def plan(response, messages, calls):
            view = json.loads(messages[1].content)['input']
            challenge = view['pending_challenge']
            if not challenge:
                return response
            self.assertEqual(challenge['checks'], [])
            window = {k: view['ticket']['scope'][k] for k in ('start', 'end')}
            checks = [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['pool_usage', 'pool_wait', 'db_cpu']}},
                      {'tool': 'get_service_logs', 'args': {**window, 'category': 'resource'}}]
            value = json.loads(response.content)
            value.update(action='dispatch', reason='程序控制：调度者独立提供合法检查',
                tasks=[{'role': 'investigation', 'goal': challenge['missing_observation'],
                        'checks': checks, 'expected_value': challenge['expected_value'], 'need_ids': ['N2']}])
            planned.extend(checks)
            return json_response(value)
        models['reviewer'] = AlterOutput(models['reviewer'], gap)
        models['supervisor'] = AlterOutput(models['supervisor'], plan)
        report = collaborate(models, FixtureProvider('case_003', P), P, investigation_mode='agent')
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['rework_rounds'], 1)
        self.assertEqual(len(planned), 2)
        self.assertLessEqual(report['run_summary']['model_calls'], 16)
        self.assertLessEqual(report['run_summary']['tool_calls'], 8)

    def test_input_measurement_does_not_add_model_calls(self):
        report = collaborate(scripted_models(), FixtureProvider('case_001', P), P, investigation_mode='agent')
        events = [e for e in report['events'] if e['type'] == 'model_input' and e.get('schema_name') == 'ReviewDecision']
        self.assertEqual(len(events), 1)
        self.assertGreater(events[0]['input_chars'], events[0]['schema_chars'])
        self.assertEqual(report['run_summary']['model_calls'], 6)


if __name__ == '__main__':
    unittest.main()
