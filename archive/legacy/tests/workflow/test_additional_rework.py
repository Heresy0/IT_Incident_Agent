"""An additional read is conditional, budgeted, and never a third rework."""
import json
import unittest
from unittest.mock import patch
from agents.contracts import DiagnosisDraft, Hypothesis, ReviewDecision, EvidenceRequest, NeedAssessment, ReviewBlocker, Assessment, SupervisorSelection
from runtime.context import Limits
from tests.workflow import test_pending_rework as fixture


class AdditionalReworkTests(unittest.TestCase):
    def eligible(self):
        c, check = fixture.PendingReworkTests().setup_rework(Limits(model_calls=32, tool_calls=48, seconds=300))
        c.blind_review = False
        c.reworks = 1
        c.draft = DiagnosisDraft(findings=[], hypotheses=[Hypothesis(hypothesis_id='H1', cause='Uncertain mechanism', level='mechanism', support_refs=[], counter_refs=[], pending_checks=[], status='unresolved')], recommended_actions=[], missing_information=[], escalation_team=None)
        c.review = ReviewDecision(assessments=[Assessment(target_id='H1', verdict='uncertain', reason='Missing current observation')],
            need_assessments=[NeedAssessment(need_id='N2', verdict='uncertain', reason='Need distinguishing observation', blocking=True,
                blocker=ReviewBlocker(cause_level='mechanism', affected_targets=['H1'], missing_observation='Current load',
                    decision_impact='Low load contradicts the load candidate; high load supports further investigation.', resolution='scoped_check'))],
            request_evidence=EvidenceRequest(need_id='N2', target_id='H1', missing_observation='Current load', expected_value='Distinguish load-related failure'))
        return c, check

    def test_additional_rework_uses_spare_stage_slot_and_real_read(self):
        c, check = self.eligible()
        self.assertTrue(c.additional_rework_eligible())
        c.additional_rework_authorized = True
        c.pending['challenge_id'] = 'C2'
        c.supervisor_calls = 4
        c.tasks = [{'role': 'investigation', 'need_ids': [], 'required_checks': [], 'expected_value': None} for _ in range(6)]
        c.models['investigation'] = fixture.InterpretOnly()
        with patch.object(c, 'ask', side_effect=AssertionError('Planning call not needed')):
            c.supervise()
        self.assertEqual(c.supervisor_calls, 5)
        self.assertEqual(c.reworks, 2)
        c.execute_task('investigation')
        self.assertEqual(len(c.tasks), 7)
        self.assertEqual(c.context.counts['tool_calls'], 1)
        self.assertEqual(c.next_node, 'diagnosis')
        self.assertEqual(c.tasks[-1]['challenge_id'], 'C2')
        self.assertFalse(c.rework_budget()['can_collect'])
        self.assertIsNone(c.pending_rework_selection())

    def test_budget_accounts_for_review_and_time(self):
        c, check = self.eligible()
        self.assertFalse(c.rework_budget()['can_collect'])
        self.assertTrue(c.rework_budget(additional=True)['can_collect'])
        c.context.counts['model_calls'] = 29
        self.assertFalse(c.rework_budget(additional=True)['can_collect'])
        c.context.counts['model_calls'] = 0
        c.context.started -= 220
        self.assertFalse(c.rework_budget(additional=True)['can_collect'])

    def test_adequate_evidence_optional_trigger_and_external_source_do_not_extend(self):
        c, check = self.eligible()
        saved = c.review
        c.review = saved.model_copy(update={'request_evidence': None})
        self.assertFalse(c.additional_rework_eligible())
        c.review = saved.model_copy(update={'assessments': [saved.assessments[0].model_copy(update={'verdict': 'supported'})]})
        self.assertFalse(c.additional_rework_eligible())
        c.review = saved
        c.draft = c.draft.model_copy(update={'hypotheses': [c.draft.hypotheses[0].model_copy(update={'level': 'trigger'})]})
        self.assertFalse(c.additional_rework_eligible())
        c, check = self.eligible()
        a = c.review.need_assessments[0]
        c.review = c.review.model_copy(update={'need_assessments': [a.model_copy(update={
            'blocker': a.blocker.model_copy(update={'resolution': 'external_source'})})]})
        self.assertFalse(c.additional_rework_eligible())

    def test_completed_failed_and_unrelated_queries_are_rejected(self):
        c, check = self.eligible()
        c.additional_rework_authorized = True
        c.executors['investigation'].execute(check['tool'], check['args'])
        selection = SupervisorSelection.model_validate_json(json.dumps({'action': 'dispatch', 'reason': 'Check again',
            'tasks': [{'role': 'investigation', 'goal': 'Check again', 'need_ids': ['N2'],
                       'checks': [check], 'expected_value': 'Current load'}]}))
        with self.assertRaises(ValueError):
            c.supervisor_decision(selection)
        c, check = self.eligible()
        c.additional_rework_authorized = True
        selection = selection.model_copy(update={'tasks': [selection.tasks[0].model_copy(update={'need_ids': ['N3']})]})
        with self.assertRaises(ValueError):
            c.supervisor_decision(selection)


if __name__ == '__main__':
    unittest.main()
