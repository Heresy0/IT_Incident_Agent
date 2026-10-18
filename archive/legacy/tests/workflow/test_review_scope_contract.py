"""Six free control families. Scripted audit choices are not model quality evidence."""
from copy import deepcopy
import json
import unittest

from api.auth import Principal
from agents.contracts import DiagnosisSelection, ReviewDecision, TaskRequest
from agents.protocol import reviewer_schema
from evidence.diagnosis import ProtocolError
from evals.independent.provider import IndependentProvider
from runtime.context import ExecutionError, Limits, activate
from tests.tools.test_incident_tools import P
from tests.workflow.test_multi_minimal_handoff import LocalRole, OutageProvider, closure_models
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate


def dns_coordinator(*, cause='调用依赖前域名解析失败，请求尚未建立连接', config_evidence=False):
    principal = Principal('synthetic_demo', 'cli_reader')
    provider = IndependentProvider('eval_001', principal)
    if config_evidence:
        # In-memory counterfactual only; never edit the frozen fixture on disk.
        extra = deepcopy(provider._data['logs'][0])
        extra.update(id='local-config-control', error_code='RESOLVER_CONFIG_INVALID',
            message='合成控制：实际加载的域名配置含无效地址；同一请求按该地址解析失败。')
        provider._data['logs'].append(extra)
    models = planned_models()
    models['reviewer'] = LocalRole(lambda *_: {})
    c = Collaboration(models, provider, principal, limits=Limits(model_calls=16, tool_calls=8))
    window = c.ticket.scope.model_dump(mode='json', include={'start','end'})
    task = TaskRequest(role='investigation', goal='本地复用当前DNS观测',
        checks=[{'tool':'get_service_logs','args':dict(window,category='dependency')}])
    objective = c.task_objective(task,False)
    result = c.collect_task(task,objective,c.executors['investigation'],c.context,c.events,0)
    c.record_task(task,objective,result)
    ref = next(key for key,value in c.references.items() if value.field_path=='message'
        and (not config_evidence or '合成控制' in str(value.value)))
    c.draft = c.diagnosis(DiagnosisSelection.model_validate({
        'findings':[{'statement':'复述当前日志','refs':[{'reference_id':ref}]}],
        'hypotheses':[{'cause':cause,'level':'mechanism', 'support_refs':[{'reference_id':ref}],
            'evidence_explanation':'仅审阅此合成控制中实际引用的当前日志，不外推来源范围。'}]}))
    return c


def dns_review(c, *, scope='full', verdict='supported'):
    ids = [ref.evidence_id for ref in c.draft.hypotheses[0].support_refs]
    return {'assessments':[
        {'target_id':'F1','verdict':'supported','reason':'复述实际日志字段','evidence_ids':ids},
        {'target_id':'H1','verdict':verdict,'supported_scope':scope,
         'reason':'日志支持完整合成命题' if scope=='full' else '解析失败有据，服务异常或配置失效部分缺少依据',
         'evidence_ids':ids}],
        'need_assessments':[
            {'need_id':n.need_id,'verdict':'not_required' if n.purpose=='alternative' else 'supported',
             'reason':'无当前竞争机制线索' if n.purpose=='alternative' else '当前日志支持现象和解析失败环节',
             'evidence_ids':[] if n.purpose=='alternative' else ids}
            for n in c.evidence_needs.values()],
        'follow_up_reason':'仅保留措辞与触发来源限制，不规划新查询'}


def ask_review(c):
    # Match Collaboration.run: calls must be charged to this run's context.
    with activate(c.context):
        return c.ask('reviewer',ReviewDecision,c.reviewer_input(),terminal=True,validate=c.validate_review)


class ReviewScopeContractTests(unittest.TestCase):
    def test_family_1_narrow_mechanism_is_not_blocked(self):
        c = dns_coordinator()
        original = ReviewDecision.model_validate(dns_review(c))
        counts = dict(c.context.counts)
        accepted = c.validate_review(original)
        self.assertEqual(accepted.assessments[1].verdict,'supported')
        self.assertEqual(accepted.assessments[1].supported_scope,'full')
        self.assertEqual(c.context.counts,counts)
        view = c.reviewer_input()
        self.assertEqual(view['targets']['H1']['cause'],c.draft.hypotheses[0].cause)
        self.assertEqual(view['targets']['H1']['claim_explanation_to_audit'],c.draft.hypotheses[0].evidence_explanation)
        self.assertEqual(view['assessment_policy']['version'],'scope_and_manual_check_v1')

    def test_family_2_partial_support_cannot_publish_supported(self):
        c = dns_coordinator(cause='DNS解析服务异常或配置失效导致依赖连接中断')
        first = dns_review(c,scope='partial')
        def reviewer(payload,number):
            return deepcopy(first) if number==1 else dns_review(c,scope='partial',verdict='uncertain')
        c.models['reviewer'] = LocalRole(reviewer)
        checked = ask_review(c)
        self.assertEqual(first['assessments'][1]['verdict'],'supported')
        self.assertEqual(checked.assessments[1].verdict,'uncertain')
        self.assertEqual(len(c.models['reviewer'].history),2)
        failures = [e for e in c.events if e['type']=='validation_failure']
        self.assertEqual([e['reason'] for e in failures],['HYPOTHESIS_SCOPE_CONFLICT'])
        self.assertEqual(c.repairs['role_repairs']['reviewer'],1)
        c.review,c.stop = checked,'NEEDS_INFORMATION'
        self.assertEqual(c.result()['output']['hypotheses'][0]['status'],'unresolved')

    def test_family_3_specific_cause_with_its_own_current_evidence_is_allowed(self):
        c = dns_coordinator(cause='当前加载的无效域名配置使本次请求解析失败',config_evidence=True)
        before = dict(c.context.counts)
        checked = c.validate_review(ReviewDecision.model_validate(dns_review(c)))
        self.assertEqual(checked.assessments[1].verdict,'supported')
        self.assertEqual(c.context.counts,before)
        ev = c.evidence[c.draft.hypotheses[0].support_refs[0].evidence_id]
        self.assertEqual(ev.payload['error_code'],'RESOLVER_CONFIG_INVALID')
        self.assertIn('配置',c.draft.hypotheses[0].cause)

    def test_family_4_safe_future_manual_check_is_published_but_business_stays_unknown(self):
        provider = OutageProvider()
        report = collaborate(closure_models(),provider,P)
        self.assertEqual(report['status'],'partial')
        self.assertTrue(report['output']['recommended_actions'])
        self.assertFalse(report['output']['hypotheses'])
        self.assertEqual(report['output']['escalation_team'],provider._data['owners'][0]['team'])
        self.assertEqual(provider.calls.count('get_service_owner'),1)
        self.assertEqual(report['rework_rounds'],0)
        self.assertEqual((report['run_summary']['model_calls'],report['run_summary']['tool_calls']),(4,3))
        self.assertTrue(all(n['status']=='pending' for n in report['evidence_needs']
            if n['purpose'] in {'symptom','mechanism'}))

    def test_family_5_future_result_only_uses_existing_repair_without_auto_approval(self):
        for persist in (False,True):
            with self.subTest(persist=persist):
                models,provider = closure_models(),OutageProvider()
                base = models['reviewer'].callback
                def reviewer(payload,number):
                    result = base(payload,number)
                    if number==1 or persist:
                        result['assessments'][0].update(verdict='uncertain',
                            reason='尚未执行，结果未知',uncertainty_basis='future_result_only')
                    return result
                models['reviewer'] = LocalRole(reviewer)
                report = collaborate(models,provider,P)
                self.assertEqual(len(models['reviewer'].history),2)
                self.assertEqual(report['repair_counts']['reviewer'],1)
                self.assertEqual((report['run_summary']['model_calls'],report['run_summary']['tool_calls']),(5,3))
                self.assertEqual(provider.calls.count('get_service_owner'),1)
                self.assertEqual(report['rework_rounds'],0)
                self.assertEqual(report['status'],'partial')
                failures = [e for e in report['events'] if e['type']=='validation_failure']
                self.assertTrue(all(e['reason']=='MANUAL_CHECK_FUTURE_RESULT_ONLY' for e in failures))
                if persist:
                    self.assertEqual(len(failures),2)
                    self.assertFalse(report['output']['recommended_actions'])
                    self.assertTrue(report['pending_actions'])
                    self.assertTrue(all(not a['executable'] for a in report['pending_actions']))
                    self.assertEqual(report['run_summary']['termination_reason'],'MODEL_OUTPUT_INVALID')
                else:
                    self.assertEqual(len(failures),1)
                    self.assertTrue(report['output']['recommended_actions'])
                    self.assertFalse(report['pending_actions'])
                    self.assertEqual(report['run_summary']['termination_reason'],'NEEDS_INFORMATION')

    def test_family_6_actual_objections_stay_pending_and_changes_stay_blocked(self):
        objections = {'relevance_gap':'没有具体核查对象或业务事项',
            'scope_or_safety_gap':'建议超出授权范围', 'assumed_outcome':'建议预设故障已经恢复'}
        for basis,reason in objections.items():
            with self.subTest(basis=basis):
                models = closure_models()
                base = models['reviewer'].callback
                def reviewer(payload,number):
                    result = base(payload,number)
                    result['assessments'][0].update(verdict='uncertain',reason=reason,uncertainty_basis=basis)
                    return result
                models['reviewer'] = LocalRole(reviewer)
                report = collaborate(models,OutageProvider(),P)
                self.assertFalse(report['output']['recommended_actions'])
                self.assertTrue(report['pending_actions'])
                self.assertEqual(report['repair_counts']['reviewer'],0)
                self.assertEqual(report['status'],'partial')
        c = Collaboration(closure_models(),OutageProvider(),P)
        c.limited_handoff = True
        bad = DiagnosisSelection.model_validate({'recommended_actions':[{
            'action':'修改服务配置','kind':'manual_change','condition':'未核实',
            'expected_result':'恢复','risk':'影响服务','requires_approval':True}]})
        with self.assertRaises(ProtocolError) as caught:
            c.diagnosis(bad)
        self.assertEqual(caught.exception.code,'HANDOFF_READ_ONLY_REQUIRED')

    def test_missing_scope_requires_explicit_new_json_audit_but_legacy_report_parses(self):
        c = dns_coordinator()
        raw = dns_review(c)
        del raw['assessments'][1]['supported_scope']
        legacy = ReviewDecision.model_validate(raw)
        self.assertIsNone(legacy.assessments[1].supported_scope)
        before = legacy.model_dump()
        with self.assertRaises(ProtocolError) as caught:
            c.validate_review(legacy)
        self.assertEqual(caught.exception.code,'HYPOTHESIS_SCOPE_REQUIRED')
        self.assertEqual(legacy.model_dump(),before)

    def test_missing_manual_check_basis_is_not_inferred_from_prose(self):
        models = closure_models()
        base = models['reviewer'].callback
        def reviewer(payload,number):
            result = base(payload,number)
            result['assessments'][0].update(verdict='uncertain',reason='尚未执行')
            return result
        models['reviewer'] = LocalRole(reviewer)
        report = collaborate(models,OutageProvider(),P)
        failures = [e for e in report['events'] if e['type']=='validation_failure']
        self.assertEqual([e['reason'] for e in failures],['MANUAL_CHECK_BASIS_REQUIRED']*2)
        self.assertFalse(report['output']['recommended_actions'])

    def test_audit_requirements_are_advertised_per_target(self):
        c = dns_coordinator()
        schema = reviewer_schema(ReviewDecision.model_json_schema(),c.evidence_needs,c.targets(),bounded_refutation=True)
        rules = schema['$defs']['Assessment']['allOf']
        self.assertTrue(any(r['then'].get('required')==['supported_scope'] for r in rules))
        c.draft = c.draft.model_copy(update={'recommended_actions':[
            DiagnosisSelection.model_validate({'recommended_actions':[{
                'action':'人工核实','kind':'manual_check','condition':'未知','expected_result':'取得记录',
                'risk':'只读','requires_approval':False}]}).recommended_actions[0]]})
        schema = reviewer_schema(ReviewDecision.model_json_schema(),c.evidence_needs,c.targets())
        rules = schema['$defs']['Assessment']['allOf']
        rule = next(r for r in rules if r['then'].get('required')==['uncertainty_basis'])
        self.assertEqual(rule['if']['properties']['target_id']['enum'],['A1'])
        self.assertEqual(rule['if']['properties']['verdict']['const'],'uncertain')

    def test_persistent_hypothesis_conflict_cannot_obtain_extra_repairs(self):
        c = dns_coordinator(cause='DNS服务异常或配置失效')
        c.models['reviewer'] = LocalRole(lambda *_: dns_review(c,scope='partial'))
        with self.assertRaises(ExecutionError) as caught:
            ask_review(c)
        self.assertEqual(caught.exception.code,'MODEL_OUTPUT_INVALID')
        self.assertEqual(len(c.models['reviewer'].history),2)
        self.assertIsNone(c.review)
        self.assertEqual(c.context.counts['model_calls'],2)

    def test_full_is_a_model_declaration_not_program_proven_semantics(self):
        c = dns_coordinator(cause='一个刻意没有来源支持的具体触发原因')
        # A lying/mistaken full declaration remains a real-model validation risk.
        accepted = c.validate_review(ReviewDecision.model_validate(dns_review(c,scope='full')))
        self.assertEqual(accepted.assessments[1].verdict,'supported')


if __name__=='__main__':
    unittest.main()
