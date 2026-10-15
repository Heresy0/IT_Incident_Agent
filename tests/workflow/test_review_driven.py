"""Explicit editing, evidence, handoff and direct collection routes; no paid calls."""
import json
import unittest
from unittest.mock import patch
from langchain_core.messages import HumanMessage, SystemMessage

from agents.contracts import ReadOnlyCheck, RevisionReviewDecision
from evidence.diagnosis import ProtocolError
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate
from workflow.revision import collection_exit


REVISION = {'target_id': 'A1', 'issue_type': 'expression',
            'reason': 'Action assumes an unconfirmed origin',
            'required_change': 'Ask for records without assuming the outcome'}


def review_models(*, persistent=False, gap=None):
    models = planned_models()

    def diagnosis(response, messages, calls):
        data = json.loads(response.content)
        data['recommended_actions'][0].update(kind='manual_check', checks=[], repair=None,
            action='Confirm the assumed remote change' if calls == 1 else 'Compare actual protocol and configuration records',
            condition='Observed dependency failure', expected_result='Obtain actual records',
            risk='Read only', requires_approval=False)
        return json_response(data)

    def reviewer(response, messages, calls):
        data = json.loads(response.content)
        if calls == 1 or persistent:
            data['revision_requests'] = [REVISION]
            next(a for a in data['assessments'] if a['target_id'] == 'A1').update(
                verdict='uncertain', reason='Expression needs revision')
            data['follow_up_reason'] = 'Existing evidence suffices to correct the expression'
        if calls == 1 and gap:
            next(a for a in data['assessments'] if a['target_id'] == 'H1').update(
                verdict='uncertain', reason='Missing discriminating observation')
            need = next(a for a in data['need_assessments'] if a['need_id'] == 'N2')
            need.update(verdict='uncertain', blocker={'cause_level': 'mechanism',
                'affected_targets': ['H1'], 'missing_observation': 'Current change records',
                'decision_impact': 'Change records may alter the candidate mechanism',
                'resolution': gap})
            data['request_evidence'] = {'need_id': 'N2', 'target_id': 'H1',
                'missing_observation': 'Current change records', 'expected_value': 'Discriminate mechanism'}
        return json_response(data)

    models['diagnosis'] = AlterOutput(models['diagnosis'], diagnosis)
    models['reviewer'] = AlterOutput(models['reviewer'], reviewer)
    return models


def make_coordinator(models=None, **options):
    return Collaboration(models or planned_models(), FixtureProvider('case_001', P), P,
        orchestration='review_driven', limits=options.pop('limits', Limits(model_calls=16, tool_calls=8, seconds=300)),
        **options)


class ReviewDrivenTests(unittest.TestCase):
    def test_direct_collection_saves_one_planning_call(self):
        baseline = collaborate(planned_models(), FixtureProvider('case_001', P), P)
        c = make_coordinator()
        report = c.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['run_summary']['model_calls'], 3)
        self.assertEqual(baseline['run_summary']['model_calls'], 4)
        self.assertEqual(report['run_summary']['tool_calls'], baseline['run_summary']['tool_calls'])
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)
        self.assertEqual(report['output'], baseline['output'])

    def test_revision_uses_no_queries_and_every_new_target_is_reviewed(self):
        c = make_coordinator(review_models())
        report = c.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['pure_revision_rounds'], 1)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 5)
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(len(report['drafts']), 2)
        self.assertEqual(len(report['reviews']), 2)
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)
        self.assertIn('Compare actual', report['output']['recommended_actions'][0]['action'])
        payload = json.loads(c.models['diagnosis'].model.histories[1][1].content)['input']
        self.assertEqual(payload['revision_task']['base_draft_revision'], 1)
        self.assertTrue(payload['revision_task']['basis_id'])
        self.assertEqual([item['revision'] for item in report['reviews']], [1, 2])

    def test_persistent_revision_cannot_loop_or_publish_uncertain_action(self):
        report = make_coordinator(review_models(persistent=True)).run()
        self.assertEqual(report['pure_revision_rounds'], 1)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['handoff']['reason'], 'revision_limit_or_budget')
        self.assertFalse(report['output']['recommended_actions'])
        self.assertTrue(report['pending_actions'])
        self.assertFalse(report['handoff']['owner_resolved'])

    def test_external_gap_takes_priority_over_revision_and_skips_planning(self):
        c = make_coordinator(review_models(gap='external_source'))
        report = c.run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['pure_revision_rounds'], 0)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['handoff']['reason'], 'external_information_required')
        self.assertIn('Current change records', report['handoff']['next_steps'])
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)

    def test_mixed_gap_merges_editing_into_new_evidence_draft_and_reuses_plan(self):
        c = make_coordinator(review_models(gap='not_planned'))
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        c.evidence_needs['N2'] = c.evidence_needs['N2'].model_copy(update={
            'next_check': ReadOnlyCheck.model_validate_json(json.dumps({
                'tool': 'get_recent_changes', 'args': {**window, 'category': 'configuration'}}))})
        report = c.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['rework_rounds'], 1)
        self.assertEqual(report['pure_revision_rounds'], 0)
        self.assertEqual(len(report['drafts']), 2)
        self.assertEqual(report['run_summary']['tool_calls'], 3)
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)
        payload = json.loads(c.models['diagnosis'].model.histories[1][1].content)['input']
        self.assertEqual(payload['revision_task']['requests'][0]['target_id'], 'A1')
        self.assertTrue(any(e['type'] == 'rework_existing_checks_selected' for e in report['events']))

    def test_low_budget_handoff_does_not_spend_finalization_reserve(self):
        report = make_coordinator(review_models(), limits=Limits(model_calls=5, tool_calls=4, seconds=300)).run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['pure_revision_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 3)
        self.assertEqual(report['handoff']['reason'], 'revision_limit_or_budget')

    def prepare_revision(self):
        c = make_coordinator(review_models())
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.review_draft()
        self.assertEqual(c.next_node, 'revise')
        self.assertEqual(c.phase, 'revise')
        self.assertIsNone(c.pending)
        return c

    def test_stale_revision_is_rejected_before_model_request(self):
        c = self.prepare_revision()
        before = c.context.counts['model_calls']
        c.drafts.append({'revision': 2})
        with self.assertRaises(ProtocolError) as error:
            c.diagnose()
        self.assertEqual(error.exception.code, 'STALE_REVISION_TASK')
        self.assertEqual(c.context.counts['model_calls'], before)

    def test_changed_ledger_during_revision_cannot_commit_new_candidate(self):
        c = self.prepare_revision()
        original_draft = c.draft
        ask = c.ask
        def changed(*args, **kwargs):
            result = ask(*args, **kwargs)
            c.executors['investigation'].check_results['changed'] = {'status': 'empty'}
            return result
        with patch.object(c, 'ask', side_effect=changed), self.assertRaises(ProtocolError):
            c.diagnose()
        self.assertIs(c.draft, original_draft)
        self.assertIsNone(c.review)
        self.assertEqual(len(c.drafts), 1)

    def test_supported_or_unknown_revision_target_is_rejected(self):
        for target in ('H4', 'H1'):
            c = make_coordinator()
            c.supervise()
            c.execute_task('investigation')
            c.diagnose()
            raw = json.loads(c.models['reviewer'].invoke([
                SystemMessage(content='test'),
                HumanMessage(
                    content=json.dumps({'input': c.reviewer_input()}))]).content)
            raw['revision_requests'] = [{**REVISION, 'target_id': target}]
            review = RevisionReviewDecision.model_validate(raw)
            with self.subTest(target=target), self.assertRaises(ProtocolError):
                c.validate_review(review)

    def test_history_only_collection_does_not_enter_diagnosis(self):
        c = make_coordinator()
        for replans, can_replan, expected in ((0, True, 'supervisor'), (1, True, 'handoff'), (0, False, 'handoff')):
            with self.subTest(replans=replans, can_replan=can_replan):
                result = collection_exit({}, c.executors, initial_replans=replans, can_replan=can_replan)
                self.assertEqual(result.route, expected)

    def test_failed_source_error_survives_handoff_and_render(self):
        c = make_coordinator(review_models(persistent=True))
        report = c.run()
        from evidence.render import render
        self.assertIn('人工交接', render(report))
        self.assertEqual(report['handoff']['attempted_checks'][0]['error_code'], None)

    def test_parallel_tool_only_branches_need_no_fictitious_leaf_model_quota(self):
        models = planned_models()
        def parallel(response, messages, calls):
            data = json.loads(response.content)
            data['tasks'].append({'role': 'knowledge', 'goal': 'Retrieve applicable background',
                'need_ids': ['N2'], 'expected_value': 'Compare historical applicability',
                'checks': [{'tool': 'search_runbooks', 'args': {'query': 'connection'}},
                           {'tool': 'search_incidents', 'args': {'symptoms': 'connection'}}]})
            return json_response(data)
        models['supervisor'] = AlterOutput(models['supervisor'], parallel)
        c = make_coordinator(models, limits=Limits(model_calls=6, tool_calls=4, seconds=300))
        report = c.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['collection_execution']['parallel_batches'], 1)
        self.assertEqual(report['run_summary']['model_calls'], 3)
        self.assertEqual(report['run_summary']['tool_calls'], 4)

    def test_nonblocking_trigger_handoff_keeps_unknown_without_promoting_it(self):
        models = planned_models()
        def trigger(response, messages, calls):
            data = json.loads(response.content)
            data['hypotheses'].append({'cause': 'Remote origin is unknown', 'level': 'trigger',
                'support_refs': [], 'pending_checks': ['Ask remote owner for change records']})
            return json_response(data)
        def review(response, messages, calls):
            data = json.loads(response.content)
            next(item for item in data['assessments'] if item['target_id'] == 'H2').update(
                verdict='uncertain', evidence_ids=[], reason='Unknown trigger does not affect the mechanism or next step')
            data['request_evidence'] = {'target_id': 'H2', 'missing_observation': 'Remote change records'}
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], trigger)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        report = make_coordinator(models).run()
        self.assertEqual(report['status'], 'completed')
        self.assertFalse(report['handoff']['blocking'])
        self.assertEqual(report['output']['hypotheses'][1]['status'], 'unresolved')
        self.assertEqual(report['rework_rounds'], 0)

    def test_protocol_failures_never_authorize_revision(self):
        models = review_models()
        models['reviewer'] = AlterOutput(models['reviewer'], lambda response, messages, calls: json_response({'invalid': True}))
        report = make_coordinator(models).run()
        self.assertEqual(report['pure_revision_rounds'], 0)
        self.assertEqual(report['handoff']['reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(len(report['drafts']), 1)

    def test_stale_evidence_request_cannot_plan_or_execute(self):
        c = make_coordinator(review_models(gap='not_planned'))
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.review_draft()
        c.drafts.append({'revision': 2})
        counts = dict(c.context.counts)
        with self.assertRaises(ProtocolError) as error:
            c.supervise()
        self.assertEqual(error.exception.code, 'STALE_REWORK_REQUEST')
        self.assertEqual(c.context.counts, counts)

    def test_extended_contract_uses_original_split_reviewer_correction_slot(self):
        models = planned_models()
        def invalid_first(response, messages, calls):
            data = json.loads(response.content)
            if calls == 1:
                data['revision_requests'] = [{**REVISION, 'target_id': 'H4'}]
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], invalid_first)
        report = make_coordinator(models, reviewer_repair_mode='split').run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['pure_revision_rounds'], 0)
        self.assertEqual(report['repair_counts']['reviewer'], 1)
        failed = next(e for e in report['events'] if e['type'] == 'validation_failure')
        self.assertEqual(failed['details'][0]['path'], 'revision_requests.0.target_id')
        self.assertIn('allowed_target_ids', failed['details'][0])


if __name__ == '__main__':
    unittest.main()
