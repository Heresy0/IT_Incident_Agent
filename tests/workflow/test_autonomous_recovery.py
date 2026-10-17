"""Integration of real v2 graph recovery; scripted judgments prove no accuracy."""
import json
import unittest

from langchain_core.messages import AIMessage

from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.tools.test_incident_tools import P, executor, window
from workflow.v2.contracts import parse_draft
from workflow.v2.engine import investigate, publish
from workflow.v2.review import review_draft


class RecoveryModel:
    """A stage-aware transport stub, not a second investigation implementation."""
    def __init__(self, *, bad_draft=False, bad_step=False, second_relation='supported'):
        self.bad_draft, self.bad_step = bad_draft, bad_step
        self.second_relation = second_relation
        self.histories, self.investigations = [], 0

    def invoke(self, messages):
        body = json.loads(messages[-1].content)
        self.histories.append(body)
        stage = body.get('stage', 'review')
        if stage == 'investigate':
            self.investigations += 1
            if self.bad_step and self.investigations == 1:
                return AIMessage(content='{broken')
            first_read = 2 if self.bad_step else 1
            if self.investigations == first_read:
                value = {'kind': 'query', 'question': '取得当前登记指标的实际观测',
                         'tool': 'get_service_metrics', 'args': {'metrics': ['db_cpu']}}
            else:
                value = {'kind': 'finish', 'question': '已有有限来源，交给诊断'}
        elif stage == 'draft':
            if body['input'].get('correction_targets') is not None:
                value = {'claims': [{'statement': '修正后的第二命题', 'refs': ['R1'],
                                     'level': 'alternative'}], 'next_steps': [], 'gaps': []}
            else:
                value = {'claims': [
                    {'statement': '原有效机制命题', 'refs': ['R1'], 'level': 'mechanism'},
                    {'statement': '原第二命题', 'refs': ['R999'] if self.bad_draft else ['R1'],
                     'level': 'alternative'}],
                    'next_steps': [{'action': '人工核对依赖侧请求记录', 'condition': '取得访问授权后',
                                    'refs': ['R1']}], 'gaps': []}
        else:
            value = {'items': [{'target_id': target['id'],
                **({'decision': 'appropriate'} if target['id'].startswith('A') else {
                    'relation': self.second_relation if target['id'] == 'C2' else 'supported'}),
                'refs': ['R1'], 'issue': '测试注入的条目判断，仅验证状态接线。'}
                for target in body['targets']]}
        return AIMessage(content=json.dumps(value))


def run(model, *, model_calls=5):
    return investigate(model, FixtureProvider('case_001', P), P,
                       limits=Limits(model_calls=model_calls, tool_calls=2))


class AutonomousRecoveryTests(unittest.TestCase):
    def test_draft_repairs_only_failed_c2_and_preserves_original_c1_and_action(self):
        model = RecoveryModel(bad_draft=True)
        result = run(model)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['review_status'], 'passed')
        self.assertFalse(result['repair_eligible'])
        hypotheses = result['output']['hypotheses']
        self.assertEqual([item['hypothesis_id'] for item in hypotheses], ['C1', 'C2'])
        self.assertEqual([item['cause'] for item in hypotheses], ['原有效机制命题', '修正后的第二命题'])
        self.assertEqual(len(result['output']['recommended_actions']), 1)
        drafts = [body for body in model.histories if body.get('stage') == 'draft']
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[1]['input']['correction_targets'], {'claims': ['C2'], 'next_steps': []})
        self.assertEqual(drafts[1]['output_schema']['properties']['claims']['maxItems'], 1)
        self.assertEqual(drafts[1]['output_schema']['properties']['next_steps']['maxItems'], 0)
        self.assertEqual(result['run_summary']['model_calls'], 5)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertEqual(result['repairs'], 1)
        self.assertTrue(result['output']['findings'])
        self.assertEqual(len({item['hypothesis_id'] for item in hypotheses}), len(hypotheses))

    def test_successful_draft_correction_retains_history_without_unresolved_failure(self):
        result = run(RecoveryModel(bad_draft=True))
        failures = result['protocol_failures']
        self.assertTrue(failures)
        self.assertTrue(all(failure.get('recovered') for failure in failures))
        self.assertTrue(any(failure['stage'] == 'draft' and failure['category'] == 'reference'
                            and failure['target_id'] == 'C2' for failure in failures))
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(any('UNKNOWN_REFERENCE' in gap for gap in result['output']['missing_information']))

    def test_successful_step_correction_retains_syntax_history_and_can_complete(self):
        result = run(RecoveryModel(bad_step=True))
        self.assertEqual(result['status'], 'completed')
        failure = next(failure for failure in result['protocol_failures'] if failure['stage'] == 'investigate')
        self.assertEqual(failure['category'], 'syntax')
        self.assertTrue(failure['recovered'])
        self.assertEqual(result['run_summary']['model_calls'], 5)
        self.assertEqual(result['repairs'], 1)

    def test_supported_mechanism_and_contradicted_alternative_can_complete(self):
        result = run(RecoveryModel(second_relation='contradicted'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([item['status'] for item in result['output']['hypotheses']], ['supported', 'refuted'])
        self.assertEqual(result['review_status'], 'passed')

    def test_partial_or_insufficient_item_never_promotes_whole_report(self):
        for relation in ('partial', 'insufficient'):
            with self.subTest(relation=relation):
                result = run(RecoveryModel(second_relation=relation))
                self.assertEqual(result['status'], 'partial')
                self.assertNotEqual(result['review_status'], 'passed')
                self.assertEqual(result['output']['hypotheses'][1]['status'], 'unresolved')
                self.assertTrue(result['output']['findings'])

    def test_saved_review_cannot_approve_same_id_with_new_version_or_invalid_key(self):
        ex = executor(role='single')
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        original, errors = parse_draft({'claims': [{'statement': '原机制', 'refs': ['R1']}],
            'next_steps': [{'action': '核对记录', 'refs': ['R1']}]}, ex)
        self.assertEqual(errors, [])
        batch = review_draft(RecoveryModel(), original, ex, ex.context)
        changed, errors = parse_draft({'claims': [{'statement': '已经修改的新机制', 'refs': ['R1']}],
            'next_steps': [{'action': '核对记录', 'refs': ['R1']}]}, ex)
        self.assertEqual(errors, [])
        for name, candidate, review in [
            ('new-version', changed, batch),
            ('bad-key', original, batch.model_copy(update={'items': [
                item.model_copy(update={'review_key': 'bad-key'}) if item.target_id == 'C1' else item
                for item in batch.items]})),
            ('new-evidence-pack', original, batch.model_copy(update={'evidence_pack_version': 'new-pack'})),
        ]:
            with self.subTest(name=name):
                result = publish(candidate, review, ex, ex.context, [], 'FINISHED', [],
                                 ex.provider.ticket(), 0, {'repairs': 0})
                self.assertEqual(result['status'], 'partial')
                self.assertNotEqual(result['review_status'], 'passed')
                self.assertEqual(result['output']['hypotheses'][0]['status'], 'tentative')
                self.assertTrue(result['output']['findings'])
                self.assertFalse(result['repair_eligible'])


if __name__ == '__main__':
    unittest.main()
