import json
import unittest
from langchain_core.messages import AIMessage
from agents.reviewer_references import ReviewerReferences
from agents.reviewer_packet import ReviewerFrame
from evidence.diagnosis import ProtocolError
from tools.executor import ToolExecutor
from workflow.evidence_needs import check_ids
from tests.workflow import test_reviewer_packet as packet_tests
from tests.workflow.test_review_driven import make_coordinator, review_models
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models


class ReviewSelectorTests(unittest.TestCase):
    def transport(self):
        return ReviewerReferences({'EV_abc': object()}, {'RD_' + 'a' * 16: {}})

    def test_typed_paths_round_trip_without_rewriting_prose_or_verdict(self):
        t = self.transport()
        data = {'assessments':[{'evidence_ids':['EV_abc'], 'reason':'EV_abc', 'verdict':'uncertain'}],
            'need_assessments':[{'evidence_ids':['EV_abc']}],
            'scope_assessments':[{'evidence_ids':['EV_abc']}],
            'request_evidence':{'evidence_ids':['EV_abc']},
            'counterpoints':[{'fields':[{'evidence_id':'EV_abc'}], 'claim_quote':'EV_abc'}],
            'manual_tasks':[{'anchor_evidence_ids':['EV_abc'], 'related_reads':[{'read_id':'RD_'+'a'*16}]}],
            'ledger_assertions':[{'read_id':'RD_'+'a'*16}]}
        encoded = t.encode(data)
        self.assertEqual(encoded['counterpoints'][0]['claim_quote'], 'EV_abc')
        self.assertEqual(json.loads(t.decode(json.dumps(encoded))), data)

    def test_cross_kind_unknown_case_and_long_ids_are_never_guessed(self):
        for token in ('Q001','E999','e001','EV_abc','RD_'+'a'*16):
            with self.subTest(token=token), self.assertRaises(ProtocolError) as caught:
                self.transport().decode(json.dumps({'assessments':[{'evidence_ids':[token]}]}))
            self.assertEqual(caught.exception.details[0]['path'], 'assessments.0.evidence_ids.0')
        with self.assertRaises(ProtocolError):
            self.transport().decode(json.dumps({'ledger_assertions':[{'read_id':'E001'}]}))

    def test_original_duplicate_and_unknown_field_guards_remain(self):
        decoded = json.loads(self.transport().decode(json.dumps({'assessments':[{'evidence_ids':['E001','E001']}]})))
        with self.assertRaises(ProtocolError):
            check_ids(decoded['assessments'][0]['evidence_ids'], {'EV_abc':object()})
        value = self.transport().decode(json.dumps({'unrecognized':{'evidence_id':'E001'}}))
        self.assertEqual(json.loads(value)['unrecognized']['evidence_id'], 'E001')

    def test_schema_read_enum_and_payload_catalog_share_the_same_frozen_mapping(self):
        t = self.transport()
        schema = {'properties':{'read_id':{'type':'string','pattern':'old'}},
            '$defs':{'ObservedEvidenceId':{'enum':['EV_abc']}}}
        result = t.encode(schema)
        self.assertEqual(result['properties']['read_id']['enum'], ['Q001'])
        self.assertNotIn('pattern', result['properties']['read_id'])
        self.assertEqual(result['$defs']['ObservedEvidenceId']['enum'], ['E001'])
        with self.assertRaises(TypeError):
            t.evidence['E001'] = 'EV_other'

    def test_counterpoint_reports_discontinuous_quote_and_verdict_together(self):
        helper = packet_tests.ReviewerPacketTests(); helper.setUp()
        target = helper.c.targets()['H1'].model_copy(update={'cause':'本次发布（EV_abc）引入代码缺陷。'})
        helper.frame.targets['H1'] = target
        next(row for row in helper.data['assessments'] if row['target_id']=='H1')['verdict']='not_supported'
        helper.data['counterpoints']=[{'target_id':'H1','claim_quote':'本次发布引入代码缺陷',
            'effect':'weakens','reason':'关联不足','fields':[{'evidence_id':helper.eid,'field_path':'value'}],
            'remaining_uncertainty':'实际生效时间未知'}]
        with self.assertRaises(ProtocolError) as caught:
            helper.frame.decode(json.dumps(helper.data))
        self.assertEqual([row['code'] for row in caught.exception.details],
            ['REVIEW_PACKET_H_VERDICT','COUNTERPOINT_CLAIM_SCOPE'])
        self.assertEqual(caught.exception.details[1]['current_cause'],target.cause)

    def test_tool_parameter_diagnostics_are_precise_and_do_not_echo_values(self):
        errors = ToolExecutor.argument_errors('get_service_logs', {'start':'2026-10-15T10:00:00Z',
            'end':'2026-10-15T10:20:00Z','category':None,'private_secret':'sensitive'})
        self.assertEqual(errors[0]['path'],'category')
        self.assertEqual(errors[0]['expected']['enum'],['dependency','configuration','resource'])
        self.assertNotIn('sensitive',json.dumps(errors))
        self.assertNotIn('private_secret',json.dumps(errors))

    def test_supervisor_corrects_null_category_once_before_any_dispatch(self):
        models = planned_models()
        def invalid(response,messages,calls):
            data = json.loads(response.content)
            if calls == 1:
                data['tasks'][0]['checks'][0]['args']['category'] = None
            return json_response(data)
        models['supervisor'] = AlterOutput(models['supervisor'],invalid)
        report = make_coordinator(models).run()
        failures = [e for e in report['events'] if e['type']=='validation_failure']
        self.assertEqual(len(failures),1)
        self.assertEqual(failures[0]['details'][0]['argument_errors'][0]['path'],'category')
        self.assertEqual(report['status'],'completed')
        self.assertEqual(report['repair_counts']['supervisor'],1)

    def test_json_reviewer_revision_then_final_review_uses_fresh_selectors(self):
        models = review_models()
        original = models['reviewer']; calls=[]
        class JSONReviewer:
            json_output=True
            def invoke(self,messages):
                calls.append(messages)
                data=packet_tests.brief(json.loads(original.invoke(messages).content))
                if len(calls)>1:
                    eid=data['assessments'][0]['evidence_ids'][0]
                    data['manual_tasks']=[{'target_id':'A1','subject':'ticket_service',
                        'question':'核对当前失败请求的协议与配置记录',
                        'requested_fields':['request_id','protocol','configuration_version'],
                        'anchor_evidence_ids':[eid],'completion_criteria':'取得可关联的来源记录；无记录保留未知',
                        'decision_use':'比较协议配置差异并调整下一步取证'}]
                return AIMessage(content=json.dumps(data))
        models['reviewer']=JSONReviewer()
        c=make_coordinator(models); report=c.run()
        self.assertEqual(report['status'],'completed')
        self.assertEqual(len(report['drafts']),2)
        self.assertEqual(len(report['reviews']),2)
        self.assertEqual(len(calls),2)
        self.assertFalse([e for e in report['events'] if e['type']=='validation_failure'])
        self.assertEqual(report['drafts'][0]['draft']['hypotheses'],report['drafts'][1]['draft']['hypotheses'])
        self.assertEqual(report['review_status'],'passed')


if __name__=='__main__':
    unittest.main()
