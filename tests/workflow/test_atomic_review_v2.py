"""Free tests for item isolation and source boundaries, not semantic accuracy."""
import json
import time
import unittest

from langchain_core.messages import AIMessage

from runtime.context import ExecutionError, Limits
from tests.tools.test_incident_tools import executor, window
from workflow.v2.contracts import (draft_correction_targets, draft_input, draft_schema,
                                   merge_drafts, parse_draft, reference_aliases)
from workflow.v2.review import parse_review, review_draft, review_matches


class Responses:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.histories = []

    def invoke(self, messages):
        self.histories.append(messages)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, AIMessage) else AIMessage(content=json.dumps(value))


def observed(*, limits=None):
    ex = executor(role='single', limits=limits)
    ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
    return ex


def draft(ex, count=2, *, next_step=False):
    payload = {'claims': [{'statement': f'待审因果命题{index}', 'refs': ['R1']}
                          for index in range(count)], 'next_steps': [], 'gaps': []}
    if next_step:
        payload['next_steps'] = [{'action': '核对依赖侧请求处理记录', 'condition': '取得授权后', 'refs': ['R1']}]
    value, errors = parse_draft(payload, ex)
    assert not errors, errors
    return value


def item(target_id='C1', relation='supported', refs=None, **updates):
    judgment = {'decision': {'supported': 'appropriate', 'partial': 'needs_revision',
                            'contradicted': 'out_of_scope', 'insufficient': 'insufficient'}[relation]} if target_id.startswith('A') else {'relation': relation}
    value = {'target_id': target_id, **judgment, 'refs': ['R1'] if refs is None else refs,
             'issue': '已检查本条命题及相关来源；这是模型判断。'}
    value.update(updates)
    return value


class AtomicDraftTests(unittest.TestCase):
    def test_short_alias_and_real_ref_expand_only_server_values(self):
        ex = observed()
        catalog = draft_input(ex)
        option = catalog['reference_options'][0]
        real = next(key for key, ref in ex.references.items() if ref == reference_aliases(ex)['R1'])
        value, errors = parse_draft({'claims': [
            {'statement': '命题一', 'refs': ['R1']},
            {'statement': '命题二', 'refs': [{'reference_id': real}]}]}, ex)
        self.assertEqual(errors, [])
        self.assertEqual(value.claims[0].refs, value.claims[1].refs)
        self.assertEqual(option['reference_id'], 'R1')
        self.assertTrue(value.facts)
        self.assertTrue(all('命题' not in fact.statement for fact in value.facts))
        self.assertTrue(any('db_cpu' in fact.statement for fact in value.facts))
        self.assertNotIn('facts', draft_schema()['properties'])

    def test_bad_reference_quarantines_only_its_item_and_preserves_ids(self):
        ex = observed()
        value, errors = parse_draft({'claims': [
            {'statement': '坏引用', 'refs': ['R999999']},
            {'statement': '有效命题', 'refs': ['R1']}],
            'next_steps': [{'action': '核对记录'}], 'gaps': ['信息不完整']}, ex)
        self.assertEqual([claim.id for claim in value.claims], ['C2'])
        self.assertEqual([step.id for step in value.next_steps], ['A1'])
        self.assertEqual(value.gaps, ['信息不完整'])
        self.assertEqual(errors[0]['category'], 'reference')
        self.assertEqual(errors[0]['target_id'], 'C1')
        self.assertTrue(value.facts)

    def test_model_cannot_supply_source_values_server_ids_or_repairs(self):
        ex = observed()
        value, errors = parse_draft({'claims': [
            {'statement': '伪造值', 'refs': [reference_aliases(ex)['R1'].model_dump(mode='json')]},
            {'statement': '指定身份', 'refs': ['R1'], 'id': 'C1'},
            {'statement': '有效命题', 'refs': ['R1']}],
            'next_steps': [{'action': '变更', 'kind': 'registered_repair', 'repair': {}}]}, ex)
        self.assertEqual([claim.id for claim in value.claims], ['C3'])
        self.assertEqual(value.next_steps, [])
        self.assertTrue(all(error['category'] == 'shape' for error in errors))

    def test_reference_spelling_and_duplicate_references_are_strict(self):
        ex = observed()
        value, errors = parse_draft({'claims': [
            {'statement': '相似引用', 'refs': ['r1']},
            {'statement': '重复引用', 'refs': ['R1', 'R1']}]}, ex)
        self.assertEqual(value.claims, [])
        self.assertEqual({error['code'] for error in errors}, {'UNKNOWN_REFERENCE', 'DUPLICATE_REFERENCE'})

    def test_invalid_json_and_fields_preserve_source_facts(self):
        ex = observed()
        for raw in ('{broken', [], {'claims': 'broken', 'gaps': ['valid', 17], 'facts': ['invented']}):
            value, errors = parse_draft(raw, ex)
            self.assertTrue(errors)
            self.assertTrue(value.facts)
            self.assertEqual(value.claims, [])
        self.assertEqual(parse_draft('{broken', ex)[1][0]['category'], 'syntax')

    def test_collection_limits_are_explicit_and_do_not_erase_valid_siblings(self):
        ex = observed()
        value, errors = parse_draft({'claims': [{'statement': '命题'}] * 4,
                                    'next_steps': [{'action': '核对记录'}] * 3,
                                    'gaps': ['gap'] * 9}, ex)
        self.assertEqual((len(value.claims), len(value.next_steps), len(value.gaps)), (3, 2, 8))
        self.assertEqual([error['code'] for error in errors], ['ITEM_LIMIT'] * 3)

    def test_versions_change_when_wording_changes(self):
        ex = observed()
        one, _ = parse_draft({'claims': [{'statement': '命题一', 'refs': ['R1']}]}, ex)
        two, _ = parse_draft({'claims': [{'statement': '命题二', 'refs': ['R1']}]}, ex)
        self.assertEqual(one.claims[0].id, two.claims[0].id)
        self.assertNotEqual(one.claims[0].version, two.claims[0].version)

    def test_draft_correction_keeps_valid_slots_and_maps_failed_original_position(self):
        ex = observed()
        previous, errors = parse_draft({'claims': [
            {'statement': '有效原命题', 'refs': ['R1']},
            {'statement': '坏命题', 'refs': ['R999']}],
            'next_steps': [{'action': '有效核查'}], 'gaps': ['原缺口']}, ex)
        targets = draft_correction_targets(previous, errors)
        self.assertEqual(targets, {'claims': ['C2'], 'next_steps': []})
        self.assertEqual(draft_schema(target_ids=targets)['properties']['claims']['maxItems'], 1)
        corrected, errors = parse_draft({'claims': [{'statement': '修正命题', 'refs': ['R1']}],
            'next_steps': [{'action': '不应替换原建议'}], 'gaps': ['新缺口']}, ex, target_ids=targets)
        merged = merge_drafts(previous, corrected)
        self.assertEqual([claim.id for claim in merged.claims], ['C1', 'C2'])
        self.assertEqual([claim.statement for claim in merged.claims], ['有效原命题', '修正命题'])
        self.assertEqual(merged.next_steps[0].action, '有效核查')
        self.assertEqual(merged.gaps, ['原缺口', '新缺口'])
        self.assertEqual(len(merged.facts), len(previous.facts))
        self.assertEqual(errors[0]['code'], 'ITEM_LIMIT')

    def test_merge_cannot_replace_valid_original_by_conflicting_id(self):
        ex = observed()
        first, _ = parse_draft({'claims': [{'statement': '原命题', 'refs': ['R1']}]}, ex)
        second, _ = parse_draft({'claims': [{'statement': '新命题', 'refs': ['R1']}]}, ex)
        self.assertEqual(merge_drafts(first, second).claims[0].statement, '原命题')

    def test_whole_draft_syntax_failure_offers_free_slots_for_one_retry(self):
        ex = observed()
        previous, errors = parse_draft('{broken', ex)
        self.assertEqual(draft_correction_targets(previous, errors),
                         {'claims': ['C1', 'C2', 'C3'], 'next_steps': ['A1', 'A2']})


class AtomicReviewTests(unittest.TestCase):
    def test_valid_relations_are_independent_model_judgments(self):
        ex = observed()
        value = draft(ex, 3, next_step=True)
        model = Responses({'items': [item('C1'), item('C2', 'partial'),
            item('C3', 'contradicted'), item('A1', 'insufficient', refs=[])]})
        review = review_draft(model, value, ex, ex.context)
        self.assertEqual([result.relation for result in review.items],
                         ['supported', 'partial', 'contradicted', 'insufficient'])
        self.assertTrue(all(result.protocol_status == 'valid' for result in review.items))
        self.assertTrue(all(result.judgment_source == 'model' for result in review.items))
        self.assertEqual(review.attempts, 1)
        self.assertEqual(ex.context.counts['model_calls'], 1)
        self.assertEqual(review.items[-1].judgment_kind, 'check_appropriateness')
        self.assertEqual(review.items[-1].submitted_decision, 'insufficient')
        self.assertIsNone(review.items[-1].submitted_relation)

    def test_check_and_factual_contracts_cannot_be_mixed(self):
        ex = observed()
        value = draft(ex, 1, next_step=True)
        mixed = {'items': [
            {'target_id': 'C1', 'decision': 'appropriate', 'refs': ['R1'], 'issue': '错误判断类型'},
            {'target_id': 'A1', 'relation': 'supported', 'refs': ['R1'], 'issue': '旧格式应拒绝'}]}
        parsed, errors = parse_review(mixed, value, ex)
        self.assertTrue(all(result.protocol_status == 'invalid' for result in parsed.values()))
        self.assertTrue(all(result.relation is None for result in parsed.values()))
        self.assertTrue(all(error['category'] == 'shape' for error in errors))
        proper, errors = parse_review({'items': [item('C1'), item('A1')]}, value, ex)
        self.assertEqual(errors, [])
        self.assertEqual(proper['C1'].judgment_kind, 'factual_relation')
        self.assertEqual(proper['A1'].judgment_kind, 'check_appropriateness')

    def test_manual_check_can_be_appropriate_without_future_result(self):
        ex = observed()
        value, errors = parse_draft({'claims': [], 'next_steps': [{
            'action': '向对应服务负责人核对尚未取得的配置变更意图',
            'condition': '获得该服务授权后核对，不预设是否误操作'}]}, ex)
        self.assertEqual(errors, [])
        model = Responses({'items': [{'target_id': 'A1', 'decision': 'appropriate', 'refs': [],
            'issue': '这是相关且有明确授权前提的核查；缺少待查结果本身不使建议无效。'}]})
        batch = review_draft(model, value, ex, ex.context)
        result = batch.items[0]
        self.assertEqual(result.protocol_status, 'valid')
        self.assertEqual(result.relation, 'supported')
        self.assertEqual(result.submitted_decision, 'appropriate')
        self.assertEqual(result.judgment_kind, 'check_appropriateness')
        self.assertEqual(result.refs, [])
        target = json.loads(model.histories[0][1].content)['targets'][0]
        self.assertEqual(target['review_kind'], 'check_appropriateness')
        self.assertIn('不要求未来核查数据已经存在', target['review_question'])
        self.assertEqual(batch.policy_version, 'atomic_review_v2')

    def test_old_action_relation_is_invalid_then_corrected_without_rewriting_claim(self):
        ex = observed()
        value = draft(ex, 1, next_step=True)
        model = Responses({'items': [item('C1'), {'target_id': 'A1', 'relation': 'supported',
                    'refs': ['R1'], 'issue': '旧格式'}]},
                          {'items': [item('A1')]})
        batch = review_draft(model, value, ex, ex.context)
        self.assertEqual([result.protocol_status for result in batch.items], ['valid', 'valid'])
        self.assertEqual(batch.items[0].submitted_relation, 'supported')
        self.assertEqual(batch.items[1].submitted_decision, 'appropriate')
        corrected = json.loads(model.histories[1][1].content)
        self.assertEqual([target['id'] for target in corrected['targets']], ['A1'])
        self.assertEqual(batch.attempts, 2)

    def test_action_decisions_have_explicit_compatibility_projection(self):
        ex = observed()
        value = draft(ex, 0, next_step=True)
        for decision, projected in [('appropriate', 'supported'), ('needs_revision', 'partial'),
                                    ('out_of_scope', 'contradicted'), ('insufficient', 'insufficient')]:
            with self.subTest(decision=decision):
                parsed, errors = parse_review({'items': [{'target_id': 'A1', 'decision': decision,
                    'refs': [], 'issue': '只测试枚举和兼容映射，不代表模型实际判断准确。'}]}, value, ex)
                self.assertEqual(errors, [])
                self.assertEqual(parsed['A1'].relation, projected)
                self.assertEqual(parsed['A1'].submitted_decision, decision)

    def test_local_correction_only_receives_failed_targets(self):
        ex = observed()
        value = draft(ex)
        model = Responses({'items': [item('C1'), item('C2', refs=['R999999'])]},
                          {'items': [item('C2', 'insufficient', refs=[])]})
        state = {'repairs': 0, 'reviewer_repairs': 0}
        review = review_draft(model, value, ex, ex.context, repairs=state)
        self.assertEqual([result.relation for result in review.items], ['supported', 'insufficient'])
        self.assertEqual([result.protocol_status for result in review.items], ['valid', 'valid'])
        corrected = json.loads(model.histories[1][1].content)
        self.assertEqual([target['id'] for target in corrected['targets']], ['C2'])
        self.assertEqual(state['repairs'], 1)
        self.assertEqual(ex.context.counts['model_calls'], 2)
        self.assertEqual(review.errors[0]['category'], 'reference')

    def test_correction_cannot_rewrite_a_valid_sibling(self):
        ex = observed()
        model = Responses({'items': [item('C1'), item('C2', refs=['R999'])]},
                          {'items': [item('C1', 'contradicted'), item('C2', 'partial')]})
        review = review_draft(model, draft(ex), ex, ex.context)
        self.assertEqual([result.relation for result in review.items], ['supported', 'partial'])
        self.assertIn('UNKNOWN_TARGET', {error['code'] for error in review.errors})

    def test_duplicate_target_does_not_choose_favorable_verdict(self):
        ex = observed()
        parsed, errors = parse_review({'items': [item('C1'), item('C1', 'contradicted'),
                                     item('C2', 'partial')]}, draft(ex), ex)
        self.assertEqual(parsed['C1'].protocol_status, 'invalid')
        self.assertIsNone(parsed['C1'].relation)
        self.assertEqual(parsed['C2'].relation, 'partial')
        self.assertEqual(errors[0]['code'], 'DUPLICATE_TARGET')

    def test_missing_item_remains_unassessed_without_correction(self):
        ex = observed()
        review = review_draft(Responses({'items': [item('C1')]}), draft(ex), ex, ex.context,
                              allow_correction=False)
        self.assertEqual(review.items[0].relation, 'supported')
        self.assertEqual(review.items[1].protocol_status, 'not_run')
        self.assertIsNone(review.items[1].relation)

    def test_second_bad_response_keeps_valid_result(self):
        ex = observed()
        model = Responses({'items': [item('C1'), item('C2', refs=['R999'])]},
                          AIMessage(content='{broken'))
        review = review_draft(model, draft(ex), ex, ex.context)
        self.assertEqual(review.items[0].relation, 'supported')
        self.assertEqual(review.items[1].protocol_status, 'invalid')
        self.assertEqual(review.attempts, 2)
        self.assertIn('syntax', {error['category'] for error in review.errors})

    def test_run_wide_correction_already_consumed_is_not_reset(self):
        ex = observed()
        model = Responses({'items': [item('C1'), item('C2', refs=['R999'])]})
        state = {'repairs': 1, 'reviewer_repairs': 0}
        review = review_draft(model, draft(ex), ex, ex.context, repairs=state)
        self.assertEqual(review.attempts, 1)
        self.assertEqual(review.items[1].protocol_status, 'invalid')
        self.assertEqual(state['repairs'], 1)

    def test_review_does_not_increase_authorized_budget(self):
        ex = observed(limits=Limits(model_calls=1, reserve_model_calls=0))
        model = Responses({'items': [item('C1', refs=['R999'])]})
        review = review_draft(model, draft(ex, 1), ex, ex.context)
        self.assertEqual(review.items[0].protocol_status, 'invalid')
        self.assertEqual(ex.context.counts['model_calls'], 1)
        self.assertEqual(len(model.histories), 1)

    def test_timeout_is_not_semantic_insufficiency(self):
        ex = observed()
        review = review_draft(Responses(ExecutionError('TIMEOUT', 'model')), draft(ex), ex, ex.context)
        self.assertTrue(all(result.protocol_status == 'timeout' for result in review.items))
        self.assertTrue(all(result.relation is None for result in review.items))
        self.assertEqual(review.errors[0]['category'], 'transport')

    def test_no_targets_do_not_call_model_and_source_facts_survive(self):
        ex = observed()
        value, errors = parse_draft({}, ex)
        model = Responses()
        review = review_draft(model, value, ex, ex.context)
        self.assertEqual((review.attempts, model.histories, review.items), (0, [], []))
        self.assertTrue(value.facts)
        self.assertEqual(errors, [])

    def test_full_input_limit_returns_unassessed_without_hiding_sources(self):
        ex = observed(limits=Limits(input_chars=200))
        model = Responses()
        review = review_draft(model, draft(ex), ex, ex.context)
        self.assertTrue(all(result.protocol_status == 'not_run' for result in review.items))
        self.assertTrue(all(result.relation is None for result in review.items))
        self.assertEqual(model.histories, [])
        self.assertEqual(review.errors[0]['category'], 'policy')

    def test_reviewer_sees_uncited_sources_and_query_quality(self):
        ex = observed()
        value = draft(ex, 1)
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        ex.check_results[('test_failure', '')] = {'tool': 'get_service_logs', 'args': {},
            'status': 'error', 'truncated': True, 'error': 'SOURCE_NOT_CONFIGURED', 'evidence_ids': []}
        model = Responses({'items': [item('C1', 'insufficient', refs=[])]})
        review_draft(model, value, ex, ex.context)
        payload = json.loads(model.histories[0][1].content)
        self.assertEqual(len(payload['evidence']), len(ex.evidence))
        cited = {ref.evidence_id for ref in value.claims[0].refs}
        self.assertTrue(any(row['evidence_id'] not in cited for row in payload['evidence']))
        self.assertTrue(any(row.get('error') == 'SOURCE_NOT_CONFIGURED' for row in payload['collection']))
        self.assertTrue(payload['evidence_pack_complete'])
        self.assertEqual(payload['omitted_evidence_count'], 0)

    def test_historical_source_is_not_current_support_or_refutation(self):
        ex = observed()
        ex.execute('search_runbooks', {'query': 'dependency'})
        alias = next(alias for alias, ref in reference_aliases(ex).items()
                     if ex.evidence[ref.evidence_id].kind == 'runbook')
        review = review_draft(Responses({'items': [item('C1', refs=[alias])]}),
                              draft(ex, 1), ex, ex.context)
        result = review.items[0]
        self.assertEqual(result.protocol_status, 'valid')
        self.assertEqual(result.relation, 'insufficient')
        self.assertEqual(result.submitted_relation, 'supported')

    def test_lack_of_support_is_not_an_observed_contradiction(self):
        ex = observed()
        parsed, errors = parse_review({'items': [item('C1', 'contradicted', refs=[])]}, draft(ex, 1), ex)
        self.assertEqual(parsed['C1'].protocol_status, 'invalid')
        self.assertIsNone(parsed['C1'].relation)
        self.assertEqual(errors[0]['code'], 'REVIEW_REFERENCE_REQUIRED')

    def test_version_key_changes_for_claim_and_evidence_pack(self):
        ex = observed()
        first = review_draft(Responses({'items': [item('C1')]}), draft(ex, 1), ex, ex.context)
        changed, _ = parse_draft({'claims': [{'statement': '另一命题', 'refs': ['R1']}]}, ex)
        second = review_draft(Responses({'items': [item('C1')]}), changed, ex, ex.context)
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        third = review_draft(Responses({'items': [item('C1')]}), changed, ex, ex.context)
        self.assertNotEqual(first.items[0].review_key, second.items[0].review_key)
        self.assertNotEqual(second.items[0].review_key, third.items[0].review_key)

    def test_review_matches_rejects_same_id_with_new_target_or_source_pack(self):
        ex = observed()
        original = draft(ex, 1)
        batch = review_draft(Responses({'items': [item('C1')]}), original, ex, ex.context)
        reviewed = batch.items[0]
        self.assertTrue(review_matches(reviewed, original.claims[0], batch.evidence_pack_version))
        revised, _ = parse_draft({'claims': [{'statement': '新命题', 'refs': ['R1']}]}, ex)
        self.assertFalse(review_matches(reviewed, revised.claims[0], batch.evidence_pack_version))
        self.assertFalse(review_matches(reviewed, original.claims[0], 'different-source-pack'))
        self.assertFalse(review_matches(reviewed.model_copy(update={'target_id': 'C2'}),
                                        original.claims[0], batch.evidence_pack_version))
        self.assertFalse(review_matches(reviewed.model_copy(update={'review_key': 'invalid'}),
                                        original.claims[0], batch.evidence_pack_version))

    def test_output_truncation_is_recorded_separately(self):
        ex = observed()
        response = AIMessage(content='{broken', response_metadata={'finish_reason': 'length'})
        review = review_draft(Responses(response), draft(ex, 1), ex, ex.context, allow_correction=False)
        self.assertEqual(review.items[0].protocol_status, 'invalid')
        self.assertIn('length', {error['category'] for error in review.errors})

    def test_parseable_but_truncated_review_cannot_approve_any_item(self):
        for reason in ('length', 'max_tokens'):
            with self.subTest(reason=reason):
                ex = observed()
                value = draft(ex)
                response = AIMessage(content=json.dumps({'items': [item('C1'), item('C2')]}),
                                     response_metadata={'finish_reason': reason})
                review = review_draft(Responses(response), value, ex, ex.context, allow_correction=False)
                self.assertTrue(all(result.protocol_status == 'invalid' for result in review.items))
                self.assertTrue(all(result.relation is None for result in review.items))
                self.assertTrue(value.facts)
                self.assertEqual(review.errors[0]['category'], 'length')

    def test_truncated_review_uses_only_shared_correction_allowance(self):
        ex = observed()
        truncated = AIMessage(content=json.dumps({'items': [item('C1'), item('C2')]}),
                              response_metadata={'finish_reason': 'length'})
        model = Responses(truncated, {'items': [item('C1', 'partial'), item('C2', 'insufficient', refs=[])]})
        repairs = {'repairs': 0, 'reviewer_repairs': 0}
        review = review_draft(model, draft(ex), ex, ex.context, repairs=repairs)
        self.assertEqual([result.relation for result in review.items], ['partial', 'insufficient'])
        self.assertEqual(repairs['repairs'], 1)
        self.assertEqual(ex.context.counts['model_calls'], 2)

    def test_late_response_is_timeout_even_when_content_is_valid(self):
        ex = observed()
        class Late:
            def invoke(self, messages):
                ex.context.started = time.monotonic() - ex.context.limits.seconds - 1
                return AIMessage(content=json.dumps({'items': [item('C1')]}))
        value = draft(ex, 1)
        review = review_draft(Late(), value, ex, ex.context)
        self.assertEqual(review.items[0].protocol_status, 'timeout')
        self.assertIsNone(review.items[0].relation)
        self.assertTrue(value.facts)


if __name__ == '__main__':
    unittest.main()
