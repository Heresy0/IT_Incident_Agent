"""Free regression for the observed M5 failures; no live credentials or gold reads."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mult_agents.harness.runtime import Limits
from mult_agents.incident.collaboration import Collaboration, collaborate
from mult_agents.incident.collaboration_contracts import DiagnosisSelection, ReviewDecision
from mult_agents.incident.collaboration_fake import scripted_models
from mult_agents.incident.diagnosis import expand_diagnosis
from mult_agents.incident.investigation import resolve_output, validate_output
from mult_agents.incident.model_view import fit_messages
from mult_agents.incident.providers import FixtureProvider
from mult_agents.incident.single import single_agent
from tests.test_incident_collaboration import AlterOutput, json_response
from tests.test_incident_investigation import selected
from tests.test_incident_tools import P, executor, window


def reference(evidence, field='value'):
    return {'reference_id': next(o.reference_id for o in evidence.reference_options if o.field_path == field)}


class PoolModel:
    def __init__(self, collect_control):
        self.collect_control = collect_control
        self.calls, self.history = 0, []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        self.history.append(list(messages))
        scope = json.loads(messages[1].content)['ticket']['scope']
        query_window = {k: scope[k] for k in ('start', 'end')}
        if self.calls == 1:
            return selected('get_service_metrics', {**query_window, 'metrics': ['pool_usage']}, 'pool')
        if self.calls == 3 and self.collect_control:
            return selected('get_service_metrics', {**query_window, 'metrics': ['db_cpu']}, 'control')
        evidence = [e for m in messages if isinstance(m, ToolMessage) for e in json.loads(m.content)['evidence']]
        pool = next(e for e in evidence if e['payload'].get('metric') == 'pool_usage')
        ref = {'reference_id': next(o['reference_id'] for o in pool['reference_options'] if o['field_path'] == 'value')}
        return json_response({'findings': [{'statement': 'DB CPU normal, ruling out database performance', 'refs': [ref]}],
            'hypotheses': [{'cause': 'Application pool saturation needs investigation', 'support_refs': [ref]}]})


class RetestPathModel(PoolModel):
    """Replay actual query choices, then emit a valid diagnosis from visible sources."""
    def __init__(self, error_only=False, broaden=True):
        super().__init__(False)
        self.error_only, self.broaden = error_only, broaden

    def invoke(self, messages):
        self.calls += 1
        self.history.append(list(messages))
        scope = json.loads(messages[1].content)['ticket']['scope']
        w = {k: scope[k] for k in ('start', 'end')}
        if self.calls == 1:
            queries = [('get_service_metrics', {**w, 'metrics': ['db_cpu','pool_usage','pool_wait']}),
                       ('get_service_logs', {**w, 'category': 'resource', **({'level':'ERROR'} if self.error_only else {})})]
            if not self.error_only:
                queries += [('get_service_logs', {**w,'category':'dependency'}), ('get_service_logs', {**w,'category':'configuration'})]
            return AIMessage(content='', tool_calls=[{'name':n,'args':a,'id':f'first-{i}','type':'tool_call'} for i,(n,a) in enumerate(queries)])
        if self.calls == 2 and not self.error_only:
            return selected('get_service_metrics', {**w,'metrics':['request_error_rate','request_latency']}, 'impact')
        if self.error_only and self.calls == 3 and self.broaden:
            return selected('get_service_logs', {**w,'category':'resource'}, 'broaden')
        sources = []
        for m in messages[2:]:
            if isinstance(m, (ToolMessage, HumanMessage)):
                try:
                    sources += json.loads(m.content).get('evidence', [])
                except json.JSONDecodeError:
                    pass
        pool = next(e for e in sources if e['payload'].get('metric') == 'pool_usage')
        cpu = next(e for e in sources if e['payload'].get('metric') == 'db_cpu')
        def ref(e):
            return {'reference_id': next(o['reference_id'] for o in e['reference_options'] if o['field_path']=='value')}
        return json_response({'findings':[{'statement':'Observed pool pressure','refs':[ref(pool)]}],
            'hypotheses':[{'cause':'Application pool pressure under load remains a candidate',
                'support_refs':[ref(pool)],'counter_refs':[ref(cpu)],'pending_checks':['Human assessment of capacity applicability']}]})


class IncidentRepairTests(unittest.TestCase):
    def test_actual_single_retest_path_now_finishes_without_raising_input_budget(self):
        model = RetestPathModel()
        result = single_agent(model, FixtureProvider('case_003',P), P, limits=Limits(model_calls=16,tool_calls=8,
            reserve_model_calls=3,reserve_seconds=20))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['run_summary']['model_calls'],3)
        self.assertEqual(result['run_summary']['tool_calls'],5)
        self.assertLess(max(sum(len(str(m.content)) for m in history) for history in model.history),28000)
        self.assertEqual(len(result['evidence']),12)
        self.assertTrue(all('hash' in e and 'excerpt' in e and 'allowed_field_paths' in e for e in result['evidence']))
        for history in model.history:
            for m in history:
                if isinstance(m,ToolMessage):
                    self.assertTrue(all('hash' not in e and 'excerpt' not in e and 'allowed_field_paths' not in e
                        for e in json.loads(m.content)['evidence']))
        self.assertEqual(result['run_summary']['limits']['input_chars'],28000)

    def test_context_rollup_keeps_registered_sources_and_never_breaks_tool_pairs(self):
        ex = executor('case_003')
        ex.execute('get_service_metrics',{**window(ex),'metrics':['db_cpu','pool_usage','pool_wait']})
        original = {k:e.model_dump_json() for k,e in ex.evidence.items()}
        messages = [HumanMessage(content='trusted schema'),HumanMessage(content='trusted scoped ticket'),
                    AIMessage(content='old response'*2000), HumanMessage(content='Return diagnosis JSON only')]
        rebuilt, info = fit_messages(messages,ex,[],6000)
        self.assertLessEqual(sum(len(m.content) for m in rebuilt),6000)
        self.assertEqual(rebuilt[:2],messages[:2])
        self.assertTrue(info)
        state = json.loads(rebuilt[-1].content)
        self.assertIn('Return diagnosis JSON only',state['instruction'])
        self.assertTrue(state['evidence'])
        for source in state['evidence']:
            for option in source['reference_options']:
                self.assertIn(option['reference_id'],ex.references)
        self.assertEqual({k:e.model_dump_json() for k,e in ex.evidence.items()},original)
        tiny, metadata = fit_messages(messages[:2],ex,[],10)
        self.assertEqual(tiny,messages[:2])
        self.assertIsNone(metadata)  # Mandatory context still fails at the unchanged harness guard.

    def test_human_capacity_experiment_without_typed_checks_never_dispatches(self):
        models = scripted_models()
        def human_only(response,*_):
            data=json.loads(response.content)
            request=data.get('request_evidence')
            if request:
                request['checks']=[]
                request['proposed_check']='Increase the pool size, then measure wait time.'
            return json_response(data)
        models['reviewer']=AlterOutput(models['reviewer'],human_only)
        result=collaborate(models,FixtureProvider('case_003',P),P)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['rework_rounds'],0)
        self.assertEqual(result['repairs'],0)
        self.assertEqual(result['run_summary']['tool_calls'],3)
        self.assertTrue(any(e['type']=='rework_denied' and e['code']=='READ_ONLY_CHECK_REQUIRED' for e in result['events']))
        self.assertIn('Increase the pool size', '\n'.join(result['output']['missing_information']))

    def test_reviewer_checks_use_tool_role_identity_and_window_preflight(self):
        ex=executor('case_003')
        for role,check,code in [
            ('investigation',{'tool':'get_service_metrics','args':{**window(ex),'metrics':['pool_usage'],
                'start':(ex.scope.start-timedelta(seconds=1)).isoformat()}},'WINDOW_DENIED'),
            ('knowledge',{'tool':'get_service_metrics','args':{**window(ex),'metrics':['pool_usage']}},'TOOL_DENIED'),
            ('investigation',{'tool':'get_service_logs','args':{**window(ex),'category':'resource','tenant_id':'other'}},'INVALID_ARGUMENTS')]:
            with self.subTest(code=code):
                models=scripted_models()
                def invalid(response,*_):
                    data=json.loads(response.content)
                    if data.get('request_evidence'):
                        data['request_evidence'].update(target_role=role,checks=[check])
                    return json_response(data)
                models['reviewer']=AlterOutput(models['reviewer'],invalid)
                result=collaborate(models,FixtureProvider('case_003',P),P)
                self.assertEqual(result['rework_rounds'],0)
                self.assertEqual(result['run_summary']['tool_calls'],3)
                self.assertTrue(any(e['type']=='rework_denied' and e['code']==code for e in result['events']))

    def test_rework_model_cannot_replace_approved_checks_with_other_allowed_tool(self):
        models=scripted_models()
        def drift(response,messages,_):
            data=json.loads(messages[1].content)
            if (data.get('objective') or {}).get('challenge') and response.tool_calls:
                return selected('get_service_owner',{'alias':'checkout-api'},'drift')
            return response
        models['investigation']=AlterOutput(models['investigation'],drift)
        result=collaborate(models,FixtureProvider('case_003',P),P)
        self.assertTrue(any(e['type']=='tool_result' and e['error'] and e['error']['code']=='CHECK_NOT_APPROVED' for e in result['events']))
        self.assertEqual(result['run_summary']['tool_calls'],3)
        self.assertEqual(result['task_results'][-1]['status'],'partial')
        self.assertIn('approved_read_only_checks_not_completed', '\n'.join(result['task_results'][-1]['output']['missing_information']))

    def test_error_empty_feedback_can_observe_warn_and_info_then_finish(self):
        model=RetestPathModel(error_only=True)
        result=single_agent(model,FixtureProvider('case_003',P),P)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['coverage_gaps'],[])
        self.assertEqual(result['run_summary']['model_calls'],4)
        self.assertEqual(result['run_summary']['tool_calls'],3)
        self.assertEqual({e['payload'].get('level') for e in result['evidence'] if 'level' in e['payload']},{'WARN','INFO'})

    def test_ignored_error_empty_gap_is_partial_after_one_feedback(self):
        result=single_agent(RetestPathModel(error_only=True,broaden=False),FixtureProvider('case_003',P),P)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['coverage_gaps'],['logs:resource:WARN/INFO'])
        self.assertEqual(result['run_summary']['model_calls'],3)
        self.assertEqual(result['run_summary']['tool_calls'],2)
        self.assertEqual(sum(e['type']=='observation_gap' for e in result['events']),1)

    def test_reviewer_cannot_accept_cause_with_error_only_log_gap(self):
        c=Collaboration(scripted_models(),FixtureProvider('case_003',P),P)
        ex=c.executors['investigation']
        ex.execute('get_service_metrics',{**window(ex),'metrics':['db_cpu','pool_usage']})
        ex.execute('get_service_logs',{**window(ex),'category':'resource','level':'ERROR'})
        c.evidence.update(ex.evidence); c.references.update(ex.references)
        source=next(e for e in ex.evidence.values() if e.payload.get('metric')=='pool_usage')
        c.draft=c.diagnosis(DiagnosisSelection.model_validate({'hypotheses':[
            {'cause':'Pool exhaustion candidate','support_refs':[reference(source)]}]}))
        review=c.validate_review(ReviewDecision.model_validate({'assessments':[
            {'target_id':'H1','verdict':'supported','reason':'seems enough','evidence_ids':[source.evidence_id]}]}))
        self.assertEqual(review.assessments[0].verdict,'uncertain')
        self.assertIn('WARN/INFO',review.assessments[0].reason)

    def test_fact_only_review_does_not_hide_unchecked_log_levels(self):
        c=Collaboration(scripted_models(),FixtureProvider('case_003',P),P)
        ex=c.executors['investigation']
        ex.execute('get_service_metrics',{**window(ex),'metrics':['db_cpu']})
        ex.execute('get_service_logs',{**window(ex),'category':'resource','level':'ERROR'})
        c.evidence.update(ex.evidence); c.references.update(ex.references)
        source=next(iter(ex.evidence.values()))
        draft=c.diagnosis(DiagnosisSelection.model_validate({'findings':[
            {'statement':'Observed CPU','refs':[reference(source)]}]}))
        review=ReviewDecision.model_validate({'assessments':[
            {'target_id':'F1','verdict':'supported','reason':'Observed numeric value','evidence_ids':[source.evidence_id]}]})
        with patch.object(c,'ask',side_effect=[draft,review]):
            c.diagnose_and_review()
        self.assertEqual(c.result()['status'],'partial')
        self.assertIn('WARN/INFO','\n'.join(c.result()['output']['missing_information']))

    def test_pool_reference_cannot_be_rendered_as_cpu_or_health_conclusion(self):
        ex = executor('case_003')
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['pool_usage']})
        source = next(iter(ex.evidence.values()))
        output, protocol = resolve_output(json.dumps({'findings': [{'statement': 'DB CPU normal, ruling out database performance',
            'refs': [reference(source)]}]}), ex)
        self.assertIn('pool_usage', output.findings[0].statement)
        self.assertNotIn('CPU', output.findings[0].statement)
        self.assertNotIn('normal', output.findings[0].statement)
        self.assertEqual(output.findings[0].refs[0].value, source.payload['value'])
        self.assertEqual(output.findings[0].refs[0].data_version, source.data_version)
        self.assertEqual(protocol, 'reference_selection_v2')
        validate_output(output, ex.evidence)

    def test_scaling_category_and_both_time_samples_come_from_sources(self):
        ex = executor('case_003')
        ex.execute('get_recent_changes', {**window(ex), 'category': 'scaling'})
        change = next(iter(ex.evidence.values()))
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        samples = [e for e in ex.evidence.values() if e.payload.get('metric') == 'db_cpu']
        draft = expand_diagnosis(DiagnosisSelection.model_validate({'findings': [
            {'statement': 'Configuration caused exhaustion', 'refs': [reference(change, 'summary')]},
            {'statement': 'CPU remained normal', 'refs': [reference(e) for e in samples]}]}), ex)
        self.assertIn('scaling change', draft.findings[0].statement)
        self.assertNotIn('caused', draft.findings[0].statement)
        self.assertGreaterEqual(len(samples), 2)
        for e in samples:
            self.assertIn(e.observed_from.isoformat(), draft.findings[1].statement)
        self.assertNotIn('normal', draft.findings[1].statement)

    def test_missing_cpu_feedback_can_collect_control_within_same_budget(self):
        model = PoolModel(True)
        result = single_agent(model, FixtureProvider('case_003', P), P, limits=Limits(model_calls=8, tool_calls=3,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['coverage_gaps'], [])
        self.assertEqual(result['run_summary']['model_calls'], 4)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(sum(e['type'] == 'observation_gap' for e in result['events']), 1)
        self.assertIn('db_cpu', model.history[2][-1].content)
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'tentative')

    def test_refused_control_is_partial_after_one_feedback_not_infinite_retry(self):
        model = PoolModel(False)
        result = single_agent(model, FixtureProvider('case_003', P), P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['business_result'], 'needs_information')
        self.assertEqual(result['coverage_gaps'], ['db_cpu'])
        self.assertEqual(result['run_summary']['model_calls'], 3)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertEqual(result['repairs'], 0)
        self.assertIn('db_cpu', result['output']['missing_information'][0])

    def test_exhausted_tool_budget_keeps_control_gap_without_extra_execution(self):
        result = single_agent(PoolModel(True), FixtureProvider('case_003', P), P,
            limits=Limits(model_calls=6, tool_calls=1, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['run_summary']['model_calls'], 2)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertEqual(result['coverage_gaps'], ['db_cpu'])

    def test_reviewer_cannot_approve_pool_cause_without_current_cpu_control(self):
        coordinator = Collaboration(scripted_models(), FixtureProvider('case_003', P), P)
        ex = coordinator.executors['investigation']
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['pool_usage']})
        coordinator.evidence.update(ex.evidence)
        coordinator.references.update(ex.references)
        source = next(iter(ex.evidence.values()))
        coordinator.draft = coordinator.diagnosis(DiagnosisSelection.model_validate({'hypotheses': [
            {'cause': 'Pool capacity is the cause', 'support_refs': [reference(source)]}]}))
        review = coordinator.validate_review(ReviewDecision.model_validate({'assessments': [
            {'target_id': 'H1', 'verdict': 'supported', 'reason': 'looks sufficient', 'evidence_ids': [source.evidence_id]}]}))
        self.assertEqual(review.assessments[0].verdict, 'uncertain')
        self.assertIn('db_cpu', review.assessments[0].reason)
        self.assertEqual(coordinator.events[-1]['code'], 'MISSING_HEALTH_CONTROL')

    def test_stalled_task_does_not_skip_independent_knowledge_in_same_batch(self):
        models = scripted_models()
        def dispatch(response, _, calls):
            if calls in (1, 2):
                tasks = [{'role': 'investigation', 'goal': f'Current check {calls}'}]
                if calls == 2:
                    tasks.append({'role': 'knowledge', 'goal': 'Confirmed history'})
                return json_response({'action': 'dispatch', 'reason': 'collect', 'tasks': tasks})
            return response
        models['supervisor'] = AlterOutput(models['supervisor'], dispatch)
        result = collaborate(models, FixtureProvider('case_003', P), P,
            limits=Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual([t['role'] for t in result['task_results'][:3]], ['investigation', 'investigation', 'knowledge'])
        self.assertTrue(any(e['kind'] == 'past_incident' for e in result['evidence']))
        second = json.loads(models['investigation'].histories[2][1].content)['objective']
        self.assertTrue(second['evidence'])
        self.assertTrue(second['collection'])
        self.assertLessEqual(sum(len(json.dumps(e, ensure_ascii=False)) for e in second['evidence']), 6000)
        self.assertTrue(any(e['error'] and e['error']['code'] == 'DUPLICATE_TOOL'
            for e in result['events'] if e['type'] == 'tool_result'))


if __name__ == '__main__':
    unittest.main()
