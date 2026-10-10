"""Free protocol replays through the graph, not diagnosis-quality scores."""
import json
import unittest

from agents.scripted import scripted_models
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import collaborate
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


def break_core(response, messages, calls):
    value = json.loads(response.content)
    if calls >= 2:
        value['evidence_needs'][0].update(status='not_required', blocking=False)
        value['evidence_needs'][1].update(blocking=False)
    return json_response(value)


class SupervisorResilienceTests(unittest.TestCase):
    def run_case(self, schedule, *, review=None, limits=None):
        models = scripted_models()
        models['supervisor'] = AlterOutput(models['supervisor'], schedule)
        if review:
            models['reviewer'] = AlterOutput(models['reviewer'], review)
        return collaborate(models, FixtureProvider('case_001', P), P, **({'limits': limits} if limits else {})), models

    def test_small_updates_preserve_server_questions_and_other_needs(self):
        def schedule(response, messages, calls):
            value = json.loads(response.content)
            schema = json.loads(messages[1].content)['output_schema']
            self.assertNotIn('evidence_needs', schema['properties'])
            update_schema = schema['$defs']['EvidenceNeedUpdate']['properties']
            self.assertNotIn('purpose', update_schema)
            self.assertNotIn('question', update_schema)
            ledger = value.pop('evidence_needs')
            value['need_updates'] = [{k: v for k, v in ledger[0].items()
                if k in update_schema}]
            value['need_updates'][0]['blocking'] = False
            return json_response(value)
        result, _ = self.run_case(schedule)
        self.assertEqual((result['status'], result['repairs']), ('completed', 0))
        self.assertEqual([n['need_id'] for n in result['evidence_needs']], ['N1', 'N2', 'N3'])
        self.assertTrue(all(n['purpose'] in {'symptom', 'mechanism', 'alternative'} for n in result['evidence_needs']))
        self.assertTrue(any(e.get('code') == 'CORE_BLOCKING_RETAINED' for e in result['events']))

    def test_first_core_failure_then_redundant_tasks_uses_only_one_correction(self):
        def schedule(response, messages, calls):
            value = json.loads(response.content)
            if calls == 2:
                return break_core(response, messages, calls)
            if calls == 3:
                correction = json.loads(messages[-1].content.split('纠正一次。', 1)[1].split('。仅返回', 1)[0])
                self.assertEqual(len(correction['details']), 3)
                self.assertEqual({d['need_id'] for d in correction['details']}, {'N1', 'N2'})
                value['tasks'] = [{'role': 'administrator', 'tenant_id': 'other'}]
            return json_response(value)
        result, _ = self.run_case(schedule)
        self.assertEqual((result['status'], result['repairs']), ('completed', 1))
        self.assertFalse(result['protocol_finalization_used'])
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (7, 2))
        self.assertEqual(len(result['task_results']), 1)

    def test_exhausted_protocol_correction_routes_to_diagnosis_and_independent_review_once(self):
        result, models = self.run_case(break_core)
        self.assertTrue(result['protocol_finalization_used'])
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['repairs'], 1)
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (7, 2))
        self.assertEqual(len(models['diagnosis'].histories), 1)
        self.assertEqual(len(models['reviewer'].histories), 1)
        self.assertEqual(len([e for e in result['events'] if e['type'] == 'protocol_finalization_started']), 1)
        self.assertTrue(any(e['code'] == 'MODEL_OUTPUT_INVALID' for e in result['run_summary']['errors']))

    def test_fallback_does_not_promote_unknown_mechanism_or_allow_pending_repairs(self):
        def uncertain(response, *_):
            value = json.loads(response.content)
            for assessment in value['assessments']:
                if assessment['target_id'].startswith(('H', 'A')):
                    assessment.update(verdict='uncertain', reason='因果与操作条件仍缺证据。')
            next(a for a in value['need_assessments'] if a['need_id'] == 'N2').update(
                verdict='uncertain', reason='关键机制仍待核查。')
            value['follow_up_reason'] = '当前渠道不足以区分机制。'
            return json_response(value)
        result, _ = self.run_case(break_core, review=uncertain)
        self.assertEqual((result['status'], result['review_status']), ('partial', 'needs_information'))
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'unresolved')
        self.assertFalse(result['output']['recommended_actions'])
        self.assertTrue(all(a['executable'] is False and a.get('repair') is None for a in result['pending_actions']))

    def test_insufficient_finalization_budget_keeps_validated_facts_without_extra_calls(self):
        result, models = self.run_case(break_core, limits=Limits(model_calls=6, tool_calls=2,
            reserve_model_calls=1, reserve_seconds=20))
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertFalse(result['protocol_finalization_used'])
        self.assertEqual(result['run_summary']['model_calls'], 5)
        self.assertFalse(models['diagnosis'].histories)
        self.assertFalse(result['output']['findings'])
        self.assertTrue(result['unreviewed_observations'])
        self.assertIn('尚未获独立语义复核', render(result))
        self.assertFalse(result['output']['recommended_actions'])

    def test_unsafe_refs_or_scopes_remain_fatal_even_when_core_flags_are_wrong(self):
        for variant in ('reference', 'scope'):
            def schedule(response, messages, calls):
                value = json.loads(break_core(response, messages, calls).content)
                if calls >= 2:
                    if variant == 'reference':
                        value['evidence_needs'][0]['evidence_ids'] = ['EV_missing']
                    else:
                        scope = json.loads(messages[1].content)['input']['ticket']['scope']
                        value.update(action='dispatch', tasks=[{'role': 'investigation', 'goal': '越界查询',
                            'checks': [{'tool': 'get_service_metrics', 'args': {
                                'start': '2020-01-01T00:00:00Z', 'end': scope['end'],
                                'metrics': ['dependency_error_rate']}}]}])
                return json_response(value)
            with self.subTest(variant=variant):
                result, models = self.run_case(schedule)
                self.assertFalse(result['protocol_finalization_used'])
                self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
                self.assertEqual(result['run_summary']['tool_calls'], 2)
                self.assertFalse(models['diagnosis'].histories)
                self.assertTrue(result['unreviewed_observations'])

    def test_no_observations_cannot_trigger_protocol_fallback(self):
        result, models = self.run_case(lambda *_: json_response({'action': 'dispatch', 'reason': '没有有效任务'}))
        self.assertFalse(result['protocol_finalization_used'])
        self.assertEqual((result['repairs'], result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (1, 2, 0))
        self.assertFalse(models['diagnosis'].histories)

    def test_fallback_review_request_stays_pending_and_never_returns_to_broken_scheduler(self):
        def request(response, messages, calls):
            payload = json.loads(messages[1].content)['input']
            value = json.loads(response.content)
            next(a for a in value['assessments'] if a['target_id'] == 'H1').update(
                verdict='uncertain', reason='需要新的变更观测。')
            next(a for a in value['need_assessments'] if a['need_id'] == 'N2').update(
                verdict='uncertain', reason='关键机制待补查。')
            value['request_evidence'] = {'hypothesis_id': 'H1', 'need_id': 'N2',
                'evidence_ids': [payload['evidence'][0]['evidence_id']],
                'missing_observation': '需要确认近期变更', 'proposed_check': '查询登记的近期变更',
                'expected_value': '变更可帮助区分原因', 'checks': [{'tool': 'get_recent_changes',
                    'args': {k: payload['check_scope'][k] for k in ('start', 'end')}}]}
            return json_response(value)
        result, models = self.run_case(break_core, review=request)
        self.assertEqual((result['status'], result['run_summary']['termination_reason']), ('partial', 'REVIEW_INCOMPLETE'))
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (7, 2))
        self.assertEqual(len(models['supervisor'].model.histories), 3)
        self.assertEqual(result['rework_rounds'], 0)
        self.assertTrue(result['reviews'][0]['review']['request_evidence'])
        self.assertTrue(any(e.get('code') == 'PROTOCOL_FINALIZATION_ONLY' for e in result['events']))

    def test_new_questions_start_pending_and_next_check_is_only_validated(self):
        def schedule(response, messages, calls):
            value = json.loads(response.content)
            ledger = value.pop('evidence_needs')
            if calls == 1:
                scope = json.loads(messages[1].content)['input']['ticket']['scope']
                value['additional_needs'] = [{'need_id': 'N4', 'purpose': 'trigger',
                    'question': '为何开始发生？', 'reason': '触发原因尚未确认。'}]
                value['need_updates'] = [{'need_id': 'N1', 'status': 'pending',
                    'reason': '先按登记范围核查观测。', 'next_check': {'tool': 'get_recent_changes',
                        'args': {k: scope[k] for k in ('start', 'end')}}}]
            else:
                value['need_updates'] = [{k: v for k, v in row.items()
                    if k not in {'purpose', 'question'}} for row in ledger]
            return json_response(value)
        result, models = self.run_case(schedule)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertFalse(any(e['type'] == 'tool_result' and e['name'] == 'get_recent_changes' for e in result['events']))
        payload = json.loads(models['supervisor'].model.histories[1][1].content)['input']
        self.assertEqual(payload['evidence_needs'][-1]['status'], 'pending')
        self.assertEqual(payload['evidence_needs'][-1]['purpose'], 'trigger')


if __name__ == '__main__':
    unittest.main()
