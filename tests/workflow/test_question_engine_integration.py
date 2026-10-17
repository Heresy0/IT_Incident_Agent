"""Questions enter existing calls; item revisions cannot declare ticket resolution."""
import unittest
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.workflow.test_autonomous_closed_engine import ClosedModel, P
from workflow.v2.engine import investigate


class QuestionEngineIntegrationTests(unittest.TestCase):
    def test_one_shared_question_context_enters_existing_stages_without_a_planner_call(self):
        model = ClosedModel()
        report = investigate(model, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        calls = [message for message in model.messages if message.get('stage')]
        self.assertEqual(set(message['stage'] for message in calls), {'investigate', 'draft', 'revise'})
        self.assertTrue(all('investigation_questions' in message['input'] for message in calls))
        rework = next(message['input'] for message in calls if message['stage'] == 'investigate'
                      and message['input'].get('review_feedback'))
        question = rework['investigation_questions']['questions'][1]
        self.assertEqual(question['target_id'], 'C1')
        self.assertEqual(question['uncertain_original_claim'], '原范围过宽的命题')
        snapshot = report['investigation_questions']
        self.assertLessEqual(len(snapshot['questions']), 3)
        self.assertEqual(snapshot['ticket_resolution'], 'not_programmatically_determined')
        self.assertTrue(snapshot['questions'][1]['revision_scope_changed'])
        self.assertTrue(snapshot['questions'][1]['current_item_review']['scope_only'])
        self.assertFalse(report['repair_eligible'])

    def test_duplicate_reads_and_rewording_leave_original_question_unresolved(self):
        report = investigate(ClosedModel(duplicate=True), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        question = report['investigation_questions']['questions'][1]
        self.assertEqual(question['acquisition']['new_observation_count'], 0)
        self.assertTrue(question['revision_scope_changed'])
        self.assertEqual(question['resolution'], 'not_programmatically_determined')
        self.assertTrue(any('未新增观测' in gap for gap in report['output']['missing_information']))


if __name__ == '__main__':
    unittest.main()
