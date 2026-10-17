"""Bounded question context uses real ledgers; doubles do not assert semantics."""
import json
import unittest
from types import SimpleNamespace

from tests.tools.test_incident_tools import executor, window
from tests.workflow.test_review_closed_loop import prepared, revision
from workflow.v2.questions import InvestigationQuestions
from workflow.v2.rework import parse_revision, rework_feedback
from workflow.v2.review import ReviewBatch, evidence_pack_version, parse_review


def ledger_row(status='empty', **changes):
    return {'tool': 'get_service_logs', 'args': {'category': 'configuration'},
            'status': status, 'truncated': False, 'error': None, 'evidence_ids': [], **changes}


class InvestigationQuestionTests(unittest.TestCase):
    def test_ticket_and_current_objections_are_bounded_and_do_not_assume_original_cause(self):
        ex, draft, batch = prepared()
        questions = InvestigationQuestions(ex).bind_review(rework_feedback(draft, batch, ex), ex)
        view = questions.context(ex)
        self.assertEqual(len(view['questions']), 3)
        self.assertEqual([row['target_id'] for row in view['questions']], [None, 'C2', 'A1'])
        target = view['questions'][1]
        self.assertEqual(target['uncertain_original_claim'], draft.claims[1].statement)
        self.assertEqual(target['review_issue'], batch.items[1].issue)
        self.assertEqual(target['original_evidence_ids'], [draft.claims[1].refs[0].evidence_id])
        self.assertIn('不把原命题当成必须证明的结论', view['next_read_must_add_scope'])
        self.assertIn('可以finish', view['legitimate_finish_handoff'])
        questions.bind_review(rework_feedback(draft, batch, ex), ex)
        self.assertEqual(len(questions.questions), 3)

    def test_channel_candidates_are_registration_facts_not_a_service_route(self):
        ex = executor(role='single')
        ex.capabilities = lambda **_: {'registered_metrics': {},
            'registered_log_categories': ['custom_b', 'custom_a'],
            'log_channels': {'custom_a': {'description': 'registered event boundary'}, 'custom_b': {}}}
        view = InvestigationQuestions(ex).context(ex)
        channels = [row for row in view['registered_read_candidates'] if row['tool'] == 'get_service_logs']
        self.assertEqual([row['category'] for row in channels], ['custom_a', 'custom_b'])
        self.assertEqual(channels[0]['description'], 'registered event boundary')
        self.assertNotIn('description', channels[1])
        self.assertNotIn('recommended_tool', view)
        ex.allowed_tools = lambda: {'get_recent_changes'}
        self.assertEqual([row['tool'] for row in InvestigationQuestions(ex).context(ex)
                          ['registered_read_candidates']], ['get_recent_changes'])

    def test_filtered_read_does_not_hide_unfiltered_channel_candidate(self):
        ex = executor(role='single')
        ex.check_results['filtered'] = ledger_row(args={'category': 'configuration', 'level': 'ERROR'})
        questions = InvestigationQuestions(ex)
        self.assertTrue(any(row.get('category') == 'configuration'
                            for row in questions.context(ex)['registered_read_candidates']))
        ex.check_results['unfiltered'] = ledger_row()
        self.assertFalse(any(row.get('category') == 'configuration'
                             for row in questions.context(ex)['registered_read_candidates']))

    def test_duplicate_empty_and_failure_cannot_become_causal_resolution(self):
        ex = executor(role='single')
        questions = InvestigationQuestions(ex)
        ex.check_results['empty'] = ledger_row()
        first = questions.snapshot(ex)['questions'][0]
        self.assertEqual(first['acquisition']['state'], 'no_new_observation')
        self.assertEqual(first['acquisition']['complete_empty_count'], 1)
        self.assertTrue(first['acquisition']['new_registered_source_attempted'])
        self.assertEqual(questions.snapshot(ex)['questions'][0]['acquisition'], first['acquisition'])
        failure_questions = InvestigationQuestions(ex)
        ex.check_results['failure'] = ledger_row('error', error='SOURCE_NOT_CONFIGURED',
                                                 args={'category': 'resource'})
        failed = failure_questions.snapshot(ex)['questions'][0]
        self.assertEqual(failed['acquisition']['state'], 'source_unavailable')
        self.assertEqual(failed['resolution'], 'not_programmatically_determined')
        self.assertFalse('resolved' in failed)

    def test_new_observation_updates_state_while_knowledge_does_not_confirm_current_incident(self):
        ex = executor(role='single')
        questions = InvestigationQuestions(ex)
        ex.evidence['knowledge'] = SimpleNamespace(kind='runbook')
        self.assertEqual(questions.context(ex)['questions'][0]['acquisition']['state'], 'no_new_observation')
        ex.evidence.pop('knowledge')
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        result = questions.snapshot(ex)
        self.assertEqual(result['questions'][0]['acquisition']['state'], 'new_observation')
        self.assertEqual(result['questions'][0]['acquisition']['new_observation_count'], len(ex.evidence))
        self.assertEqual(result['ticket_resolution'], 'not_programmatically_determined')

    def test_rewording_or_withdrawal_changes_scope_without_closing_investigation(self):
        ex, draft, batch = prepared()
        questions = InvestigationQuestions(ex).bind_review(rework_feedback(draft, batch, ex), ex)
        updated = parse_revision({'items': [revision()]}, draft, ex, ['C2'])
        questions.record_revision(updated.audit, ex)
        question = questions.snapshot(ex)['questions'][1]
        self.assertTrue(question['revision_scope_changed'])
        self.assertFalse(question['target_withdrawn'])
        self.assertEqual(question['acquisition']['new_observation_count'], 0)
        self.assertEqual(question['resolution'], 'not_programmatically_determined')
        withdrawn = parse_revision({'withdraw': [{'target_id': 'C2', 'reason': '原因仍未知'}]}, draft, ex, ['C2'])
        questions.record_revision(withdrawn.audit, ex)
        self.assertTrue(questions.snapshot(ex)['questions'][1]['target_withdrawn'])

    def test_current_item_pass_is_scope_only_and_stale_pack_never_inherits_it(self):
        ex, draft, batch = prepared()
        questions = InvestigationQuestions(ex).bind_review(rework_feedback(draft, batch, ex), ex)
        updated = parse_revision({'items': [revision(statement='仅复述现象')]}, draft, ex, ['C2'])
        questions.record_revision(updated.audit, ex)
        pack = evidence_pack_version(ex)
        items, errors = parse_review({'items': [
            {'target_id': 'C1', 'relation': 'supported', 'refs': ['R1'], 'issue': '固定测试判断'},
            {'target_id': 'C2', 'relation': 'supported', 'refs': ['R1'], 'issue': '仅当前表述得到支持'},
            {'target_id': 'A1', 'decision': 'appropriate', 'refs': [], 'issue': '固定测试判断'}]},
            updated.draft, ex, evidence_pack_version=pack)
        self.assertFalse(errors)
        reviewed = ReviewBatch(items=list(items.values()), evidence_pack_version=pack)
        current = questions.snapshot(ex, updated.draft, reviewed)['questions'][1]
        self.assertTrue(current['current_item_review']['scope_only'])
        self.assertFalse(current['current_item_review']['ticket_resolution_inferred'])
        self.assertEqual(current['resolution'], 'not_programmatically_determined')
        ex.check_results['new_empty'] = ledger_row()
        self.assertNotIn('current_item_review', questions.snapshot(ex, updated.draft, reviewed)['questions'][1])

    def test_large_registration_and_feedback_cannot_grow_question_context_without_bound(self):
        ex, draft, batch = prepared()
        feedback = rework_feedback(draft, batch, ex)
        for item in feedback['targets']:
            item['issue'] = '异议' * 10000
            item['target']['statement'] = '未确认假设' * 10000
        ex.capabilities = lambda **_: {'registered_metrics': {f'metric_{n}': {} for n in range(100)},
            'registered_log_categories': [f'channel_{n}' for n in range(100)],
            'log_channels': {f'channel_{n}': {'description': '描述' * 10000,
                'fields': {f'field_{k}': '字段' * 10000 for k in range(100)}} for n in range(100)}}
        questions = InvestigationQuestions(ex).bind_review(feedback, ex)
        view = questions.context(ex)
        self.assertLessEqual(len(view['registered_read_candidates']), 6)
        self.assertGreater(view['omitted_candidate_count'], 0)
        self.assertLess(len(json.dumps(view, ensure_ascii=False)), 11000)
        self.assertNotIn('incident_id', json.dumps(view))


if __name__ == '__main__':
    unittest.main()
