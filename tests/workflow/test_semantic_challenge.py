"""Blind challenge controls; no scripted verdict is a semantic accuracy score."""
import json
import time
import unittest

from langchain_core.messages import AIMessage

from runtime.context import ExecutionError, Limits
from tests.workflow.test_review_closed_loop import prepared
from tests.tools.test_incident_tools import window
from workflow.v2.challenge import CHALLENGE_POLICY_VERSION, challenge_supported
from workflow.v2.rework import rework_feedback
from workflow.v2.review import review_matches


class Responses:
    def __init__(self, value):
        self.value, self.histories = value, []

    def invoke(self, messages):
        self.histories.append(messages)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value if isinstance(self.value, AIMessage) else AIMessage(content=json.dumps(self.value))


def decision(target_id='C1', coverage='covered', **updates):
    result = {'target_id': target_id, 'coverage': coverage, 'refs': ['R1'],
              'uncovered_assertion': '', 'missing_evidence': '', 'reason': '脚本只验证控制边界。'}
    if coverage == 'uncovered':
        result.update(uncovered_assertion='命题中未取得观测的整体性断言', missing_evidence='需要相应对象的业务观测')
    result.update(updates)
    return result


class SemanticChallengeTests(unittest.TestCase):
    def test_pass_sees_original_targets_and_all_sources_but_not_primary_judgment(self):
        ex, draft, batch = prepared()
        batch = batch.model_copy(update={'items': [item.model_copy(update={'issue': 'SECRET_PRIMARY_REASON'})
                                                  for item in batch.items]})
        model = Responses({'items': [decision()]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        body = json.loads(model.histories[0][1].content)
        self.assertEqual([target['id'] for target in body['targets']], ['C1'])
        self.assertEqual(body['targets'][0]['statement'], draft.claims[0].statement)
        self.assertEqual(len(body['evidence']), len(ex.evidence))
        self.assertEqual(len(body['collection']), len(ex.check_results))
        self.assertNotIn('SECRET_PRIMARY_REASON', model.histories[0][1].content)
        self.assertNotIn('original_reviews', body)
        self.assertNotIn('relation', body['targets'][0])
        self.assertEqual(audit['original_reviews'][0]['issue'], 'SECRET_PRIMARY_REASON')
        self.assertEqual(audit['policy_version'], CHALLENGE_POLICY_VERSION)
        self.assertFalse(audit['is_ground_truth'])
        self.assertEqual(result.items, batch.items)

    def test_covered_keeps_primary_supported_without_upgrading_other_items(self):
        ex, draft, batch = prepared()
        model = Responses({'items': [decision(), decision('C2')]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        self.assertEqual([item.relation for item in result.items], ['supported', 'partial', 'partial'])
        self.assertEqual(result.items[0], batch.items[0])
        self.assertEqual(audit['eligible_ids'], ['C1'])
        self.assertEqual(audit['model_calls'], 1)
        self.assertEqual(len(model.histories), 1)
        self.assertEqual(audit['disputed_ids'], [])

    def test_uncovered_becomes_dispute_not_program_certified_false(self):
        ex, draft, batch = prepared()
        model = Responses({'items': [decision(coverage='uncovered')]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        disputed = result.items[0]
        self.assertEqual(disputed.protocol_status, 'valid')
        self.assertEqual(disputed.relation, 'partial')
        self.assertIn('模型异议', disputed.issue)
        self.assertEqual(disputed.review_key, batch.items[0].review_key)
        self.assertTrue(review_matches(disputed, draft.claims[0], batch.evidence_pack_version))
        self.assertEqual(audit['original_reviews'][0]['relation'], 'supported')
        self.assertEqual(audit['disputed_ids'], ['C1'])
        self.assertFalse(audit['is_ground_truth'])
        self.assertIn('C1', rework_feedback(draft, result, ex)['allowed_target_ids'])
        self.assertEqual(result.items[1:], batch.items[1:])

    def test_uncertain_downgrades_without_claiming_a_refutation(self):
        ex, draft, batch = prepared()
        result, audit = challenge_supported(Responses({'items': [decision(coverage='uncertain', refs=[],
                missing_evidence='来源范围不足以裁决')]}), draft, batch, ex, ex.context)
        self.assertEqual(result.items[0].relation, 'insufficient')
        self.assertEqual(audit['disputed_ids'], ['C1'])
        self.assertFalse(audit['is_ground_truth'])

    def test_invalid_json_null_and_shape_fail_closed_without_repair(self):
        for raw in ('{broken', 'null', '[]', '{}', '{"items":"bad"}'):
            with self.subTest(raw=raw):
                ex, draft, batch = prepared()
                model = Responses(AIMessage(content=raw))
                result, audit = challenge_supported(model, draft, batch, ex, ex.context)
                self.assertEqual(result.items[0].protocol_status, 'not_run')
                self.assertIsNone(result.items[0].relation)
                self.assertEqual(audit['unassessed_ids'], ['C1'])
                self.assertEqual(audit['model_calls'], 1)
                self.assertEqual(len(model.histories), 1)

    def test_bad_reference_and_non_specific_uncovered_are_not_accepted(self):
        for candidate in (decision(refs=['R999']), decision(coverage='uncovered', uncovered_assertion=''),
                          decision(coverage='uncovered', missing_evidence='')):
            with self.subTest(candidate=candidate):
                ex, draft, batch = prepared()
                result, audit = challenge_supported(Responses({'items': [candidate]}), draft, batch, ex, ex.context)
                self.assertEqual(result.items[0].protocol_status, 'not_run')
                self.assertIsNone(result.items[0].relation)
                self.assertTrue(audit['errors'])

    def test_missing_and_duplicate_targets_do_not_preserve_approval(self):
        for values in ([decision()], [decision(), decision()]):
            with self.subTest(values=values):
                ex, draft, batch = prepared()
                batch = batch.model_copy(update={'items': [item.model_copy(update={'relation': 'supported'})
                    if item.target_id == 'C2' else item for item in batch.items]})
                result, audit = challenge_supported(Responses({'items': values}), draft, batch, ex, ex.context)
                self.assertEqual(result.items[1].protocol_status, 'not_run')
                self.assertIsNone(result.items[1].relation)
                self.assertIn('C2', audit['unassessed_ids'])
                if len(values) > 1:
                    self.assertEqual(result.items[0].protocol_status, 'not_run')
                else:
                    self.assertEqual(result.items[0].relation, 'supported')

    def test_empty_sources_do_not_allow_covered_without_registered_reference(self):
        ex, draft, batch = prepared()
        result, audit = challenge_supported(Responses({'items': [decision(refs=[])]}), draft, batch, ex, ex.context)
        self.assertEqual(result.items[0].protocol_status, 'not_run')
        self.assertEqual(audit['errors'][0]['code'], 'CHALLENGE_REFERENCE_REQUIRED')

    def test_parseable_length_truncated_response_never_preserves_supported(self):
        for finish in ('length', 'max_tokens'):
            with self.subTest(finish=finish):
                ex, draft, batch = prepared()
                response = AIMessage(content=json.dumps({'items': [decision()]}),
                                     response_metadata={'finish_reason': finish})
                result, audit = challenge_supported(Responses(response), draft, batch, ex, ex.context)
                self.assertIsNone(result.items[0].relation)
                self.assertEqual(result.items[0].protocol_status, 'not_run')
                self.assertEqual(audit['errors'][0]['category'], 'length')

    def test_late_response_is_unassessed_even_when_complete(self):
        ex, draft, batch = prepared()
        class Late:
            def invoke(self, messages):
                ex.context.started = time.monotonic() - ex.context.limits.seconds - 1
                return AIMessage(content=json.dumps({'items': [decision()]}))
        result, audit = challenge_supported(Late(), draft, batch, ex, ex.context)
        self.assertEqual(result.items[0].protocol_status, 'not_run')
        self.assertIsNone(result.items[0].relation)
        self.assertEqual(audit['errors'][0]['code'], 'TIME_BUDGET_EXCEEDED')

    def test_transport_timeout_does_not_preserve_supported_or_repeat_call(self):
        ex, draft, batch = prepared()
        model = Responses(ExecutionError('TIMEOUT', 'model'))
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        self.assertEqual(result.items[0].protocol_status, 'not_run')
        self.assertEqual(audit['model_calls'], 1)
        self.assertEqual(len(model.histories), 1)

    def test_input_limit_does_not_drop_counterevidence_to_obtain_approval(self):
        ex, draft, batch = prepared()
        ex.context.limits = Limits(input_chars=200)
        model = Responses({'items': [decision()]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        self.assertEqual(result.items[0].protocol_status, 'not_run')
        self.assertIsNone(result.items[0].relation)
        self.assertEqual(audit['errors'][0]['code'], 'CHALLENGE_INPUT_LIMIT')
        self.assertEqual(audit['model_calls'], 0)
        self.assertEqual(model.histories, [])

    def test_overall_budget_applies_to_challenge_without_extra_allowance(self):
        ex, draft, batch = prepared()
        ex.context.counts['model_calls'] = ex.context.limits.model_calls
        model = Responses({'items': [decision()]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        self.assertEqual(result.items[0].protocol_status, 'not_run')
        self.assertIsNone(result.items[0].relation)
        self.assertEqual(audit['model_calls'], 0)
        self.assertFalse(audit['called'])
        self.assertEqual(model.histories, [])

    def test_no_supported_current_claims_skip_call_and_cannot_upgrade(self):
        ex, draft, batch = prepared()
        batch = batch.model_copy(update={'items': [item.model_copy(update={'relation': 'partial'}) for item in batch.items]})
        model = Responses({'items': [decision()]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        self.assertEqual(result, batch)
        self.assertEqual(audit['status'], 'skipped')
        self.assertEqual(model.histories, [])

    def test_new_source_or_query_quality_invalidates_whole_old_batch(self):
        ex, draft, batch = prepared()
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        model = Responses({'items': [decision()]})
        result, audit = challenge_supported(model, draft, batch, ex, ex.context)
        self.assertTrue(all(item.protocol_status == 'not_run' for item in result.items))
        self.assertTrue(all(item.relation is None for item in result.items))
        self.assertEqual(audit['status'], 'stale')
        self.assertEqual(model.histories, [])


if __name__ == '__main__':
    unittest.main()
