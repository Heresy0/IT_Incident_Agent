"""Precise correction and real query identity; no new orchestration or automatic verdicts."""
import json
import unittest
from agents.contracts import EvidenceNeed, ReviewDecision, SupervisorSelection
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import Collaboration
from workflow.diagnostic_audit import validate_blockers
from workflow.evidence_needs import check_ids
from tests.tools.test_incident_tools import P
from tests.workflow.test_planned_default import planned_models


class MinimalReviewRepairTests(unittest.TestCase):
    def coordinator(self):
        return Collaboration(planned_models(),FixtureProvider('case_001',P),P,
            limits=Limits(model_calls=16,tool_calls=8,seconds=180))

    def plan(self,c,category,goal='Find the actual error'):
        window=c.ticket.scope.model_dump(mode='json',include={'start','end'})
        return c.supervisor_decision(SupervisorSelection(action='dispatch',reason='Read observations',tasks=[{
            'role':'investigation','goal':goal,'need_ids':['N2'],
            'checks':[{'tool':'get_service_logs','args':{**window,'category':category}}],
            'expected_value':'Observe the emitting boundary, not a predetermined cause'}]))

    def test_same_goal_with_different_registered_query_is_not_duplicate(self):
        c=self.coordinator()
        c.prepare_dispatch(self.plan(c,'configuration'));c.execute_task('investigation')
        c.prepare_dispatch(self.plan(c,'resource'));c.execute_task('investigation')
        self.assertEqual(c.context.counts['tool_calls'],2)
        self.assertEqual(len(c.seen_tasks),2)
        self.assertEqual([t['goal'] for t in c.tasks],['Find the actual error']*2)

    def test_identical_completed_query_is_still_reused(self):
        c=self.coordinator()
        c.prepare_dispatch(self.plan(c,'dependency'));c.execute_task('investigation')
        c.prepare_dispatch(self.plan(c,'dependency'))
        self.assertEqual(c.context.counts['tool_calls'],1)
        self.assertEqual(len(c.tasks),1)
        self.assertEqual(len(c.reused_requests),1)

    def test_equivalent_query_sets_in_same_batch_still_rejected_before_reads(self):
        c=self.coordinator();task=self.plan(c,'dependency').tasks[0]
        window=c.ticket.scope.model_dump(mode='json',include={'start','end'})
        other={'tool':'get_service_metrics','args':{**window,'metrics':['dependency_error_rate']}}
        from agents.contracts import ReadOnlyCheck
        metrics=ReadOnlyCheck.model_validate(other)
        task=task.model_copy(update={'checks':[*task.checks,metrics]})
        decision=self.plan(c,'resource').model_copy(update={'tasks':[task,task.model_copy(update={'checks':list(reversed(task.checks))})]})
        with self.assertRaises(ProtocolError) as caught:c.prepare_dispatch(decision)
        self.assertEqual(caught.exception.code,'DUPLICATE_TASK')
        self.assertEqual(c.context.counts['tool_calls'],0)

    def test_agent_mode_keeps_original_goal_duplicate_guard(self):
        c=self.coordinator();c.investigation_mode='agent'
        c.prepare_dispatch(self.plan(c,'configuration'))
        with self.assertRaises(ProtocolError) as caught:c.prepare_dispatch(self.plan(c,'resource'))
        self.assertEqual(caught.exception.code,'DUPLICATE_TASK')
        self.assertEqual(c.context.counts['tool_calls'],0)

    def test_unknown_and_duplicate_ids_have_exact_paths_without_echoing_values(self):
        with self.assertRaises(ProtocolError) as caught:
            check_ids(['EV_valid','private_value','EV_valid'],{'EV_valid':object()},'assessments.0.evidence_ids')
        details=caught.exception.details
        self.assertEqual([d['path'] for d in details],['assessments.0.evidence_ids.1','assessments.0.evidence_ids.2'])
        self.assertEqual([d['type'] for d in details],['unknown_evidence_id','duplicate_evidence_id'])
        self.assertNotIn('private_value',json.dumps(details))

    def test_close_but_truncated_evidence_id_is_not_guessed(self):
        with self.assertRaises(ProtocolError):
            check_ids(['EV_2b5bc682c4607b704d17'],{'EV_2b5bc682c4607b704d281d17':object()})

    def test_reviewer_reference_paths_cover_targets_needs_and_requests(self):
        c=self.coordinator();c.run()
        for location in ('assessments','need_assessments','request_evidence'):
            data=c.review.model_dump(mode='json')
            if location=='request_evidence':
                data[location]={'need_id':'N2','target_id':'H1','missing_observation':'Actual observation',
                    'evidence_ids':['EV_unknown']}
            else:data[location][0]['evidence_ids']=['EV_unknown']
            with self.subTest(location=location),self.assertRaises(ProtocolError) as caught:
                c.validate_review(ReviewDecision.model_validate(data))
            expected=location+('.0' if location!='request_evidence' else '')+'.evidence_ids.0'
            self.assertEqual(caught.exception.details[0]['path'],expected)

    def test_missing_blockers_report_all_questions_in_one_bounded_correction(self):
        needs={f'N{i}':EvidenceNeed(need_id=f'N{i}',purpose=purpose,question='Question',reason='Unassessed')
            for i,purpose in enumerate(('symptom','mechanism','alternative'),1)}
        data={'assessments':[{'target_id':'H1','verdict':'uncertain','reason':'No mechanism proof'}],
            'need_assessments':[{'need_id':key,'verdict':'uncertain','reason':'Missing observations'} for key in needs],
            'request_evidence':{'need_id':'N2','target_id':'H1','missing_observation':'Mechanism records'}}
        review=ReviewDecision.model_validate(data)
        with self.assertRaises(ProtocolError) as caught:validate_blockers(needs,review,{},required=True)
        self.assertEqual([d['need_id'] for d in caught.exception.details],['N1','N3'])
        self.assertEqual([d['path'] for d in caught.exception.details],['need_assessments.0.blocker','need_assessments.2.blocker'])
        self.assertNotIn('cause_level',caught.exception.details[0]['required_fields'])
        self.assertTrue(all(row.verdict=='uncertain' and row.blocker is None for row in review.need_assessments))

    def test_nonblocking_unknown_does_not_acquire_a_fabricated_blocker(self):
        need=EvidenceNeed(need_id='N3',purpose='alternative',question='Question',reason='Unassessed')
        review=ReviewDecision.model_validate({'assessments':[{'target_id':'H1','verdict':'uncertain','reason':'Unknown'}],
            'need_assessments':[{'need_id':'N3','verdict':'uncertain','reason':'No current competing clue','blocking':False}]})
        self.assertIs(validate_blockers({'N3':need},review,{},required=True),review)

    def test_reviewer_receives_requirements_but_not_prior_verdicts(self):
        c=self.coordinator();c.run();payload=c.reviewer_input()
        self.assertIn('reason不能替代',payload['uncertain_need_rule'])
        for need in payload['evidence_needs']:
            self.assertIn('blocking_default',need)
            self.assertNotIn('status',need);self.assertNotIn('reason',need)

    def test_missing_reference_downgrade_is_explained_in_blocker_error(self):
        c=self.coordinator();c.run();c.models['reviewer'].json_output=True
        data=c.review.model_dump(mode='json');data['request_evidence']=None
        row=next(a for a in data['need_assessments'] if a['need_id']=='N1')
        row.update(verdict='supported',evidence_ids=[],blocker=None)
        value=ReviewDecision.model_validate(data)
        with self.assertRaises(ProtocolError) as caught:c.validate_review(value)
        detail=next(d for d in caught.exception.details if d['need_id']=='N1')
        self.assertEqual(detail['support_gate']['submitted_verdict'],'supported')
        self.assertEqual(detail['support_gate']['effective_verdict'],'uncertain')
        self.assertEqual(detail['support_gate']['path'],'need_assessments.0.evidence_ids')
        self.assertEqual(value.need_assessments[0].verdict,'supported')
        self.assertEqual(value.need_assessments[0].evidence_ids,[])


if __name__=='__main__':unittest.main()
