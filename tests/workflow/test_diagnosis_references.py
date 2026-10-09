"""Strict visible references and actionable single repair; no paid calls or live writes."""
import json
import unittest

from agents.contracts import DiagnosisSelection
from agents.scripted import scripted_models
from evidence.diagnosis import ProtocolError, diagnosis_reference_options
from evidence.render import render
from providers.fixtures import FixtureProvider
from workflow.coordinator import Collaboration, collaborate
from tests.tools.test_incident_tools import P, window
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_diagnosis_completion import WorkerObservationModel, WorkerDiagnosisModel, worker_provider


class DiagnosisReferenceTests(unittest.TestCase):
    def registry(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P)
        ex = c.executors['investigation']
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['dependency_error_rate']})
        c.evidence.update(ex.evidence)
        c.references.update(ex.references)
        ref = diagnosis_reference_options(c)[0]['reference_id']
        return c, {'reference_id': ref}

    def selection(self, field, refs):
        if field == 'findings':
            return DiagnosisSelection.model_validate({'findings': [{'statement': '当前观测', 'refs': refs}]})
        return DiagnosisSelection.model_validate({'hypotheses': [{'cause': '待复核原因', field: refs}]})

    def run_worker(self, alter):
        models = scripted_models()
        models['investigation'] = WorkerObservationModel()
        histories = []
        def output(response, messages, calls):
            histories.append(messages)
            return alter(response, messages, calls)
        models['diagnosis'] = AlterOutput(WorkerDiagnosisModel(False), output)
        return collaborate(models, worker_provider(), P), histories

    def test_schema_enumerates_only_exposed_references_and_unique_lists(self):
        result, histories = self.run_worker(lambda response, *_: response)
        data = json.loads(histories[0][1].content)
        schema = data['output_schema']
        expected = {option['reference_id'] for evidence in data['input']['evidence']
                    for option in evidence['reference_options']}
        self.assertEqual(set(schema['$defs']['ReferenceSelection']['properties']['reference_id']['enum']), expected)
        self.assertTrue(schema['$defs']['SelectedFinding']['properties']['refs']['uniqueItems'])
        for field in ('support_refs', 'counter_refs'):
            self.assertTrue(schema['$defs']['HypothesisSelection']['properties'][field]['uniqueItems'])
        hidden = {option['reference_id'] for evidence in result['evidence']
                  for option in evidence['reference_options'] if option['field_path'] == 'metric'}
        self.assertTrue(hidden)
        self.assertTrue(hidden.isdisjoint(expected))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['run_summary']['model_calls'], 6)

    def test_unknown_reference_reports_exact_path_without_echoing_rejected_id(self):
        c, _ = self.registry()
        bad = 'REF_' + '0' * 24
        for field, path in [('findings', 'findings.0.refs.0.reference_id'),
                            ('support_refs', 'hypotheses.0.support_refs.0.reference_id'),
                            ('counter_refs', 'hypotheses.0.counter_refs.0.reference_id')]:
            with self.subTest(field=field), self.assertRaises(ProtocolError) as caught:
                c.diagnosis(self.selection(field, [{'reference_id': bad}]))
            error = caught.exception
            self.assertEqual(error.code, 'UNKNOWN_REFERENCE')
            self.assertEqual(error.details[0]['path'], path)
            self.assertEqual(error.details[0]['type'], 'unknown_reference')
            self.assertNotIn(bad, json.dumps(error.details))

    def test_duplicate_reference_is_rejected_in_each_list_without_silent_deduplication(self):
        c, ref = self.registry()
        for field, path in [('findings', 'findings.0.refs.1.reference_id'),
                            ('support_refs', 'hypotheses.0.support_refs.1.reference_id'),
                            ('counter_refs', 'hypotheses.0.counter_refs.1.reference_id')]:
            with self.subTest(field=field), self.assertRaises(ProtocolError) as caught:
                c.diagnosis(self.selection(field, [ref, ref]))
            self.assertEqual(caught.exception.code, 'DUPLICATE_REFERENCE')
            self.assertEqual(caught.exception.details[0]['path'], path)
            self.assertEqual(caught.exception.details[0]['type'], 'duplicate_reference')

    def test_hidden_metadata_cannot_be_selected_even_when_present_in_registry(self):
        c, _ = self.registry()
        evidence = next(iter(c.evidence.values()))
        hidden = next(option.reference_id for option in evidence.reference_options if option.field_path == 'metric')
        self.assertIn(hidden, c.references)
        with self.assertRaises(ProtocolError) as caught:
            c.diagnosis(self.selection('support_refs', [{'reference_id': hidden}]))
        self.assertEqual(caught.exception.code, 'UNKNOWN_REFERENCE')

    def test_unknown_support_can_be_corrected_once_with_server_reference_catalog(self):
        def first_bad(response, _, calls):
            value = json.loads(response.content)
            if calls == 1:
                value['hypotheses'][0]['support_refs'][0] = {'reference_id': 'REF_' + '0' * 24}
            return json_response(value)
        result, histories = self.run_worker(first_bad)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['review_status'], 'passed')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['run_summary']['model_calls'], 7)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        error = next(event for event in result['events'] if event['type'] == 'validation_failure')
        self.assertEqual(error['reason'], 'UNKNOWN_REFERENCE')
        self.assertIn('hypotheses.0.support_refs.0.reference_id', histories[1][-1].content)
        self.assertIn('不能混用', histories[1][-1].content)
        for item in diagnosis_reference_options_from_result(result):
            self.assertIn(item['reference_id'], histories[1][-1].content)
        self.assertEqual(len(histories), 2)

    def test_duplicate_counter_can_be_corrected_once_and_cross_section_reuse_is_valid(self):
        def first_duplicate(response, _, calls):
            value = json.loads(response.content)
            if calls == 1:
                ref = value['hypotheses'][0]['support_refs'][0]
                value['hypotheses'][0]['counter_refs'] = [ref, ref]
            return json_response(value)
        result, histories = self.run_worker(first_duplicate)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['run_summary']['model_calls'], 7)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        error = next(event for event in result['events'] if event['type'] == 'validation_failure')
        self.assertEqual(error['reason'], 'DUPLICATE_REFERENCE')
        self.assertIn('hypotheses.0.counter_refs.1.reference_id', histories[1][-1].content)
        facts = {ref['evidence_id'] for finding in result['output']['findings'] for ref in finding['refs']}
        support = {ref['evidence_id'] for ref in result['output']['hypotheses'][0]['support_refs']}
        self.assertEqual(facts, support)

    def test_persistent_invalid_reference_stops_after_one_repair_with_public_error_location(self):
        def always_bad(response, *_):
            value = json.loads(response.content)
            value['hypotheses'][0]['support_refs'][0] = {'reference_id': 'REF_' + '0' * 24}
            return json_response(value)
        result, histories = self.run_worker(always_bad)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['review_status'], 'not_performed')
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(len(histories), 2)
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(result['reviews'], [])
        self.assertIn('UNKNOWN_REFERENCE', render(result))
        self.assertIn('hypotheses.0.support_refs.0.reference_id', render(result))


def diagnosis_reference_options_from_result(result):
    from evidence.model_view import substantive
    return [option for evidence in result['evidence'] for option in evidence['reference_options']
            if substantive(option['field_path'])]
