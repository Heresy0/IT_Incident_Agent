"""Bounded evidence questions through the real graph, using free response replays."""
import json
import unittest

from agents.contracts import EvidenceNeed, NeedAssessment
from agents.scripted import scripted_models
from evidence.diagnosis import ProtocolError
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import Collaboration, collaborate
from workflow.evidence_needs import update_needs, validate_assessments
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_information_gain import action_models


class EvidenceNeedGraphTests(unittest.TestCase):
    def run_case(self, models, **kwargs):
        return collaborate(models, FixtureProvider('case_001', P), P, **kwargs)

    def test_local_tasks_and_all_claims_can_pass_while_omitted_causal_question_blocks_completion(self):
        models = scripted_models()
        def schedule(response, _, calls):
            value = json.loads(response.content)
            extra = {'need_id': 'N4', 'purpose': 'alternative',
                'question': '是否还有能改变依赖异常解释的其他原因？', 'status': 'unavailable',
                'reason': '尚无可核查的其他路径，保留关键歧义。'}
            value['evidence_needs'] = [n for n in value['evidence_needs'] if n['need_id'] != 'N4'] + [extra]
            return json_response(value)
        def review(response, *_):
            value = json.loads(response.content)
            next(a for a in value['need_assessments'] if a['need_id'] == 'N4').update(
                verdict='uncertain', reason='其他解释尚未区分；已登记能力不足以进一步核实。', evidence_ids=[])
            value['follow_up_reason'] = '没有能区分这个歧义的已登记只读检查。'
            return json_response(value)
        models['supervisor'] = AlterOutput(models['supervisor'], schedule)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        result = self.run_case(models)
        self.assertTrue(all(a['verdict'] == 'supported' for a in result['reviews'][0]['review']['assessments']))
        self.assertTrue(all(t['execution_status'] == 'completed' for t in result['task_results']))
        self.assertEqual((result['status'], result['review_status']), ('partial', 'needs_information'))
        self.assertIn('N4', '\n'.join(result['output']['missing_information']))
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertEqual(result['run_summary']['tool_calls'], 2)

    def test_metric_support_is_valid_without_a_fixed_log_path(self):
        models = scripted_models()
        # Remove the log read. All three questions are independently assessed by the replay.
        models['investigation'] = AlterOutput(models['investigation'], lambda response, *_:
            response.model_copy(update={'tool_calls': [c for c in response.tool_calls
                if c['name'] == 'get_service_metrics']}) if response.tool_calls else response)
        from tests.workflow.test_task_checks import MetricDiagnosis
        models['diagnosis'] = MetricDiagnosis()
        result = self.run_case(models)
        self.assertEqual((result['status'], result['review_status']), ('completed', 'passed'))
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertFalse(any(e['type'] == 'tool_result' and e['name'] == 'get_service_logs' for e in result['events']))
        self.assertEqual(len(result['evidence_needs']), 3)
        self.assertTrue({eid for need in result['evidence_needs'] for eid in need['evidence_ids']}
                        <= set(result['used_evidence_ids']))
        self.assertIn('共享证据需求', render(result))

    def test_unknown_trigger_and_human_precondition_remain_explicit_without_blocking_conditional_diagnosis(self):
        models = scripted_models()
        def schedule(response, *_):
            value = json.loads(response.content)
            for need_id, purpose in [('N4', 'trigger'), ('N5', 'action_condition')]:
                value['evidence_needs'] = [n for n in value['evidence_needs'] if n['need_id'] != need_id]
                value['evidence_needs'].append({'need_id': need_id, 'purpose': purpose,
                    'question': '执行处置前还需人工确认的条件是什么？', 'status': 'deferred',
                    'reason': '只读观测不足；人工确认后才可执行建议。'})
            return json_response(value)
        def review(response, *_):
            value = json.loads(response.content)
            for a in value['need_assessments']:
                if a['need_id'] in {'N4', 'N5'}:
                    a.update(verdict='uncertain', evidence_ids=[], reason='深层原因与操作条件仍须人工核实；建议有明确前置条件。')
            return json_response(value)
        models['supervisor'] = AlterOutput(models['supervisor'], schedule)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        result = self.run_case(models)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['evidence_needs'][-1]['status'], 'deferred')
        self.assertIn('N5', '\n'.join(result['output']['missing_information']))
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertTrue(all(a['requires_approval'] for a in result['output']['recommended_actions']))

    def test_missing_need_review_cannot_be_hidden_by_supported_claims_or_small_budget(self):
        models = scripted_models()
        def omit(response, *_):
            value = json.loads(response.content)
            value.pop('need_assessments')
            return json_response(value)
        models['reviewer'] = AlterOutput(models['reviewer'], omit)
        result = self.run_case(models, limits=Limits(model_calls=6, tool_calls=2,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(result['run_summary']['model_calls'], 6)
        self.assertTrue(all(a['verdict'] == 'uncertain' for a in result['reviews'][0]['review']['need_assessments']))
        self.assertFalse(any(e['type'] == 'review_follow_up_correction' for e in result['events']))

    def test_rework_reassesses_questions_and_preserves_link_without_extra_calls(self):
        provider = FixtureProvider('case_003', P)
        models = action_models(provider)
        def review(response, *_):
            value = json.loads(response.content)
            if value.get('request_evidence'):
                value['request_evidence']['need_id'] = 'N2'
                next(a for a in value['need_assessments'] if a['need_id'] == 'N2').update(
                    verdict='uncertain', reason='该候选机制还需要本次只读补查。')
            return json_response(value)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        result = collaborate(models, provider, P)
        self.assertEqual((result['status'], result['rework_rounds']), ('completed', 1))
        self.assertEqual(result['task_results'][-1]['need_ids'], ['N2'])
        self.assertEqual(result['run_summary']['model_calls'], 11)
        self.assertEqual(result['evidence_needs'][1]['status'], 'supported')
        self.assertFalse(any('证据需求N2' in gap for gap in result['output']['missing_information']))

    def test_status_check_has_no_invented_causal_requirements(self):
        provider = FixtureProvider('case_001', P)
        provider._data['ticket']['purpose'] = 'status_check'
        result = collaborate(scripted_models(), provider, P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([n['purpose'] for n in result['evidence_needs']], ['symptom'])
        self.assertFalse(result['output']['hypotheses'])


class EvidenceNeedBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.c = Collaboration(scripted_models(), FixtureProvider('case_003', P), P)
        ex = self.c.executors['investigation']
        ex.execute('get_service_metrics', {**self.c.ticket.scope.model_dump(mode='json', include={'start', 'end'}),
            'metrics': ['db_cpu']})
        self.c.evidence.update(ex.evidence)
        self.current = next(iter(self.c.evidence))

    def update(self, rows):
        return update_needs(self.c.evidence_needs, rows, self.c.evidence, self.c.executors, 'diagnosis')

    def test_questions_cannot_be_erased_retyped_or_downgraded_to_not_required(self):
        rows = list(self.c.evidence_needs.values())
        for bad in (rows[:1], [rows[0].model_copy(update={'purpose': 'trigger'}), *rows[1:]],
                    [rows[0].model_copy(update={'status': 'not_required'}), *rows[1:]]):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                self.update(bad)
        self.assertEqual(self.update([]), self.c.evidence_needs, 'Omission retains unresolved questions')

    def test_need_proposal_cannot_expand_permissions_or_execute_a_tool(self):
        rows = list(self.c.evidence_needs.values())
        before = dict(self.c.context.counts)
        bad = EvidenceNeed.model_validate({**rows[0].model_dump(), 'next_check': {
            'tool': 'get_service_metrics', 'args': {'start': '2020-01-01T00:00:00Z',
                'end': self.c.ticket.scope.end.isoformat(), 'metrics': ['db_cpu']}}})
        with self.assertRaises(ProtocolError):
            self.update([bad, *rows[1:]])
        self.assertEqual(self.c.context.counts, before)

    def test_unknown_references_and_history_cannot_supply_current_causal_support(self):
        rows = list(self.c.evidence_needs.values())
        with self.assertRaises(ProtocolError):
            self.update([rows[0].model_copy(update={'status': 'supported', 'evidence_ids': ['EV_missing']}), *rows[1:]])
        historical = self.c.executors['knowledge'].execute('search_incidents', {'symptoms': 'latency database pool'}).evidence[0]
        self.c.evidence[historical.evidence_id] = historical
        with self.assertRaises(ProtocolError):
            self.update([rows[0].model_copy(update={'status': 'supported',
                'evidence_ids': [historical.evidence_id]}), *rows[1:]])
        review = validate_assessments(self.c.evidence_needs, [NeedAssessment(need_id='N2',
            verdict='supported', reason='历史线索不足以证明本次机制', evidence_ids=[historical.evidence_id])], self.c.evidence)
        self.assertTrue(all(a.verdict == 'uncertain' for a in review))


if __name__ == '__main__':
    unittest.main()
