"""Review phases each get one correction, inside the unchanged run budget."""
import copy
import json
import unittest
from langchain_core.messages import AIMessage
from agents.contracts import ObservationReview, ReviewDecision, DiagnosisDraft
from agents.protocol import corrections_within_policy, REVIEW_STAGE_POLICY
from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from runtime.context import RunContext, Limits, ExecutionError
from tests.tools.test_incident_tools import P
from workflow.coordinator import Collaboration


class Replies:
    def __init__(self, replies):
        self.replies = iter(replies)
    def invoke(self, messages):
        return AIMessage(content=json.dumps(next(self.replies)))


class ReviewerStageBudgetTests(unittest.TestCase):
    def test_each_review_stage_corrects_once_without_stealing_other_stage(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
            reviewer_repair_mode='split')
        c.draft = DiagnosisDraft(findings=[], hypotheses=[], recommended_actions=[], missing_information=[], escalation_team=None)
        c.models['reviewer'] = Replies([
            {'directly_supported': [{'statement': 'unverified', 'refs': []}]}, {},
            {}, {'assessments': [{'target_id': 'F1', 'verdict': 'uncertain', 'reason': 'Missing evidence'}]}])
        # Use the real ask loop and schemas; business assessment remains outside this control.
        from runtime.context import activate
        with activate(c.context):
            c.ask('reviewer', ObservationReview, {})
            c.ask('reviewer', ReviewDecision, {'required_target_ids': ['F1']})
        self.assertEqual(c.repairs['reviewer_stage_repairs'], {'ObservationReview': 1, 'ReviewDecision': 1})
        self.assertEqual(c.context.counts['model_calls'], 4)
        self.assertEqual(c.repairs['reviewer_repairs'], 2)
        events = [e for e in c.events if e['type'] == 'validation_repair']
        report = {'repair_policy': REVIEW_STAGE_POLICY, 'repairs': 2,
                  'repair_counts': {'collection': 0, 'reviewer': 2}, 'events': events}
        self.assertTrue(corrections_within_policy(report))
        forged = copy.deepcopy(report)
        forged['events'][1]['review_stage'] = 'ObservationReview'
        self.assertFalse(corrections_within_policy(forged))

    def test_no_second_correction_in_same_stage_or_extra_collection_correction(self):
        context, state = RunContext(), {'repairs': 0}
        self.assertTrue(context.claim_repair(state, reviewer=True, review_stage='ObservationReview'))
        self.assertFalse(context.claim_repair(state, reviewer=True, review_stage='ObservationReview'))
        self.assertTrue(context.claim_repair(state, reviewer=True, review_stage='ReviewDecision'))
        self.assertFalse(context.claim_repair(state, reviewer=True, review_stage='ReviewDecision'))
        self.assertTrue(context.claim_repair(state))
        self.assertFalse(context.claim_repair(state))
        self.assertEqual(state['repairs'], 3)

    def test_extra_permission_never_increases_total_model_budget(self):
        c = Collaboration(scripted_models(), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=1), reviewer_repair_mode='split')
        c.draft = DiagnosisDraft(findings=[], hypotheses=[], recommended_actions=[], missing_information=[], escalation_team=None)
        c.models['reviewer'] = Replies([{}])
        from runtime.context import activate
        with activate(c.context), self.assertRaises(ExecutionError):
            c.ask('reviewer', ReviewDecision, {'required_target_ids': ['F1']}, terminal=True)
        self.assertEqual(c.context.counts['model_calls'], 1)


if __name__ == '__main__':
    unittest.main()
