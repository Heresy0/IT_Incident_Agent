"""Free checks of causal counterpoint scope and actionable human task records."""
from dataclasses import replace
import unittest

from agents.contracts import ClaimCounterpoint, ManualHandoffTask, ReadStateAssertion
from evidence.diagnosis import ProtocolError
from tests.workflow.test_review_driven import make_coordinator, review_models
from workflow.semantic_scope import (validate_semantic_scope, read_catalog, read_state,
                                      manual_task_view, counterpoint_view)
from workflow.review_protection import protect_refutations


class SemanticScopeTests(unittest.TestCase):
    def setUp(self):
        self.c = make_coordinator(review_models())
        report = self.c.run()
        self.assertEqual(report['status'], 'completed')
        self.view = self.c.review_context()
        self.eid = next(iter(self.c.evidence))

    def task(self, **changes):
        return ManualHandoffTask(**{'target_id': 'A1', 'subject': 'ticket_service',
            'action_quote': self.c.targets()['A1'].action,
            'question': '核实尚未提供的操作人与审批记录', 'requested_fields': ['operator', 'approval_record'],
            'anchor_evidence_ids': [self.eid], 'completion_criteria': '提供对象、时间和来源明确的原始核查记录',
            'decision_use': '核对变更条件，无法取得则保留未知', **changes})

    def test_local_counterevidence_cannot_refute_whole_free_cause(self):
        a = next(a for a in self.c.review.assessments if a.target_id == 'H1').model_copy(update={
            'verdict': 'not_supported', 'evidence_ids': [self.eid], 'refutation_basis': None})
        review = self.c.review.model_copy(update={'assessments': [a]})
        events = []
        result = protect_refutations(review, self.c.evidence, lambda *args, **kw: events.append(kw),
                                     claim_scoped=True, scope=self.c.ticket.scope)
        self.assertEqual(result.assessments[0].verdict, 'uncertain')
        self.assertEqual(events[0]['code'], 'CLAIM_COUNTERPOINT_NOT_WHOLE_REFUTATION')
        # The standard path remains governed by its original opt-in mode.
        legacy = protect_refutations(review, self.c.evidence, lambda *args, **kw: None)
        self.assertEqual(legacy.assessments[0].verdict, 'not_supported')

    def test_counterpoint_quotes_existing_claim_and_renders_only_real_source_fields(self):
        cause = self.c.targets()['H1'].cause
        point = ClaimCounterpoint(target_id='H1', claim_quote=cause[:100], effect='weakens',
            reason='局部来源不足以排除整条原因', fields=[{'evidence_id': self.eid, 'field_path': 'observed_at'}],
            remaining_uncertainty='其它请求及未读边界仍未知')
        review = self.c.review.model_copy(update={'counterpoints': [point]})
        validate_semantic_scope(review, self.view)
        result = counterpoint_view(review, self.c.evidence)[0]
        self.assertFalse(result['whole_hypothesis_refuted'])
        self.assertFalse(result['semantic_proof_verified'])
        self.assertEqual(result['facts'][0]['value'], self.c.evidence[self.eid].observed_from.isoformat())
        for update, code in [({'claim_quote': 'A different claim that was never made'}, 'COUNTERPOINT_CLAIM_SCOPE'),
                             ({'fields': [{'evidence_id': self.eid, 'field_path': 'invented'}]}, 'COUNTERPOINT_FIELD_UNKNOWN')]:
            from agents.contracts import ClaimCounterpoint as Point
            bad = Point.model_validate({**point.model_dump(), **update})
            with self.assertRaises(ProtocolError) as error:
                validate_semantic_scope(review.model_copy(update={'counterpoints': [bad]}), self.view)
            self.assertEqual(error.exception.code, code)

    def test_unqueried_changes_cannot_be_reported_as_failed_and_no_read_is_authorized(self):
        row = next(row for row in read_catalog(self.c.executors, self.c.ticket.scope).values()
                   if row['tool'] == 'get_recent_changes')
        self.assertEqual(row['state'], 'not_queried')
        before = dict(self.c.context.counts)
        with self.assertRaises(ProtocolError) as error:
            read_state(ReadStateAssertion(read_id=row['read_id'], state='failed'), self.c.executors, self.c.ticket.scope)
        self.assertEqual(error.exception.code, 'LEDGER_STATEMENT_MISMATCH')
        self.assertEqual(self.c.context.counts, before)
        with self.assertRaises(ProtocolError):
            read_state(ReadStateAssertion(read_id='RD_' + '0' * 16, state='failed'), self.c.executors, self.c.ticket.scope)

    def test_same_record_timestamp_is_known_but_new_observation_timestamp_is_distinct(self):
        task = self.task(record_scope='anchored_record', requested_fields=['timestamp'])
        with self.assertRaises(ProtocolError) as error:
            validate_semantic_scope(self.c.review.model_copy(update={'manual_tasks': [task]}), self.view)
        self.assertEqual(error.exception.code, 'MANUAL_HANDOFF_ALREADY_OBSERVED')
        new = task.model_copy(update={'record_scope': 'new_observation'})
        validate_semantic_scope(self.c.review.model_copy(update={'manual_tasks': [new]}), self.view)

    def test_json_reviewer_must_detail_supported_manual_action_without_promoting_unreviewed_tasks(self):
        with self.assertRaises(ProtocolError) as error:
            validate_semantic_scope(self.c.review, replace(self.view, require_blockers=True))
        self.assertEqual(error.exception.code, 'MANUAL_HANDOFF_DETAIL_REQUIRED')
        task = self.task()
        review = self.c.review.model_copy(update={'manual_tasks': [task]})
        validate_semantic_scope(review, replace(self.view, require_blockers=True))
        rows = manual_task_view(review, self.c.executors, self.c.ticket.scope, self.c.evidence)
        self.assertFalse(rows[0]['execution_authorized'])
        self.assertTrue(rows[0]['model_authored'])
        self.assertTrue(rows[0]['known_fields'])
        uncertain = review.model_copy(update={'assessments': [a.model_copy(update={'verdict': 'uncertain'})
            if a.target_id == 'A1' else a for a in review.assessments]})
        with self.assertRaises(ProtocolError) as error:
            validate_semantic_scope(uncertain, self.view)
        self.assertEqual(error.exception.code, 'MANUAL_HANDOFF_UNREVIEWED')

    def test_external_task_cannot_use_local_read_as_remote_internal_observation(self):
        row = next(iter(read_catalog(self.c.executors, self.c.ticket.scope).values()))
        task = self.task(subject='external_dependency', related_reads=[{'read_id': row['read_id'], 'state': row['state']}])
        with self.assertRaises(ProtocolError) as error:
            validate_semantic_scope(self.c.review.model_copy(update={'manual_tasks': [task]}), self.view)
        self.assertEqual(error.exception.code, 'MANUAL_HANDOFF_EXTERNAL_READ')


if __name__ == '__main__':
    unittest.main()
