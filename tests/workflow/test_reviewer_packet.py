"""Concise wire decisions keep original scope/approval/uncertainty gates."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from agents.investigation import validation_details
from agents.contracts import RevisionReviewDecision
from agents.protocol import RoleContractContext,role_output_schema,revision_review_generation,parse_role_output
from agents.reviewer_packet import ReviewerFrame,ReviewerPacket
from agents.models import build_model
from config.incident import IncidentConfig
from evidence.diagnosis import ProtocolError
from tests.workflow.test_review_driven import make_coordinator,review_models
from tests.workflow.test_planned_default import planned_models
from workflow.semantic_scope import semantic_schema


def brief(review):
    d=review.model_dump(mode='json') if hasattr(review,'model_dump') else json.loads(json.dumps(review))
    d.pop('request_source',None)
    for a in d['assessments']:a.pop('refutation_basis',None)
    for a in d['need_assessments']:
        if a.get('blocker'):
            for key in ('cause_level','target_role','checks'):a['blocker'].pop(key,None)
    if d.get('request_evidence'):
        for key in ('hypothesis_id','proposed_check','target_role','checks'):d['request_evidence'].pop(key,None)
    for task in d.get('manual_tasks',[]):task.pop('action_quote',None)
    return d


class ReviewerPacketTests(unittest.TestCase):
    def setUp(self):
        self.c=make_coordinator(review_models())
        self.assertEqual(self.c.run()['status'],'completed')
        c=self.c
        self.view=RoleContractContext(c.evidence,c.references,c.executors,c.evidence_needs,c.targets(),
            c.ticket.purpose,c.phase,c.planning_only,c.investigation_mode,c.knowledge_mode,c.review_protection)
        self.frame=ReviewerFrame(self.view)
        self.data=brief(c.review)
        self.eid=next(iter(c.evidence))

    def task(self):
        return {'target_id':'A1','subject':'ticket_service','question':'核实真正缺失的操作审计',
            'requested_fields':['operator','approval_record'],'anchor_evidence_ids':[self.eid],
            'completion_criteria':'取得对象、时间及来源明确的审计记录；无记录继续保留未知',
            'decision_use':'区分触发来源，不作为变更授权'}

    def decoded(self,data):
        return parse_role_output('reviewer',RevisionReviewDecision,self.frame.decode(json.dumps(data)),
            self.view,emit=lambda *args,**kwargs:None)

    def test_program_binds_current_action_and_preserves_all_verdicts(self):
        self.data['manual_tasks']=[self.task()]
        value=self.decoded(self.data)
        self.assertEqual(value.manual_tasks[0].action_quote,self.c.targets()['A1'].action)
        self.assertEqual([(a.target_id,a.verdict) for a in value.assessments],
            [(a['target_id'],a['verdict']) for a in self.data['assessments']])
        checked=self.c.validate_review(value)
        self.assertEqual(checked.manual_tasks[0].target_id,'A1')
        self.assertNotIn('action_quote',ReviewerPacket.model_json_schema()['$defs']['BoundManualTask']['properties'])

    def test_unreviewed_manual_task_has_precise_error_without_promoting_action(self):
        next(a for a in self.data['assessments'] if a['target_id']=='A1')['verdict']='uncertain'
        self.data['manual_tasks']=[self.task()]
        with self.assertRaises(ProtocolError) as caught:self.decoded(self.data)
        self.assertEqual(caught.exception.code,'MANUAL_HANDOFF_UNREVIEWED')
        self.assertEqual(caught.exception.details[0]['path'],'manual_tasks.0')
        self.assertEqual(next(a for a in self.data['assessments'] if a['target_id']=='A1')['verdict'],'uncertain')

    def test_mixed_external_blocker_and_local_source_still_rejected(self):
        n=next(n for n in self.data['need_assessments'] if n['need_id']=='N2')
        n.update(verdict='uncertain',blocker={'affected_targets':['H1'],'missing_observation':'远端内部记录',
            'decision_impact':'区分具体环节','resolution':'external_source'})
        self.data['request_evidence']={'need_id':'N2','target_id':'H1','missing_observation':'混合缺口',
            'source':{'tool':'get_service_logs','category':'dependency'}}
        with self.assertRaises(ProtocolError) as caught:self.decoded(self.data)
        self.assertEqual(caught.exception.code,'REVIEW_SOURCE_HINT_INVALID')
        self.assertEqual(caught.exception.details[0]['path'],'request_evidence.source')

    def test_nonblocking_unknown_matches_runtime_without_manufacturing_support(self):
        n=next(n for n in self.data['need_assessments'] if n['need_id']=='N3')
        n.update(verdict='uncertain',blocking=False,blocker=None,reason='范围有限但无当前线索改变已支持机制')
        value=self.c.validate_review(self.decoded(self.data))
        self.assertEqual(next(n for n in value.need_assessments if n.need_id=='N3').verdict,'uncertain')
        schema=role_output_schema('reviewer',RevisionReviewDecision,self.view,output_base=self.frame.base_schema())
        schema=revision_review_generation(schema,self.c.evidence_needs,self.c.targets(),self.c.evidence,materiality_aligned=True)
        schema=self.frame.finalize_schema(semantic_schema(schema,self.c.targets(),self.c.evidence))
        row=schema['properties']['need_assessments']['prefixItems'][2]
        self.assertEqual(row['oneOf'][-1]['properties']['blocking'],{'const':False})
        self.assertEqual(len(schema['properties']['need_assessments']['prefixItems'][1]['oneOf']),3)

    def test_cause_and_source_metadata_bound_without_relaxing_trigger_blocker_guard(self):
        self.data['manual_tasks']=[self.task()]
        n=next(n for n in self.data['need_assessments'] if n['need_id']=='N2')
        n.update(verdict='uncertain',blocker={'affected_targets':['A1'],'missing_observation':'核查结果',
            'decision_impact':'确认触发','resolution':'external_source'})
        parsed=self.decoded(self.data)
        self.assertEqual(next(n for n in parsed.need_assessments if n.need_id=='N2').blocker.cause_level,'mechanism')
        with self.assertRaises(ProtocolError) as caught:self.c.validate_review(parsed)
        self.assertEqual(caught.exception.code,'REVIEW_BLOCKER_SCOPE')

    def test_truncated_even_complete_json_is_not_published_and_corrects_once(self):
        models=planned_models()
        original=models['reviewer']
        calls=[]
        class JSONReview:
            json_output=True
            def invoke(inner,messages):
                calls.append(messages)
                d=brief(json.loads(original.invoke(messages).content))
                return AIMessage(content=json.dumps(d),response_metadata={'finish_reason':'length' if len(calls)==1 else 'stop'})
        models['reviewer']=JSONReview()
        c=make_coordinator(models)
        r=c.run()
        self.assertEqual(r['status'],'completed')
        self.assertEqual(len(calls),2)
        errors=[e for e in r['events'] if e['type']=='validation_failure' and e['role']=='reviewer']
        self.assertEqual([e['reason'] for e in errors],['MODEL_OUTPUT_TRUNCATED'])
        self.assertEqual(r['repair_counts']['reviewer'],1)
        self.assertIn('截断',calls[1][-1].content)
        self.assertEqual(calls[0][:2],calls[1][:2])

    def test_model_output_and_timeout_override_leave_default_builder_unchanged(self):
        with patch('agents.models.ChatOpenAI') as model,patch('agents.models.IncidentConfig.from_file',return_value=IncidentConfig(api_key='test-only')):
            build_model('test-only',max_output_tokens=4800,request_timeout=60)
            self.assertEqual(model.call_args.kwargs['max_tokens'],4800)
            self.assertEqual(model.call_args.kwargs['timeout'],60)
            build_model('test-only')
            self.assertEqual(model.call_args.kwargs['max_tokens'],2200)
            self.assertEqual(model.call_args.kwargs['timeout'],30)
            self.assertEqual(model.call_args.kwargs['max_retries'],0)

    def test_conciseness_is_guidance_and_original_length_guard_still_applies(self):
        self.data['assessments'][0]['reason'] = '证' * 150
        value = self.decoded(self.data)
        self.assertEqual(value.assessments[0].reason, '证' * 150)
        self.data['assessments'][0]['reason'] = '证' * 501
        with self.assertRaises(ValidationError):
            self.decoded(self.data)

    def test_packet_errors_identify_known_misplaced_fields_and_redact_unknown_keys(self):
        self.data['assessments'][0].update(blocker={'secret': 'not exposed'}, private_response_key='not exposed')
        with self.assertRaises(ValidationError) as caught:
            self.decoded(self.data)
        original = validation_details(caught.exception, RevisionReviewDecision)
        precise = validation_details(caught.exception, RevisionReviewDecision, extra_fields=self.frame.validation_fields)
        self.assertTrue(all(d['path'].endswith('<extra>') for d in original['details']))
        self.assertEqual([d['path'] for d in precise['details']], ['assessments.0.blocker', 'assessments.0.<extra>'])
        self.assertNotIn('private_response_key', json.dumps(precise))
        self.assertNotIn('not exposed', json.dumps(precise))


if __name__=='__main__':unittest.main()
