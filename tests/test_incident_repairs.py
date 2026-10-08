"""Free regression for the observed M5 failures; no live credentials or gold reads."""
import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mult_agents.harness.runtime import Limits
from mult_agents.incident.collaboration import Collaboration, collaborate
from mult_agents.incident.collaboration_contracts import DiagnosisSelection, ReviewDecision
from mult_agents.incident.collaboration_fake import scripted_models
from mult_agents.incident.diagnosis import expand_diagnosis
from mult_agents.incident.investigation import resolve_output, validate_output
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


class IncidentRepairTests(unittest.TestCase):
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
