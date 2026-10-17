"""Free controls for semantic revision identity, scope and bounded admission."""
import json
import time
import unittest

from runtime.context import Limits
from tests.tools.test_incident_tools import executor, window
from workflow.v2.contracts import parse_draft
from workflow.v2.review import ReviewBatch, evidence_pack_version, parse_review, review_matches
from workflow.v2.rework import claim_rework, parse_revision, rework_feedback, revision_schema


def prepared():
    ex = executor(role='single')
    ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
    draft, errors = parse_draft({'claims': [
        {'statement': '第一机制命题', 'refs': ['R1']},
        {'statement': '第二过宽命题', 'refs': ['R1']}],
        'next_steps': [{'action': '核对未取得的内部记录', 'refs': ['R1']}], 'gaps': ['已知缺口']}, ex)
    assert not errors, errors
    pack = evidence_pack_version(ex)
    items, errors = parse_review({'items': [
        {'target_id': 'C1', 'relation': 'supported', 'refs': ['R1'], 'issue': '脚本注入支持，不证明实际语义。'},
        {'target_id': 'C2', 'relation': 'partial', 'refs': ['R1'], 'issue': '当前来源只支持较窄范围。'},
        {'target_id': 'A1', 'decision': 'needs_revision', 'refs': [], 'issue': '应明确对应服务授权条件。'}]},
        draft, ex, evidence_pack_version=pack)
    assert not errors, errors
    batch = ReviewBatch(items=list(items.values()), evidence_pack_version=pack)
    return ex, draft, batch


def revision(target_id='C2', **changes):
    fields = {'statement': '收窄后的待复审命题', 'refs': ['R1']}
    fields.update(changes)
    return {'target_id': target_id, 'revision': fields, 'change_kind': 'narrow',
            'reason': '按观测范围收窄，不假设已被证实。'}


class ReworkFeedbackTests(unittest.TestCase):
    def test_only_matching_partial_and_insufficient_are_selected(self):
        ex, draft, batch = prepared()
        feedback = rework_feedback(draft, batch, ex)
        self.assertEqual(feedback['allowed_target_ids'], ['C2', 'A1'])
        self.assertEqual(feedback['targets'][0]['target_version'], draft.claims[1].version)
        self.assertEqual(feedback['targets'][0]['review_refs'][0]['evidence_id'], draft.claims[1].refs[0].evidence_id)
        self.assertEqual(feedback['targets'][1]['decision'], 'needs_revision')
        self.assertNotIn('C1', feedback['allowed_target_ids'])

    def test_supported_misjudgment_does_not_automatically_become_rework(self):
        ex, draft, batch = prepared()
        batch = batch.model_copy(update={'items': [item.model_copy(update={
            'relation': 'supported', 'issue': '可能是错误的支持，但程序无法自行证明。'}) for item in batch.items]})
        feedback = rework_feedback(draft, batch, ex)
        self.assertEqual(feedback['allowed_target_ids'], [])
        self.assertEqual(feedback['reason'], 'NO_CURRENT_OBJECTIONS')

    def test_stale_claim_and_invalid_protocol_cannot_supply_current_objection(self):
        ex, draft, batch = prepared()
        items = [item.model_copy(update={'target_version': 'stale'}) if item.target_id == 'C2'
                 else item.model_copy(update={'protocol_status': 'invalid'}) if item.target_id == 'A1'
                 else item for item in batch.items]
        self.assertEqual(rework_feedback(draft, batch.model_copy(update={'items': items}), ex)['targets'], [])

    def test_new_evidence_invalidates_entire_prior_review_pack(self):
        ex, draft, batch = prepared()
        old = batch.evidence_pack_version
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        self.assertNotEqual(old, evidence_pack_version(ex))
        feedback = rework_feedback(draft, batch, ex)
        self.assertEqual(feedback['allowed_target_ids'], [])
        self.assertEqual(feedback['reason'], 'EVIDENCE_CHANGED_REVIEW_REQUIRED')
        self.assertTrue(all(not review_matches(item, target, evidence_pack_version(ex))
            for item, target in zip(batch.items, [*draft.claims, *draft.next_steps])))

    def test_empty_or_failed_read_also_changes_evidence_pack(self):
        ex, draft, batch = prepared()
        ex.check_results[('new_read', '')] = {'tool': 'get_service_logs', 'args': {},
            'status': 'error', 'truncated': False, 'error': 'SOURCE_NOT_CONFIGURED', 'evidence_ids': []}
        self.assertNotEqual(batch.evidence_pack_version, evidence_pack_version(ex))
        self.assertEqual(rework_feedback(draft, batch, ex)['reason'], 'EVIDENCE_CHANGED_REVIEW_REQUIRED')

    def test_duplicate_review_target_cannot_choose_favorable_objection(self):
        ex, draft, batch = prepared()
        duplicate = next(item for item in batch.items if item.target_id == 'C2')
        feedback = rework_feedback(draft, batch.model_copy(update={'items': [*batch.items, duplicate]}), ex)
        self.assertEqual(feedback['allowed_target_ids'], ['A1'])


class RevisionTests(unittest.TestCase):
    def test_only_authorized_slot_changes_and_old_review_no_longer_matches(self):
        ex, draft, batch = prepared()
        result = parse_revision({'items': [revision()]}, draft, ex, ['C2'])
        self.assertEqual(result.errors, [])
        self.assertEqual([item.id for item in result.draft.claims], ['C1', 'C2'])
        self.assertEqual(result.draft.claims[0], draft.claims[0])
        self.assertEqual(result.draft.next_steps, draft.next_steps)
        self.assertEqual(result.changed_ids, ['C2'])
        self.assertNotEqual(result.draft.claims[1].version, draft.claims[1].version)
        self.assertFalse(review_matches(batch.items[1], result.draft.claims[1], batch.evidence_pack_version))
        self.assertFalse(result.semantic_approval_inherited)
        self.assertFalse(result.audit[0]['narrowing_verified_by_program'])
        self.assertEqual(result.audit[0]['claimed_change_kind'], 'narrow')

    def test_injected_ids_or_untargeted_supported_item_cannot_change(self):
        ex, draft, _ = prepared()
        result = parse_revision({'items': [revision('C1'), revision('C3'), revision('C2')]}, draft, ex, ['C2'])
        self.assertEqual(result.draft.claims[0], draft.claims[0])
        self.assertEqual(result.changed_ids, ['C2'])
        self.assertEqual([error['code'] for error in result.errors], ['REVISION_TARGET_DENIED'] * 2)

    def test_bad_ref_preserves_original_but_valid_sibling_revises(self):
        ex, draft, _ = prepared()
        result = parse_revision({'items': [revision(refs=['R999']),
            {'target_id': 'A1', 'revision': {'action': '联系对应负责人，在取得授权后只读核对记录'},
             'reason': '补充授权及交接前提。'}]}, draft, ex, ['C2', 'A1'])
        self.assertEqual(result.draft.claims[1], draft.claims[1])
        self.assertEqual(result.changed_ids, ['A1'])
        self.assertNotEqual(result.draft.next_steps[0].version, draft.next_steps[0].version)
        self.assertEqual(result.errors[0]['category'], 'reference')
        self.assertTrue(result.draft.facts)

    def test_withdrawal_is_explicit_audited_and_keeps_uncertainty(self):
        ex, draft, _ = prepared()
        result = parse_revision({'withdraw': [{'target_id': 'C2', 'reason': '现有证据无法支持原范围。'}],
                                 'gaps': []}, draft, ex, ['C2'])
        self.assertEqual(result.errors, [])
        self.assertEqual([item.id for item in result.draft.claims], ['C1'])
        self.assertEqual(result.withdrawn_ids, ['C2'])
        self.assertEqual(result.audit[0]['operation'], 'withdrawn')
        self.assertEqual(result.audit[0]['before_version'], draft.claims[1].version)
        self.assertIn('已知缺口', result.draft.gaps)
        self.assertTrue(any('撤回不等于' in gap for gap in result.draft.gaps))
        self.assertEqual(result.draft.next_steps, draft.next_steps)

    def test_revision_withdraw_conflict_does_not_choose_one(self):
        ex, draft, _ = prepared()
        result = parse_revision({'items': [revision()],
            'withdraw': [{'target_id': 'C2', 'reason': '同时撤回'}]}, draft, ex, ['C2'])
        self.assertEqual(result.draft.claims, draft.claims)
        self.assertEqual((result.changed_ids, result.withdrawn_ids), ([], []))
        self.assertEqual(result.errors[0]['code'], 'DUPLICATE_REVISION_TARGET')

    def test_repetition_or_omission_cannot_erase_original_siblings(self):
        ex, draft, _ = prepared()
        for raw in ({}, {'items': [revision(), revision()]}, '{broken'):
            result = parse_revision(raw, draft, ex, ['C2'])
            self.assertEqual(result.draft.claims, draft.claims)
            self.assertEqual(result.draft.next_steps, draft.next_steps)
            self.assertEqual(result.changed_ids, [])

    def test_revision_cannot_supply_status_values_or_fact_overrides(self):
        ex, draft, _ = prepared()
        payload = {'items': [revision(status='supported')], 'facts': [{'statement': '伪造'}], 'gaps': []}
        result = parse_revision(payload, draft, ex, ['C2'])
        self.assertEqual(result.draft.claims, draft.claims)
        self.assertEqual(result.draft.facts, draft.facts)
        self.assertTrue(result.errors)
        self.assertEqual(result.changed_ids, [])

    def test_noop_is_audited_not_a_semantic_upgrade(self):
        ex, draft, _ = prepared()
        original = draft.claims[1]
        payload = {'items': [revision(statement=original.statement)]}
        result = parse_revision(payload, draft, ex, ['C2'])
        self.assertEqual(result.changed_ids, [])
        self.assertEqual(result.audit[0]['operation'], 'unchanged')
        self.assertFalse(result.audit[0]['semantic_approval_inherited'])

    def test_new_source_facts_preserved_even_when_revision_invalid(self):
        ex, draft, _ = prepared()
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        result = parse_revision('{broken', draft, ex, ['C2'])
        self.assertGreater(len(result.draft.facts), len(draft.facts))
        self.assertEqual(result.draft.claims, draft.claims)

    def test_schema_only_advertises_server_authorized_ids(self):
        schema = revision_schema(['C2'])
        self.assertEqual(schema['$defs']['ClaimRevisionInput']['properties']['target_id']['enum'], ['C2'])
        self.assertEqual(schema['$defs']['WithdrawalInput']['properties']['target_id']['enum'], ['C2'])
        self.assertEqual(schema['properties']['items']['items']['anyOf'], [{'$ref': '#/$defs/ClaimRevisionInput'}])
        self.assertEqual(revision_schema([])['properties']['items']['maxItems'], 0)


class ReworkAdmissionTests(unittest.TestCase):
    def test_one_round_claim_does_not_add_budget_or_reset_protocol_repairs(self):
        ex, _, _ = prepared()
        state = {'repairs': 1, 'reviewer_repairs': 1}
        original = dict(ex.context.counts)
        self.assertTrue(claim_rework(ex.context, state))
        self.assertFalse(claim_rework(ex.context, state))
        self.assertEqual(ex.context.counts, original)
        self.assertEqual(state['repairs'], 1)
        self.assertEqual(state['semantic_reworks'], 1)

    def test_two_call_terminal_reserve_and_three_call_query_reserve(self):
        ex, _, _ = prepared()
        ex.context.limits = Limits(model_calls=5)
        ex.context.counts['model_calls'] = 4
        state = {}
        self.assertFalse(claim_rework(ex.context, state))
        self.assertEqual(state, {})
        ex.context.counts['model_calls'] = 3
        self.assertFalse(claim_rework(ex.context, state, needs_query=True))
        self.assertTrue(claim_rework(ex.context, state))

    def test_query_requires_tool_and_time_budget(self):
        ex, _, _ = prepared()
        ex.context.counts['tool_calls'] = ex.context.limits.tool_calls
        self.assertFalse(claim_rework(ex.context, {}, needs_query=True))
        ex.context.started = time.monotonic() - ex.context.limits.seconds
        state = {}
        self.assertFalse(claim_rework(ex.context, state))
        self.assertEqual(state, {})


if __name__ == '__main__':
    unittest.main()
