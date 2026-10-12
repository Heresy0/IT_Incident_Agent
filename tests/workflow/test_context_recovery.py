"""Near-limit correction tests use the real coordinator, registry and gates."""
import copy
from dataclasses import replace
import json
import unittest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.contracts import DiagnosisSelection
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import activate, ExecutionError
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate
from workflow.model_input import encoded, message_chars, rebuild_correction


class ContextRecoveryTests(unittest.TestCase):
    def test_rebuild_preserves_full_candidate_and_all_original_evidence(self):
        base = [SystemMessage(content='Policy'), HumanMessage(content=encoded({'input': {
            'evidence': [{'observed_at': '2026-10-12T09:00Z', 'message': 'Observed failure',
                          'reference_options': ['REF_observed']}], 'collection': ['failed query']},
            'output_schema': {'required': ['facts', 'causes']}}))]
        old = {'facts': ['observed fact'], 'causes': ['conditional mechanism']}
        history = [*base, AIMessage(content='earlier reply'), HumanMessage(content='Keep the gap explicit')]
        result, metric = rebuild_correction(history, json_response(old), 'Correct the field only', [], 2000)
        self.assertEqual(result[:2], base)
        self.assertEqual(json.loads(result[2].content), old)
        self.assertIn('Keep the gap explicit', result[3].content)
        self.assertEqual(metric['previous_output_mode'], 'full')
        self.assertNotIn('earlier reply', str(result))
        self.assertFalse(metric['evidence_omitted'])
        self.assertFalse(metric['schema_changed'])

    def test_rebuild_retains_intact_erroneous_claim_when_full_reply_cannot_fit(self):
        base = [SystemMessage(content='P'), HumanMessage(content='evidence and schema')]
        bad = {'findings': [{'statement': 'Keep this observation', 'refs': [{'reference_id': 'REF_bad'}]}],
               'irrelevant': 'x' * 7000}
        result, metric = rebuild_correction(base, json_response(bad), 'Use original allowed IDs',
            [{'path': 'findings.0.refs.0.reference_id'}], 500)
        self.assertEqual(result[:2], base)
        self.assertEqual(metric['previous_output_mode'], 'error_context')
        self.assertIn('Keep this observation', result[2].content)
        self.assertIn('REF_bad', result[2].content)
        self.assertLessEqual(message_chars(result), 500)

    def prepared(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        c.run()
        body = json.loads(c.models['diagnosis'].histories[0][1].content)
        initial = next(e['input_chars'] for e in c.events if e['type'] == 'model_input' and e['role'] == 'diagnosis')
        return c, body['input'], initial

    def test_default_diagnosis_repairs_unknown_reference_near_limit_without_dropping_sources(self):
        c, payload, initial = self.prepared()
        snapshots = copy.deepcopy(c.evidence)
        def wrong(response, messages, calls):
            data = json.loads(response.content)
            if calls == 1:
                data['findings'][0]['refs'][0]['reference_id'] = 'REF_' + '0' * 24
                data['hypotheses'][0]['evidence_explanation'] = '保留当前机制候选。' * 35
            return json_response(data)
        model = AlterOutput(planned_models()['diagnosis'], wrong)
        c.models['diagnosis'] = model
        c.context.limits = replace(c.context.limits, input_chars=initial + 1000)
        with activate(c.context):
            draft = c.ask('diagnosis', DiagnosisSelection, payload, terminal=True, validate=c.diagnosis)
        self.assertTrue(draft.findings)
        self.assertEqual(model.calls, 2)
        self.assertEqual(c.repairs['role_repairs']['diagnosis'], 1)
        self.assertEqual(c.evidence, snapshots)
        history = model.model.histories
        self.assertEqual(history[0][:2], history[1][:2])
        rebuilt = next(e for e in c.events if e['type'] == 'correction_input_rebuilt' and e['role'] == 'diagnosis')
        self.assertGreater(rebuilt['before_chars'], c.context.limits.input_chars)
        self.assertLessEqual(message_chars(history[1]), c.context.limits.input_chars)
        self.assertEqual(set(json.loads(history[1][1].content)['output_schema']['$defs']['ReferenceSelection']['properties']['reference_id']['enum']),
                         {o['reference_id'] for e in payload['evidence'] for o in e['reference_options']})

    def test_repeated_bad_reference_still_fails_in_original_correction_allowance(self):
        c, payload, _ = self.prepared()
        def wrong(response, messages, calls):
            data = json.loads(response.content)
            data['findings'][0]['refs'][0]['reference_id'] = 'REF_' + '0' * 24
            return json_response(data)
        c.models['diagnosis'] = AlterOutput(planned_models()['diagnosis'], wrong)
        with activate(c.context), self.assertRaises(ExecutionError) as failure:
            c.ask('diagnosis', DiagnosisSelection, payload, terminal=True, validate=c.diagnosis)
        self.assertEqual(failure.exception.code, 'MODEL_OUTPUT_INVALID')
        self.assertEqual(c.models['diagnosis'].calls, 2)
        self.assertEqual(c.repairs['role_repairs']['diagnosis'], 1)

    def test_unfittable_base_is_not_silently_truncated_or_sent(self):
        c, payload, _ = self.prepared()
        c.context.limits = replace(c.context.limits, input_chars=100)
        before = c.context.counts['model_calls']
        with activate(c.context), self.assertRaises(ExecutionError) as failure:
            c.ask('diagnosis', DiagnosisSelection, payload, terminal=True, validate=c.diagnosis)
        self.assertEqual(failure.exception.code, 'BUDGET_EXCEEDED')
        self.assertEqual(c.context.counts['model_calls'], before)
        gate = next(e for e in c.events if e['type'] == 'budget_rejected')
        self.assertEqual(gate['dimension'], 'input_chars')
        self.assertGreater(gate['actual'], gate['limit'])
        self.assertIn('模型输入字符数', render(c.result()))

    def test_all_role_measurements_only_record_counts(self):
        models = planned_models()
        report = collaborate(models, FixtureProvider('case_001', P), P)
        measurements = [e for e in report['events'] if e['type'] == 'model_input']
        self.assertEqual({e['role'] for e in measurements}, {'supervisor', 'diagnosis', 'reviewer'})
        for event in measurements:
            self.assertTrue(all(isinstance(v, int) for v in event['section_chars'].values()))
            self.assertNotIn('content', event)
            self.assertNotIn('messages', event)
        self.assertEqual(report['run_summary']['model_calls'], 4)

    def test_budget_rejection_callback_can_read_summary_without_deadlock(self):
        c, _, _ = self.prepared()
        c.context.limits = replace(c.context.limits, model_calls=c.context.counts['model_calls'])
        observed = []
        c.context.emit_callback = lambda event: observed.append(c.context.summary())
        with self.assertRaises(ExecutionError):
            c.context.reserve('model', terminal=True)
        self.assertTrue(observed)


if __name__ == '__main__':
    unittest.main()
