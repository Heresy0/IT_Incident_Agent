"""Regression for facts-only live diagnoses; all models and observations are local substitutes."""
import json
import unittest

from langchain_core.messages import AIMessage, ToolMessage
from agents.contracts import DiagnosisDraft, ReviewDecision
from agents.scripted import scripted_models, selector
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import activate
from workflow.coordinator import Collaboration, collaborate
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class WorkerObservationModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        data = json.loads(messages[1].content)
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        if not results:
            window = {key: data['ticket']['scope'][key] for key in ('start', 'end')}
            checks = [('get_service_metrics', {**window, 'metrics': ['workers_active', 'queue_depth']}),
                      ('get_service_logs', {**window, 'category': 'resource'})]
            return AIMessage(content='', tool_calls=[{'name': name, 'args': args,
                'id': f'worker-check-{n}', 'type': 'tool_call'} for n, (name, args) in enumerate(checks)])
        evidence = [item for result in results for item in result['evidence']]
        return json_response({'findings': [{'statement': '故障演练观测',
            'refs': [selector(item, 'value' if 'metric' in item['payload'] else 'message')]}
            for item in evidence]})


class WorkerDiagnosisModel:
    def __init__(self, facts_only):
        self.facts_only, self.history = facts_only, []

    def invoke(self, messages):
        self.history.append(messages)
        data = json.loads(messages[1].content)
        evidence = data['input']['evidence']
        facts = [{'statement': '索引任务排查观测',
            'refs': [selector(item, 'value' if 'metric' in item['payload'] else 'message')]}
            for item in evidence]
        if self.facts_only:
            return json_response({'findings': facts})
        # Include both endpoint samples and the stop event to support the time sequence.
        refs = [selector(item, 'value' if 'metric' in item['payload'] else 'message') for item in evidence]
        return json_response({'findings': facts, 'hypotheses': [{
            'cause': 'Worker 停止后缺少活跃消费者，索引任务留在队列中。',
            'support_refs': refs, 'pending_checks': ['Worker 停止的触发原因尚未核实。']}],
            'recommended_actions': [{'action': '人工核实后恢复索引 Worker。',
                'condition': '确认停止不是计划维护，启动条件已满足且没有重复消费者。',
                'expected_result': '检查新鲜 workers_active、queue_depth 指标和测试任务是否完成。',
                'risk': '恢复消费会重新处理待执行任务，需关注重试和重复处理。',
                'requires_approval': True}], 'missing_information': ['Worker 停止的触发原因尚未核实。']})


def worker_provider():
    provider = FixtureProvider('case_001', P)
    ticket = provider._data['ticket']
    ticket.update(title='索引任务未执行', symptoms='测试任务持续 queued', purpose='diagnosis')
    ticket['scope'].update(service='enterprise-indexing', service_version='local-v1',
        start='2026-10-09T13:32:34+00:00', end='2026-10-09T14:32:34+00:00', allowed_dependencies=[])
    common = {'service': 'enterprise-indexing', 'environment': 'staging',
              'data_version': 'local-v1', 'visibility': 'public_synthetic'}
    provider._data['metrics'] = [{**common, 'id': f'{metric}-{time}', 'timestamp': time,
        'metric': metric, 'value': value, 'unit': unit, 'aggregation': 'sum', 'granularity': '1m'}
        for metric, unit, before, after in [('workers_active', 'workers', 1., 0.), ('queue_depth', 'jobs', 0., 1.)]
        for time, value in [('2026-10-09T13:32:34+00:00', before), ('2026-10-09T14:32:34+00:00', after)]]
    provider._data['logs'] = [{**common, 'id': 'worker-stopped', 'timestamp': '2026-10-09T14:26:17+00:00',
        'category': 'resource', 'level': 'UNKNOWN', 'error_code': None, 'message': 'event=indexing.worker.stopped'}]
    for key in ('owners', 'changes', 'runbooks', 'incidents'):
        provider._data[key] = []
    return provider


class DiagnosisCompletionTests(unittest.TestCase):
    def run_incomplete(self, remove, gaps=None):
        def omit(response, *_):
            value = json.loads(response.content)
            for field in remove:
                value[field] = []
            if gaps is not None:
                value['missing_information'] = gaps
            return json_response(value)
        models = scripted_models()
        models['diagnosis'] = AlterOutput(models['diagnosis'], omit)
        return collaborate(models, FixtureProvider('case_001', P), P), models

    def assert_incomplete(self, result):
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['review_status'], 'needs_information')
        self.assertEqual(result['business_result'], 'needs_information')
        self.assertEqual(result['run_summary']['termination_reason'], 'DIAGNOSIS_INCOMPLETE')
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(result['rework_rounds'], 0)
        self.assertTrue(any(e['type'] == 'diagnosis_gate' for e in result['events']))
        self.assertEqual(next(e for e in result['events'] if e['type'] == 'review_completed')['status'], 'needs_information')

    def test_facts_only_cannot_pass_even_when_reviewer_supports_every_fact(self):
        result, models = self.run_incomplete(['hypotheses', 'recommended_actions'])
        self.assert_incomplete(result)
        self.assertTrue(result['output']['findings'])
        self.assertTrue(all(a['verdict'] == 'supported' for a in result['reviews'][0]['review']['assessments']))
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertIn('尚未形成原因假设', render(result))
        self.assertIn('尚无经过复核的处理建议', render(result))
        payload = json.loads(models['reviewer'].histories[0][1].content)['input']
        self.assertEqual(len(payload['diagnosis_completion_gaps']), 2)

    def test_missing_cause_cannot_be_replaced_by_an_action(self):
        result, _ = self.run_incomplete(['hypotheses'])
        self.assert_incomplete(result)
        self.assertTrue(result['output']['recommended_actions'])

    def test_missing_action_cannot_be_replaced_by_a_supported_cause(self):
        result, _ = self.run_incomplete(['recommended_actions'])
        self.assert_incomplete(result)
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'supported')

    def test_missing_facts_cannot_pass_with_only_a_cause_and_action(self):
        result, _ = self.run_incomplete(['findings'])
        self.assert_incomplete(result)
        self.assertIn('故障诊断缺少可复核的当前观测。', result['output']['missing_information'])

    def test_explicit_information_gap_keeps_facts_without_forcing_a_model_retry(self):
        result, _ = self.run_incomplete(['hypotheses', 'recommended_actions'], ['尚缺任务失败日志。'])
        self.assert_incomplete(result)
        self.assertIn('尚缺任务失败日志。', result['output']['missing_information'])
        self.assertEqual(result['run_summary']['model_calls'], 6)

    def test_supervisor_finish_cannot_bypass_content_gate(self):
        result, models = self.run_incomplete(['hypotheses', 'recommended_actions'])
        c = Collaboration(models, FixtureProvider('case_001', P), P)
        c.draft = DiagnosisDraft.model_validate_json(json.dumps(result['drafts'][0]['draft']))
        c.review = ReviewDecision.model_validate_json(json.dumps(result['reviews'][0]['review']))
        c.models['supervisor'] = AlterOutput(models['supervisor'], lambda *_:
            json_response({'action': 'finish', 'reason': '已有观测全部通过'}))
        with activate(c.context):
            c.supervise()
        self.assertEqual(c.stop, 'DIAGNOSIS_INCOMPLETE')
        self.assertNotEqual(c.status(), 'completed')

    def run_worker(self, facts_only):
        models = scripted_models()
        models['investigation'] = WorkerObservationModel()
        diagnosis = models['diagnosis'] = WorkerDiagnosisModel(facts_only)
        return collaborate(models, worker_provider(), P), diagnosis

    def test_worker_stop_evidence_does_not_make_facts_only_a_complete_diagnosis(self):
        result, _ = self.run_worker(True)
        self.assert_incomplete(result)
        self.assertEqual(len(result['evidence']), 5)
        self.assertEqual(result['run_summary']['tool_calls'], 2)

    def test_worker_mechanism_and_manual_advice_can_pass_with_trigger_still_unknown(self):
        result, diagnosis = self.run_worker(False)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['review_status'], 'passed')
        self.assertEqual(result['business_result'], 'diagnosis_available')
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'supported')
        self.assertIn('停止的触发原因尚未核实', result['output']['missing_information'][0])
        self.assertIsNone(result['output']['recommended_actions'][0]['repair'])
        self.assertTrue(result['output']['recommended_actions'][0]['requires_approval'])
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        payload = json.loads(diagnosis.history[0][1].content)['input']
        self.assertEqual(payload['completion_requirements']['required_sections'],
                         ['findings', 'hypotheses', 'recommended_actions'])
        self.assertIn('purpose=diagnosis', diagnosis.history[0][0].content)
