"""A bad source choice and a duplicate do not consume all recovery chances."""
import json
import unittest
from langchain_core.messages import AIMessage
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.workflow.test_autonomous_closed_engine import ClosedModel, P
from workflow.v2.engine import investigate


class RecoveringReadModel(ClosedModel):
    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        if request.get('stage') == 'investigate' and request['input'].get('review_feedback'):
            self.rework_steps += 1
            choices = [('get_recent_changes', {}), ('get_service_logs', {'category': 'dependency'}),
                       ('get_service_logs', {'category': 'resource'})]
            name, args = choices[min(self.rework_steps - 1, 2)]
            return AIMessage(content=json.dumps({'kind': 'query', 'tool': name, 'args': args}),
                             response_metadata={'finish_reason': 'stop'})
        return super().invoke(messages)


class ReworkQueryRecoveryTests(unittest.TestCase):
    def test_corrected_argument_error_is_retained_as_history_without_false_interruption(self):
        class BadThenValid(ClosedModel):
            def invoke(self, messages):
                request = json.loads(messages[-1].content)
                if request.get('stage') == 'investigate' and request['input'].get('review_feedback'):
                    self.rework_steps += 1
                    value = ({'kind': 'query', 'tool': 'get_service_logs',
                              'args': {'category': 'resource', 'unexpected': True}}
                             if self.rework_steps == 1 else
                             {'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': 'resource'}}
                             if self.rework_steps == 2 else {'kind': 'finish'})
                    return AIMessage(content=json.dumps(value))
                return super().invoke(messages)
        report = investigate(BadThenValid(), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        failure = next(row for row in report['protocol_failures'] if row.get('code') == 'INVALID_ARGUMENTS')
        self.assertTrue(failure['recovered'])
        self.assertEqual(report['run_outcome']['execution']['failure_codes'], [])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['run_summary']['tool_calls'], 2)

    def test_duplicate_feedback_can_be_followed_by_a_third_new_scope_choice(self):
        model = RecoveringReadModel()
        report = investigate(model, FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=12, tool_calls=8), enable_rework=True, enable_challenge=False)
        audit = report['reworks'][0]
        self.assertEqual(audit['decisions'], 3)
        self.assertEqual(audit['read_attempts'], 2)
        self.assertEqual(audit['collection_stop'], 'COLLECTION_READ_LIMIT_REACHED')
        self.assertEqual(report['run_summary']['tool_calls'], 3)
        self.assertLessEqual(report['run_summary']['model_calls'], 12)
        self.assertTrue(any(row['name'] == 'get_service_logs' and
            row['query_profile']['category'] == 'resource' for row in report['collection']))
        reused = next(event for event in report['events'] if event['type'] == 'query_reused')
        self.assertEqual(reused['query_profile']['category'], 'dependency')
        self.assertEqual(len(report['reviews']), 2)
        self.assertFalse(report['repair_eligible'])


if __name__ == '__main__':
    unittest.main()
