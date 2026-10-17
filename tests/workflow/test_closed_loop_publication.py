"""Regression guards against re-sampling approval and publishing stale reviews."""
import json
import unittest
from langchain_core.messages import AIMessage
from tests.workflow.test_autonomous_closed_engine import ClosedModel, P
from tests.tools.test_incident_tools import executor, window
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.v2.contracts import parse_draft
from workflow.v2.engine import investigate, publish
from workflow.v2.review import review_draft


class NoProgressModel(ClosedModel):
    def invoke(self, messages):
        data = json.loads(messages[-1].content)
        if data.get('stage') == 'investigate' and data['input'].get('review_feedback'):
            return AIMessage(content='{"kind":"finish"}')
        if data.get('stage') == 'revise':
            return AIMessage(content='{}')
        return super().invoke(messages)


class SharedRepairModel(ClosedModel):
    def invoke(self, messages):
        data = json.loads(messages[-1].content)
        if data.get('stage') == 'investigate' and data['input'].get('review_feedback'):
            return AIMessage(content='{broken')
        if not data.get('stage') and data.get('targets'):
            self.reviews += 1
            return AIMessage(content=json.dumps({'items': [{'target_id': target['id'],
                'refs': ['R999'] if self.reviews == 1 else ['R1'], 'issue': '固定双检验共享纠正',
                **({'decision': 'appropriate'} if target['id'].startswith('A') else {
                    'relation': 'partial' if self.reviews <= 2 else 'supported'})}
                for target in data['targets']]}))
        return super().invoke(messages)


class PublicationTests(unittest.TestCase):
    def test_rewording_without_new_observation_cannot_erase_original_handoff_gap(self):
        result = investigate(ClosedModel(duplicate=True), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        self.assertEqual(result['reworks'][0]['new_observation_ids'], [])
        self.assertTrue(any('未新增观测' in gap for gap in result['output']['missing_information']))

    def test_no_new_read_or_changed_version_cannot_reroll_partial_into_pass(self):
        model = NoProgressModel()
        result = investigate(model, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reworks'][0]['reason'], 'NO_SEMANTIC_PROGRESS')
        self.assertEqual(model.reviews, 1)
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'unresolved')

    def test_one_protocol_correction_is_shared_with_rework_collection(self):
        result = investigate(SharedRepairModel(), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        self.assertEqual(result['repairs'], 1)
        self.assertLessEqual(result['run_summary']['model_calls'], 12)
        self.assertEqual(result['status'], 'partial')

    def test_publishing_old_batch_after_new_read_cannot_approve_new_pack(self):
        ex = executor(role='single')
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        draft, errors = parse_draft({'claims': [{'statement': '有限测试命题', 'refs': ['R1']}],
            'next_steps': [{'action': '取得授权后核对记录', 'refs': ['R1']}]}, ex)
        self.assertEqual(errors, [])
        batch = review_draft(ClosedModel(primary_pass=True), draft, ex, ex.context)
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        result = publish(draft, batch, ex, ex.context, [], 'FINISHED', [], ex.provider.ticket(), 0, {'repairs': 0})
        self.assertEqual(result['status'], 'partial')
        self.assertNotEqual(result['review_status'], 'passed')
        self.assertTrue(all(item['protocol_status'] != 'valid' for item in result['item_reviews']))


if __name__ == '__main__':
    unittest.main()
