"""Free replays of failure classes; saved live traces contain no raw rejected JSON.

These control models exercise the real graph, executor and gates, not LLM quality.
No independent answer/rubric files are read or changed.
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from agents.contracts import DiagnosisSelection, SupervisorSelection
from agents.investigation import investigate
from agents.scripted import scripted_models, selector
from evidence.contracts import InvestigationSelection
from evidence.ownership import owner_schema, owner_options
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import collaborate
from workflow.task_checks import completion
from tests.tools.test_incident_tools import P, executor, window
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_information_gain import action_models, FocusedLeaf
from tests.agents.test_incident_investigation import Responses, selected


class ProtocolProgressTests(unittest.TestCase):
    def test_owner_domain_is_separate_from_fact_domain_in_all_selectors(self):
        ex = executor('case_003')
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        for schema in (InvestigationSelection, DiagnosisSelection, SupervisorSelection):
            self.assertEqual(owner_schema(schema, ex)['properties']['escalation_ref']['type'], 'null')
        ex.execute('get_service_owner', {'alias': 'checkout-api'})
        choices = owner_options(ex)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['team'], 'Checkout on-call')
        for schema in (InvestigationSelection, DiagnosisSelection, SupervisorSelection):
            shaped = owner_schema(schema, ex)
            self.assertEqual(shaped['$defs']['OwnerReferenceSelection']['properties']['reference_id']['enum'],
                             [choices[0]['reference_id']])
            if schema is not SupervisorSelection:
                self.assertNotIn('enum', shaped['$defs']['ReferenceSelection']['properties']['reference_id'])

    def test_owner_options_refresh_after_read_and_correct_non_owner_reference(self):
        class OwnerModel:
            def __init__(self): self.histories = []
            def bind_tools(self, tools): return self
            def invoke(self, messages):
                self.histories.append(list(messages))
                data = json.loads(messages[1].content)
                if len(self.histories) == 1:
                    scope = data['ticket']['scope']
                    return AIMessage(content='', tool_calls=[
                        {'name': 'get_service_metrics', 'args': {k: scope[k] for k in ('start', 'end')}
                         | {'metrics': ['db_cpu']}, 'id': 'metric', 'type': 'tool_call'},
                        {'name': 'get_service_owner', 'args': {'alias': 'checkout-api'},
                         'id': 'owner', 'type': 'tool_call'}])
                sources = [e for m in messages if m.type == 'tool' for e in json.loads(m.content)['evidence']]
                cpu = next(e for e in sources if 'metric' in e['payload'])
                selected = selector(cpu, 'value') if len(self.histories) == 2 else {
                    'reference_id': data['owner_options'][0]['reference_id']}
                return json_response({'findings': [{'statement': '来源测量', 'refs': [selector(cpu, 'value')]}],
                    'escalation_ref': selected})
        model = OwnerModel()
        result = investigate(model, FixtureProvider('case_003', P), P)
        self.assertEqual(result['run_summary']['termination_reason'], 'FINISHED')
        self.assertEqual((result['repairs'], result['run_summary']['model_calls'],
                          result['run_summary']['tool_calls']), (1, 3, 2))
        self.assertEqual(result['output']['escalation_team'], 'Checkout on-call')
        self.assertEqual(json.loads(model.histories[0][1].content)['owner_options'], [])
        self.assertEqual(len(json.loads(model.histories[1][1].content)['owner_options']), 1)
        self.assertIn('non_owner_reference', model.histories[2][-1].content)
        self.assertIn('owner_options', model.histories[2][-1].content)

    def test_empty_event_read_is_complete_but_empty_metric_remains_a_gap(self):
        ex = executor('case_001')
        for tool, args in [('get_service_logs', {**window(ex), 'category': 'dependency'}),
                           ('get_service_metrics', {**window(ex), 'metrics': ['dependency_error_rate']})]:
            with patch.object(ex.provider, 'query', return_value=([], False)):
                ex.execute(tool, args)
            coverage = completion([{'tool': tool, 'args': ex.validate_args(tool, args).model_dump(mode='json')}], ex)
            self.assertTrue(coverage['query_complete'])
            self.assertFalse(coverage['goal_verified'])
            self.assertTrue(coverage['limitations'])
            self.assertEqual(bool(coverage['missing_information']), tool == 'get_service_metrics')

    def test_empty_query_claim_still_needs_correction_and_never_becomes_a_fact(self):
        provider = FixtureProvider('case_001', P)
        args = {**provider.ticket().scope.model_dump(mode='json', include={'start', 'end'}),
                'category': 'dependency', 'error_code': 'ABSENT'}
        model = Responses([selected('get_service_logs', args),
            json_response({'findings': [{'statement': '所选查询没有记录', 'refs': []}]}),
            json_response({'findings': [], 'missing_information': ['所选窗口及过滤条件无匹配记录']})])
        result = investigate(model, provider, P, objective={'required_checks': [
            {'tool': 'get_service_logs', 'args': args}]})
        self.assertEqual((result['status'], result['repairs']), ('completed', 1))
        self.assertEqual(result['output']['findings'], [])
        self.assertTrue(result['output']['missing_information'])
        self.assertIn('findings=[]', model.history[-1][-1].content)
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (3, 1))

    def test_empty_second_batch_returns_to_supervisor_for_a_new_channel(self):
        provider = FixtureProvider('case_001', P)
        scope = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        checks = [
            {'tool': 'get_service_metrics', 'args': {**scope, 'metrics': ['dependency_error_rate']}},
            {'tool': 'get_recent_changes', 'args': scope},
            {'tool': 'get_service_logs', 'args': {**scope, 'category': 'dependency'}}]
        models = scripted_models()
        def schedule(response, messages, calls):
            if calls == 4:
                return json_response({'action': 'diagnose', 'reason': '交接当前证据及查询限制'})
            if calls == 3:
                prior = json.loads(messages[1].content)['input']['tasks'][-1]
                self.assertEqual(prior['completion']['status'], 'checks_completed_empty')
                self.assertTrue(prior['completion']['limitations'])
                self.assertEqual(prior['execution_status'], 'completed')
            return json_response({'action': 'dispatch', 'reason': '选择能够区分机制的新检查',
                'tasks': [{'role': 'investigation', 'goal': f'聚焦检查{calls}', 'checks': [checks[calls - 1]]}]})
        models['supervisor'] = AlterOutput(models['supervisor'], schedule)
        models['investigation'] = FocusedLeaf()
        original = provider.query
        def read(tool, *args, **kwargs):
            return ([], False) if tool == 'get_recent_changes' else original(tool, *args, **kwargs)
        with patch.object(provider, 'query', side_effect=read):
            result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual((result['supervisor_decisions'], result['run_summary']['tool_calls']), (4, 3))
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(result['task_results'][1]['output']['findings'], [])
        self.assertFalse(any('任务目标未满足' in gap for gap in result['output']['missing_information']))
        self.assertIn('查询限制', render(result))
        self.assertTrue(result['output']['hypotheses'])

    def test_valid_rework_reopens_conflicting_need_without_using_structure_repair(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider, invalid='A1')
        def conflicting(response, messages, calls):
            data = json.loads(response.content)
            if data.get('request_evidence'):
                data['request_evidence'].update(need_id='N2', target_id='H1')
                for assessment in data['assessments']:
                    if assessment['target_id'] == 'H1': assessment['verdict'] = 'uncertain'
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], conflicting)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual((result['rework_rounds'], result['repairs']), (1, 0))
        first = result['reviews'][0]['review']
        self.assertEqual(next(a for a in first['need_assessments'] if a['need_id'] == 'N2')['verdict'], 'uncertain')
        events = [e for e in result['events'] if e['type'] == 'review_need_reopened']
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]['previous_verdict'], events[0]['verdict']), ('supported', 'uncertain'))
        self.assertEqual(result['evidence_needs'][1]['status'], 'supported', 'Only the second review closes the need')
        self.assertEqual(result['run_summary']['tool_calls'], 3)

    def test_denied_duplicate_request_gets_one_normal_budget_alternative(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider, invalid='A1')
        def duplicate(response, messages, calls):
            data = json.loads(response.content)
            if calls == 1:
                data['request_evidence']['checks'][0]['args']['metrics'] = ['db_cpu']
            if calls == 2:
                self.assertIn('DUPLICATE_TOOL', messages[-1].content)
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], duplicate)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual((result['rework_rounds'], result['repairs']), (1, 0))
        self.assertEqual(sum(e['type'] == 'review_follow_up_correction' for e in result['events']), 1)
        self.assertEqual(sum(e['type'] == 'rework_denied' for e in result['events']), 1)
        self.assertEqual(result['run_summary']['tool_calls'], 3)

    def test_invalid_scope_cannot_reopen_need_or_execute_rework(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider, invalid='A1')
        def invalid(response, messages, calls):
            data = json.loads(response.content)
            if data.get('request_evidence'):
                data['request_evidence']['need_id'] = 'N2'
                data['request_evidence']['checks'][0]['args']['user_id'] = 'forged'
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], invalid)
        result = collaborate(models, provider, P, limits=Limits(model_calls=8, tool_calls=5,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual((result['status'], result['rework_rounds']), ('partial', 0))
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertFalse(any(e['type'] == 'review_need_reopened' for e in result['events']))
        self.assertTrue(any(e['type'] == 'rework_denied' for e in result['events']))


if __name__ == '__main__':
    unittest.main()
