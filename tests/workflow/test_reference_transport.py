"""End-to-end alias decoding preserves real references and bounded rejection."""
import json
import unittest

from langchain_core.messages import AIMessage
from agents.contracts import DiagnosisSelection
from agents.reference_transport import ReferenceTransport
from evidence.diagnosis import ProtocolError
from tests.workflow import test_diagnosis_references
from tests.workflow.test_review_driven import make_coordinator
from tests.workflow.test_planned_default import planned_models


class ReferenceTransportTests(unittest.TestCase):
    def registry(self):
        return test_diagnosis_references.DiagnosisReferenceTests().registry()[0]

    def test_exact_mapping_field_identity_and_prose_preservation(self):
        registry = self.registry()
        transport = ReferenceTransport(registry)
        rid = transport.rows[0]['reference_id']
        data = {'findings': [{'statement': rid, 'refs': [{'reference_id': rid}]}]}
        encoded = transport.encode(data)
        self.assertEqual(encoded['findings'][0]['statement'], rid)
        self.assertEqual(encoded['findings'][0]['refs'][0]['reference_id'], 'R001')
        self.assertEqual(json.loads(transport.decode(json.dumps(encoded))), data)
        self.assertEqual(ReferenceTransport(registry).rows, transport.rows)
        self.assertEqual(len(transport.rows), len(transport.to_canonical))

    def test_unknown_long_ids_wrong_case_and_stale_selectors_are_not_guessed(self):
        transport = ReferenceTransport(self.registry())
        for alias in ('R999', 'r001', 'R01', 'REF_'+'0'*24, 'EV_'+'0'*24):
            with self.subTest(alias=alias), self.assertRaises(ProtocolError) as error:
                transport.decode(json.dumps({'findings':[{'refs':[{'reference_id':alias}]}]}))
            self.assertEqual(error.exception.code,'UNKNOWN_REFERENCE_ALIAS')
            self.assertEqual(error.exception.details[0]['path'],'findings.0.refs.0.reference_id')
        with self.assertRaises(ProtocolError):
            transport.encode({'reference_id':'REF_'+'0'*24})

    def test_revision_consts_concerns_and_runtime_duplicates_keep_original_guards(self):
        registry = self.registry()
        transport = ReferenceTransport(registry)
        rid = transport.rows[0]['reference_id']
        schema = {'properties':{'locked':{'const':{'support_refs':[{'reference_id':rid}]}},
            'reference_id':{'type':'string','pattern':'old','enum':[rid]}}}
        encoded = transport.encode(schema)
        self.assertEqual(encoded['properties']['locked']['const']['support_refs'][0]['reference_id'],'R001')
        self.assertEqual(encoded['properties']['reference_id']['enum'],['R001'])
        raw = {'hypotheses':[{'cause':'test','support_refs':[{'reference_id':'R001'},{'reference_id':'R001'}]}],
               'revision_concerns':[{'candidate':{'counter_refs':[{'reference_id':'R001'}]}}]}
        result = json.loads(transport.decode(json.dumps(raw)))
        self.assertEqual(result['revision_concerns'][0]['candidate']['counter_refs'][0]['reference_id'],rid)
        with self.assertRaises(ProtocolError) as error:
            registry.diagnosis(DiagnosisSelection.model_validate({'hypotheses':result['hypotheses']}))
        self.assertEqual(error.exception.code,'DUPLICATE_REFERENCE')

    def test_real_json_role_reaches_original_validation_after_one_focused_correction(self):
        self.run_json(invalid_forever=False)

    def test_persistent_alias_error_retains_failures_and_uses_no_extra_tool_or_repair(self):
        self.run_json(invalid_forever=True)

    def run_json(self, invalid_forever):
        models = planned_models()
        original = models['diagnosis']
        histories = []
        read_counts = []
        class JSONDiagnosis:
            json_output = True
            def invoke(self, messages):
                body = json.loads(messages[1].content)
                histories.append(messages)
                read_counts.append(c.context.counts['tool_calls'])
                # Existing scripted selector reads the advertised field options;
                # here it receives aliases and returns them as a JSON model would.
                response = original.invoke(messages)
                data = json.loads(response.content)
                if invalid_forever or len(histories)==1:
                    data['findings'][0]['refs'][0]['reference_id']='R999'
                return AIMessage(content=json.dumps(data,ensure_ascii=False))
        models['diagnosis'] = JSONDiagnosis()
        c = make_coordinator(models)
        report = c.run()
        self.assertEqual(len(histories),2)
        self.assertIn('当前允许标签',histories[1][-1].content)
        self.assertEqual(histories[0][:2],histories[1][:2])
        failures=[e for e in report['events'] if e['type']=='validation_failure' and e['role']=='diagnosis']
        self.assertEqual(len(failures),2 if invalid_forever else 1)
        self.assertEqual(failures[0]['reason'],'UNKNOWN_REFERENCE_ALIAS')
        self.assertEqual(report['repair_counts']['diagnosis'],1)
        if invalid_forever:
            self.assertEqual(report['review_status'],'not_performed')
            self.assertEqual(report['run_summary']['termination_reason'],'MODEL_OUTPUT_INVALID')
        else:
            self.assertEqual(report['status'],'completed')
            for finding in report['output']['findings']:
                self.assertTrue(all(ref['evidence_id'].startswith('EV_') for ref in finding['refs']))
        self.assertEqual(set(read_counts),{read_counts[0]})
        self.assertEqual(report['run_summary']['tool_calls'],read_counts[0])


if __name__=='__main__':
    unittest.main()
