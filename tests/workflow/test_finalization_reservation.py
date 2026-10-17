"""Protocol corrections cannot spend the already quoted mandatory checks."""
import json
import unittest
from langchain_core.messages import AIMessage
from tests.tools.test_incident_tools import executor, window
from tests.workflow.test_autonomous_closed_engine import ClosedModel, P
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.v2.engine import make_draft, investigate


class FinalizationReservationTests(unittest.TestCase):
    def test_bad_draft_cannot_use_review_and_challenge_capacity_for_correction(self):
        ex = executor(role='single', limits=Limits(model_calls=3, tool_calls=8))
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})

        class BadDraft:
            def invoke(self, messages):
                return AIMessage(content=json.dumps({'claims': [{'statement': 'invalid ref', 'refs': ['R999']}]}))
        failures, repairs = [], {'repairs': 0, 'reviewer_repairs': 0}
        make_draft(BadDraft(), ex, ex.context, failures, repairs, 'FINISHED', reserve_after=2)
        self.assertEqual(ex.context.counts['model_calls'], 1)
        self.assertEqual(repairs['repairs'], 0)
        self.assertTrue(failures)

    def test_partial_invalid_review_keeps_last_call_for_supported_sibling_challenge(self):
        class MixedReview(ClosedModel):
            def invoke(self, messages):
                request = json.loads(messages[-1].content)
                if request.get('targets') and not request.get('challenge_policy_version'):
                    return AIMessage(content=json.dumps({'items': [
                        {'target_id': 'C1', 'relation': 'supported', 'refs': ['R1'], 'issue': 'test'},
                        {'target_id': 'A1', 'decision': 'appropriate', 'refs': ['R999'], 'issue': 'test'}]}))
                return super().invoke(messages)
        report = investigate(MixedReview(primary_pass=True), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=5, tool_calls=8), enable_rework=True, enable_challenge=True)
        self.assertEqual(report['repairs'], 0)
        self.assertTrue(report['challenges'][0].get('called'))
        self.assertLessEqual(report['run_summary']['model_calls'], 5)
        self.assertEqual(report['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
