"""Free multi-role closure and review-input boundaries; no model or external I/O."""
from copy import deepcopy
import json
import unittest

from agents.contracts import DiagnosisSelection, ReviewDecision, TaskRequest
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate
from workflow.handoff import owner_choice


class OutageProvider(FixtureProvider):
    def __init__(self, owner_error=None, empty_owner=False, conflict=False):
        super().__init__('case_001', P)
        self.calls = []
        self.owner_error, self.empty_owner, self.conflict = owner_error, empty_owner, conflict
        if conflict:
            other = deepcopy(self._data['owners'][0])
            other.update(id='other-owner', team='Conflicting Team')
            self._data['owners'].append(other)

    def query(self, name, args, scope):
        self.calls.append(name)
        if name in {'get_service_metrics', 'get_service_logs'}:
            raise ProviderError('SOURCE_UNAVAILABLE')
        if name == 'get_service_owner':
            if self.owner_error:
                raise self.owner_error
            if self.empty_owner:
                return [], False
            if self.conflict:
                return deepcopy(self._data['owners']), False
        return super().query(name, args, scope)


class LocalRole:
    json_output = True

    def __init__(self, callback):
        self.callback, self.history = callback, []

    def invoke(self, messages):
        payload = json.loads(messages[1].content)['input']
        self.history.append(payload)
        return json_response(self.callback(payload, len(self.history)))


def closure_models(*, reject_action=False, request_more=False):
    def supervisor(payload, number):
        if number > 1:
            return {'action':'request_info', 'reason':'当前观测渠道不可用，需要人工核实',
                    'missing_information':['缺少当前业务指标和日志']}
        scope = payload['ticket']['scope']
        window = {k:scope[k] for k in ('start','end')}
        return {'action':'dispatch', 'reason':'取得当前故障观测', 'tasks':[{
            'role':'investigation', 'goal':'核实本次异常', 'need_ids':['N1','N2'],
            'expected_value':'核对现象和失败环节', 'checks':[
                {'tool':'get_service_metrics','args':dict(window,metrics=['dependency_error_rate'])},
                {'tool':'get_service_logs','args':dict(window,category='dependency')}]}]}

    def diagnosis(payload, number):
        assert payload['limited_handoff']
        return {'hypotheses':[], 'findings':[], 'recommended_actions':[{
            'action':'请服务负责人核实受影响功能、实际请求错误、复现步骤及发生时间',
            'condition':'当前观测渠道不可用，需要人工补充业务样本',
            'expected_result':'取得实际业务样本，渠道恢复后再核对；无法确认的原因保留未知',
            'risk':'只读信息核查，不修改服务', 'kind':'manual_check','requires_approval':False}],
            'missing_information':['当前业务原因未知'], 'escalation_ref':None}

    def reviewer(payload, number):
        assert payload['limited_handoff']
        result = {'assessments':[{'target_id':key,'verdict':'uncertain' if reject_action else 'supported',
            'reason':'具体人工只读待办' if not reject_action else '任务还不够具体','evidence_ids':[]}
            for key in payload['required_target_ids']],
            'need_assessments':[{'need_id':key,'verdict':'uncertain','reason':'没有当前业务观测'}
                for key in payload['required_need_ids']],
            'follow_up_reason':'受限收尾保留业务未知，不要求追加查询'}
        if request_more:
            result['request_evidence']={'need_id':'N1','missing_observation':'仍缺业务表现',
                'expected_value':'明确现象','checks':[]}
        return result
    models = planned_models()
    models.update(supervisor=LocalRole(supervisor),diagnosis=LocalRole(diagnosis),reviewer=LocalRole(reviewer))
    return models


class MultiMinimalHandoffTests(unittest.TestCase):
    def coordinator(self, *, provider=None, limits=None):
        return Collaboration(closure_models(), provider or OutageProvider(), P,
            limits=limits or Limits(model_calls=16,tool_calls=8,reserve_model_calls=3,reserve_seconds=20))

    def collect(self, c, tool, args):
        task=TaskRequest(role='investigation',goal='本地取证',checks=[{'tool':tool,'args':args}])
        objective=c.task_objective(task,False)
        result=c.collect_task(task,objective,c.executors['investigation'],c.context,c.events,0)
        c.record_task(task,objective,result)

    def fail_read(self,c):
        self.collect(c,'get_service_logs',dict(c.ticket.scope.model_dump(mode='json',include={'start','end'}),category='dependency'))

    def test_full_outage_has_one_owner_read_and_one_judging_round(self):
        provider,models=OutageProvider(),closure_models()
        r=collaborate(models,provider,P)
        self.assertEqual((r['run_summary']['model_calls'],r['run_summary']['tool_calls']),(4,3))
        self.assertEqual((len(models['diagnosis'].history),len(models['reviewer'].history)),(1,1))
        self.assertEqual(r['status'],'partial')
        self.assertEqual(r['review_status'],'needs_information')
        self.assertFalse(r['output']['hypotheses'])
        self.assertTrue(r['output']['recommended_actions'])
        self.assertEqual(r['output']['escalation_team'],provider._data['owners'][0]['team'])
        self.assertTrue(all(n['status'] not in {'supported','not_required'} for n in r['evidence_needs'] if n['purpose'] in {'symptom','mechanism'}))
        self.assertEqual(provider.calls.count('get_service_owner'),1)
        self.assertTrue(any(e.get('execution_source')=='limited_handoff_policy' for e in r['events']))

    def test_rejected_manual_action_is_pending_not_approved(self):
        r=collaborate(closure_models(reject_action=True),OutageProvider(),P)
        self.assertFalse(r['output']['recommended_actions'])
        self.assertTrue(r['pending_actions'])
        self.assertTrue(all(not a['executable'] for a in r['pending_actions']))
        self.assertEqual(r['run_summary']['model_calls'],4)

    def test_review_request_does_not_restart_collection(self):
        r=collaborate(closure_models(request_more=True),OutageProvider(),P)
        self.assertEqual((r['run_summary']['model_calls'],r['run_summary']['tool_calls']),(4,3))
        self.assertEqual(r['rework_rounds'],0)
        self.assertEqual(r['status'],'partial')

    def test_existing_owner_is_reused_and_applied(self):
        c=self.coordinator()
        self.fail_read(c)
        self.collect(c,'get_service_owner',{'alias':c.ticket.scope.service})
        before=dict(c.context.counts)
        self.assertTrue(c.prepare_limited_handoff())
        self.assertEqual(c.context.counts,before)
        self.assertEqual(c.team,c.provider._data['owners'][0]['team'])
        self.assertFalse(c.prepare_limited_handoff())

    def test_previous_failed_owner_read_is_not_retried(self):
        c=self.coordinator(provider=OutageProvider(owner_error=ProviderError('SOURCE_UNAVAILABLE')))
        self.fail_read(c)
        self.collect(c,'get_service_owner',{'alias':c.ticket.scope.service})
        before=dict(c.context.counts)
        self.assertTrue(c.prepare_limited_handoff())
        self.assertEqual(c.context.counts,before)
        self.assertIsNone(c.team)

    def test_transient_owner_failure_is_one_actual_attempt(self):
        provider=OutageProvider(owner_error=ProviderError('TIMEOUT',retryable=True))
        r=collaborate(closure_models(),provider,P)
        self.assertEqual(provider.calls.count('get_service_owner'),1)
        self.assertEqual(r['run_summary']['tool_calls'],3)
        self.assertIsNone(r['output']['escalation_team'])
        self.assertEqual(r['status'],'partial')

    def test_empty_owner_does_not_invent_a_team(self):
        r=collaborate(closure_models(),OutageProvider(empty_owner=True),P)
        self.assertIsNone(r['output']['escalation_team'])
        self.assertIn('未取得可用登记负责人', '\n'.join(r['output']['missing_information']))

    def test_conflicting_owner_remains_unknown(self):
        r=collaborate(closure_models(),OutageProvider(conflict=True),P)
        self.assertIsNone(r['output']['escalation_team'])
        self.assertIn('冲突','\n'.join(r['output']['missing_information']))

    def test_unrelated_owner_is_not_selected(self):
        c=self.coordinator()
        self.collect(c,'get_service_owner',{'alias':c.ticket.scope.service})
        self.assertEqual(owner_choice(c.evidence,'unregistered-other-service').status,'missing')

    def test_truncated_owner_is_not_treated_as_unique(self):
        c=self.coordinator()
        self.fail_read(c)
        self.collect(c,'get_service_owner',{'alias':c.ticket.scope.service})
        event=next(e for e in c.events if e.get('name')=='get_service_owner' and e['type']=='tool_result')
        event['truncated']=True
        for query in c.executors['investigation'].check_results.values():
            if query['tool']=='get_service_owner':
                query['truncated']=True
        c.prepare_limited_handoff()
        self.assertIsNone(c.team)
        self.assertIn('负责人查询结果截断','\n'.join(c.extra_gaps))
        selection=DiagnosisSelection(escalation_ref={'reference_id':next(
            ref for ref,value in c.references.items() if value.field_path=='team')})
        with self.assertRaises(ProtocolError) as caught:
            c.diagnosis(selection)
        self.assertEqual(caught.exception.code,'HANDOFF_OWNER_UNCERTAIN')

    def test_low_model_budget_keeps_concrete_unreviewed_notes(self):
        c=self.coordinator()
        self.fail_read(c)
        c.context.counts['model_calls']=15
        before=dict(c.context.counts)
        self.assertTrue(c.prepare_limited_handoff())
        self.assertEqual(c.context.counts,before)
        r=c.result()
        self.assertEqual(r['status'],'partial')
        self.assertFalse(r['output']['recommended_actions'])
        self.assertIn('复现步骤','\n'.join(r['output']['missing_information']))

    def test_exhausted_tools_still_allow_judging_without_owner(self):
        c=self.coordinator()
        self.fail_read(c)
        c.context.counts['tool_calls']=8
        self.assertTrue(c.prepare_limited_handoff())
        self.assertEqual(c.next_node,'diagnosis')
        self.assertEqual(c.context.counts['tool_calls'],8)

    def test_expired_time_does_not_add_any_calls(self):
        c=self.coordinator()
        self.fail_read(c)
        c.context.started-=c.context.limits.seconds
        before=dict(c.context.counts)
        self.assertTrue(c.prepare_limited_handoff())
        self.assertEqual(c.context.counts,before)
        self.assertEqual(c.stop,'NEEDS_INFORMATION')

    def test_fatal_owner_error_stops_before_judging(self):
        models=closure_models()
        r=collaborate(models,OutageProvider(owner_error=ProviderError('AUTH_ERROR')),P)
        self.assertEqual(r['run_summary']['termination_reason'],'AUTH_ERROR')
        self.assertFalse(models['diagnosis'].history)
        self.assertFalse(models['reviewer'].history)

    def test_status_check_and_agent_mode_do_not_enter_closure(self):
        for change in ('status','agent'):
            c=self.coordinator()
            self.fail_read(c)
            if change=='status':
                c.ticket=c.ticket.model_copy(update={'purpose':'status_check'})
            else:
                c.investigation_mode='agent'
            self.assertFalse(c.prepare_limited_handoff())

    def test_limited_closure_cannot_invent_cause_or_change_action(self):
        c=self.coordinator()
        self.fail_read(c)
        c.prepare_limited_handoff()
        for data,code in [({'hypotheses':[{'cause':'编造业务原因'}]},'HANDOFF_CAUSE_NOT_OBSERVED'),
                          ({'recommended_actions':[{'action':'修改配置','condition':'未知','expected_result':'未知',
                            'risk':'未知','kind':'manual_change','requires_approval':True}]},'HANDOFF_READ_ONLY_REQUIRED')]:
            with self.subTest(code=code), self.assertRaises(ProtocolError) as caught:
                c.diagnosis(DiagnosisSelection.model_validate(data))
            self.assertEqual(caught.exception.code,code)

    def test_owner_cannot_close_core_business_needs(self):
        c=self.coordinator()
        self.fail_read(c)
        c.prepare_limited_handoff()
        review=ReviewDecision(assessments=[{'target_id':'A1','verdict':'supported','reason':'核查'}],
            need_assessments=[{'need_id':'N1','verdict':'supported','reason':'owner并非业务观测'}])
        with self.assertRaises(ProtocolError) as caught:
            c.validate_review(review)
        self.assertEqual(caught.exception.code,'HANDOFF_CORE_UNRESOLVED')

    def test_result_reuses_observed_owner_without_querying_on_render(self):
        c=Collaboration(planned_models(),FixtureProvider('case_001',P),P)
        self.collect(c,'get_service_owner',{'alias':c.ticket.scope.service})
        c.stop='NEEDS_INFORMATION'
        before=dict(c.context.counts)
        self.assertEqual(c.result()['output']['escalation_team'],c.provider._data['owners'][0]['team'])
        c.result()
        self.assertEqual(c.context.counts,before)

    def test_full_explanation_is_visible_and_uncertain_is_not_supported(self):
        models=planned_models()
        def draft(response,*_):
            data=json.loads(response.content)
            data['hypotheses'][0]['evidence_explanation']='进程存活因此排除所有资源瓶颈（待复核陈述）'
            return json_response(data)
        def review(response,messages,_):
            payload=json.loads(messages[1].content)['input']
            self.assertEqual(payload['targets']['H1']['claim_explanation_to_audit'],'进程存活因此排除所有资源瓶颈（待复核陈述）')
            self.assertNotIn('status',payload['targets']['H1'])
            data=json.loads(response.content)
            next(a for a in data['assessments'] if a['target_id']=='H1').update(verdict='uncertain',reason='局部存活不支持全部排除')
            data.update(request_evidence=None,follow_up_reason='需收窄表达，不新增查询')
            return json_response(data)
        models['diagnosis']=AlterOutput(models['diagnosis'],draft)
        models['reviewer']=AlterOutput(models['reviewer'],review)
        r=collaborate(models,FixtureProvider('case_001',P),P)
        self.assertEqual(r['status'],'partial')
        self.assertNotEqual(r['output']['hypotheses'][0]['status'],'supported')


if __name__=='__main__':
    unittest.main()
