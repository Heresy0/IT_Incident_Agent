"""One real orchestration path; doubles assert control boundaries, not accuracy."""
import json
import unittest
from langchain_core.messages import AIMessage
from api.auth import Principal
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.v2.engine import investigate

P = Principal('team_a', 'alice')


class ClosedModel:
    def __init__(self, *, withdraw=False, malformed=False, duplicate=False, primary_pass=False):
        self.withdraw, self.malformed, self.duplicate = withdraw, malformed, duplicate
        self.messages, self.reviews, self.rework_steps = [], 0, 0
        self.primary_pass = primary_pass

    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        self.messages.append(request)
        stage, payload = request.get('stage'), request.get('input', request)
        if payload.get('challenge_policy_version'):
            value = {'items': [{'target_id': target['id'],
                'coverage': 'uncovered' if target['statement'] == '原范围过宽的命题' else 'covered',
                'uncovered_assertion': '来源之外的整体性断言', 'missing_evidence': '未观测路径',
                'refs': ['R1'], 'reason': '模型双固定质疑，仅测试接线'} for target in payload['targets']]}
        elif stage == 'investigate':
            if payload.get('review_feedback'):
                self.rework_steps += 1
                value = ({'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': 'dependency'}}
                         if self.duplicate or self.rework_steps == 1 else {'kind': 'finish'})
                if not self.duplicate and self.rework_steps == 1:
                    value['args']['category'] = 'resource'
            elif not payload['collection']:
                value = {'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': 'dependency'}}
            else:
                value = {'kind': 'finish'}
        elif stage == 'draft':
            value = {'claims': [{'statement': '原范围过宽的命题', 'refs': ['R1']}],
                     'next_steps': [{'action': '核对授权范围内请求记录', 'refs': ['R1']}], 'gaps': []}
        elif stage == 'revise':
            if self.malformed:
                return AIMessage(content='{broken')
            value = ({'withdraw': [{'target_id': 'C1', 'reason': '保持原因未知'}]} if self.withdraw else
                     {'items': [{'target_id': 'C1', 'revision': {'statement': '来源仅记录当前请求失败',
                        'refs': ['R1']}, 'change_kind': 'narrow', 'reason': '按实际来源收窄'}]})
        else:
            self.reviews += 1
            value = {'items': [{'target_id': target['id'], 'refs': ['R1'], 'issue': '固定注入的有限审阅',
                **({'decision': 'appropriate'} if target['id'].startswith('A') else
                   {'relation': 'partial' if self.reviews == 1 and not self.primary_pass else 'supported'})}
                for target in payload['targets']]}
        return AIMessage(content=json.dumps(value), response_metadata={'finish_reason': 'stop'})


class ClosedEngineTests(unittest.TestCase):
    def run_case(self, model=None, budget=12, **options):
        model = model or ClosedModel()
        result = investigate(model, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=budget, tool_calls=8), enable_rework=True,
            enable_challenge=False, **options)
        return result, model

    def test_feedback_reenters_same_executor_and_only_one_revision_round(self):
        result, model = self.run_case()
        self.assertEqual(result['rework_rounds'], 1)
        self.assertEqual(result['run_summary']['tool_calls'], 2)
        self.assertEqual(len(result['drafts']), 2)
        self.assertEqual(len(result['reviews']), 2)
        self.assertEqual(result['drafts'][0]['draft']['claims'][0]['id'], 'C1')
        self.assertNotEqual(result['drafts'][0]['draft']['claims'][0]['version'],
                            result['drafts'][1]['draft']['claims'][0]['version'])
        rework_messages = [m for m in model.messages if m.get('stage') == 'investigate'
                           and m['input'].get('review_feedback')]
        self.assertEqual(rework_messages[0]['input']['review_feedback']['allowed_target_ids'], ['C1'])
        self.assertTrue(result['output']['findings'])
        self.assertFalse(result['repair_eligible'])
        self.assertEqual(result['status'], 'completed')

    def test_low_budget_retains_source_and_unresolved_claim(self):
        result, _ = self.run_case(budget=4)
        self.assertLessEqual(result['run_summary']['model_calls'], 4)
        self.assertEqual(result['rework_rounds'], 0)
        self.assertTrue(result['output']['findings'])
        self.assertEqual(result['status'], 'partial')

    def test_duplicate_rework_read_is_not_executed_and_does_not_loop(self):
        result, model = self.run_case(ClosedModel(duplicate=True))
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertLessEqual(model.rework_steps, 3)
        self.assertEqual(result['rework_rounds'], 1)

    def test_bad_revision_preserves_previous_items_and_new_sources(self):
        result, _ = self.run_case(ClosedModel(malformed=True))
        self.assertEqual(result['output']['hypotheses'][0]['cause'], '原范围过宽的命题')
        self.assertTrue(any(f['stage'] == 'revision' for f in result['protocol_failures']))
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['evidence'])

    def test_withdrawal_is_an_explicit_gap_never_a_confirmed_cause(self):
        result, _ = self.run_case(ClosedModel(withdraw=True))
        self.assertEqual(result['output']['hypotheses'], [])
        self.assertIn('C1', result['reworks'][0]['withdrawn_ids'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any('撤回' in gap for gap in result['output']['missing_information']))

    def test_blind_challenge_can_create_objection_to_primary_pass_without_upgrading(self):
        model = ClosedModel(primary_pass=True)
        result = investigate(model, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=True)
        self.assertEqual(result['rework_rounds'], 1)
        self.assertIn('C1', result['challenges'][0]['disputed_ids'])
        self.assertEqual(result['output']['hypotheses'][0]['cause'], '来源仅记录当前请求失败')
        self.assertLessEqual(result['run_summary']['model_calls'], 12)
        self.assertFalse(result['semantic_review']['human_verified'])
        self.assertIn('审阅返工', result['final'])

    def test_no_challenge_budget_is_unassessed_instead_of_primary_pass(self):
        model = ClosedModel(primary_pass=True)
        result = investigate(model, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=6, tool_calls=8), enable_rework=True, enable_challenge=True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['rework_rounds'], 0)
        self.assertLessEqual(result['run_summary']['model_calls'], 6)


if __name__ == '__main__':
    unittest.main()
