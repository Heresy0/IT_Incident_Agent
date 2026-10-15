"""Explicit editing, evidence, handoff and direct collection routes; no paid calls."""
import json
import unittest
from unittest.mock import patch
from copy import deepcopy
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


def scope_models(*, disposition='revise', persistent=False, pending_need=False):
    models = review_models()
    def diagnosis(response, messages, calls):
        data = json.loads(response.content)
        data['hypotheses'][0].update(cause='Unproven quota reduction' if calls == 1 else 'Observed dependency unreachable',
                                     level='mechanism')
        return json_response(data)
    def reviewer(response, messages, calls):
        data = json.loads(response.content)
        if calls == 2:
            input_data = json.loads(messages[1].content)['input']
            self_target = input_data['targets']['H1']['cause']
            assert self_target == 'Unproven quota reduction', 'Proposed correction must not be committed before independent review'
            evidence_ids = data['assessments'][0]['evidence_ids']
            decision = {'target_id': 'H1', 'disposition': disposition,
                        'reason': 'Independently checked raw observations', 'evidence_ids': evidence_ids}
            data['scope_assessments'] = [decision]
            if disposition != 'keep':
                next(item for item in data['assessments'] if item['target_id'] == 'H1').update(
                    verdict='uncertain', reason='Quota claim has no current evidence')
            if disposition == 'revise':
                data['revision_requests'] = [{'target_id': 'H1', 'issue_type': 'reasoning',
                    'reason': 'Replace unproven attribution', 'required_change': 'Explain the observed dependency failure only'}]
                if pending_need:
                    next(item for item in data['need_assessments'] if item['need_id'] == 'N2').update(
                        verdict='uncertain', gap_kind='revision', revision_targets=['H1'], blocker=None,
                        evidence_ids=evidence_ids, reason='Evidence exists; draft mechanism requires correction')
            if disposition == 'evidence':
                data['request_evidence'] = {'need_id': 'N2', 'target_id': 'H1', 'missing_observation': 'Remote ownership records'}
                next(item for item in data['need_assessments'] if item['need_id'] == 'N2').update(
                    verdict='uncertain', blocker={'cause_level': 'mechanism', 'affected_targets': ['H1'],
                        'missing_observation': 'Remote ownership records', 'decision_impact': 'Determine causal scope',
                        'resolution': 'external_source'})
        elif calls > 2 and persistent:
            next(item for item in data['assessments'] if item['target_id'] == 'H1').update(verdict='uncertain')
            data['revision_requests'] = [{'target_id': 'H1', 'issue_type': 'reasoning',
                'reason': 'Still not satisfactory', 'required_change': 'Revise again'}]
        return json_response(data)
    models['diagnosis'] = AlterOutput(models['diagnosis'], diagnosis)
    models['reviewer'] = AlterOutput(models['reviewer'], reviewer)
    return models


class ReviewDrivenTests(unittest.TestCase):
    def test_implicit_annotation_only_drift_is_restored_without_scope_proposal(self):
        from agents.contracts import RevisionDiagnosisSelection
        c = self.prepare_revision()
        value = c.revision_task.model_input(c.references)['output_template']
        value['recommended_actions'][0]['action'] = 'Neutral check of actual records'
        value['hypotheses'][0]['evidence_explanation'] = 'Added public commentary; no claim or evidence change'
        result = c.revision_candidate(RevisionDiagnosisSelection.model_validate(value))
        self.assertEqual(result.draft.hypotheses, c.draft.hypotheses)
        self.assertEqual(result.concerns, ())
        self.assertEqual(result.annotation_drifts[0]['target_id'], 'H1')

    def test_explicit_annotation_proposal_is_not_silently_discarded(self):
        from agents.contracts import RevisionDiagnosisSelection
        c = self.prepare_revision()
        value = c.revision_task.model_input(c.references)['output_template']
        value['recommended_actions'][0]['action'] = 'Neutral check of actual records'
        candidate = {**value['hypotheses'][0], 'evidence_explanation': 'Question original evidence sufficiency'}
        value['revision_concerns'] = [{'target_id': 'H1', 'reason': 'Request independent evidence review', 'candidate': candidate}]
        result = c.revision_candidate(RevisionDiagnosisSelection.model_validate(value))
        self.assertEqual(result.concerns[0]['origin'], 'explicit_proposal')
        self.assertEqual(result.annotation_drifts, ())

    def test_trigger_only_uncertainty_gets_scope_diagnostic_before_missing_blocker(self):
        from agents.contracts import Hypothesis
        c = make_coordinator()
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.draft.hypotheses.append(Hypothesis(hypothesis_id='H2', cause='Unknown origin', level='trigger', support_refs=[], counter_refs=[], pending_checks=[]))
        value = json.loads(c.models['reviewer'].invoke([SystemMessage(content='test'),
            HumanMessage(content=json.dumps({'input': c.reviewer_input()}))]).content)
        next(item for item in value['assessments'] if item['target_id'] == 'H2').update(verdict='uncertain', evidence_ids=[])
        next(item for item in value['need_assessments'] if item['need_id'] == 'N2').update(verdict='uncertain', blocker=None)
        with self.assertRaises(ProtocolError) as error:
            c.validate_review(RevisionReviewDecision.model_validate(value))
        self.assertEqual(error.exception.code, 'REVIEW_NEED_SCOPE')
        self.assertEqual(error.exception.details[0]['eligible_unresolved_targets'], [])
        self.assertEqual(c.evidence_needs['N2'].status, 'pending')

    def test_supported_mechanism_cannot_pad_a_trigger_only_blocker(self):
        from agents.contracts import Hypothesis
        c = make_coordinator()
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.draft.hypotheses.append(Hypothesis(hypothesis_id='H2', cause='Unknown origin', level='trigger', support_refs=[], counter_refs=[], pending_checks=[]))
        value = json.loads(c.models['reviewer'].invoke([SystemMessage(content='test'),
            HumanMessage(content=json.dumps({'input': c.reviewer_input()}))]).content)
        next(item for item in value['assessments'] if item['target_id'] == 'H2').update(verdict='uncertain', evidence_ids=[])
        need = next(item for item in value['need_assessments'] if item['need_id'] == 'N2')
        need.update(verdict='uncertain', blocker={'cause_level': 'mechanism', 'affected_targets': ['H1', 'H2'],
            'missing_observation': 'Remote origin', 'decision_impact': 'Why it started', 'resolution': 'external_source'})
        with self.assertRaises(ProtocolError) as error:
            c.validate_review(RevisionReviewDecision.model_validate(value))
        self.assertEqual(error.exception.code, 'REVIEW_BLOCKER_SCOPE')
        self.assertEqual(error.exception.details[0]['target_verdicts']['H1'], 'supported')

    def test_revision_conflict_pinpoints_blocker_and_keeps_verdicts_unchanged(self):
        from workflow.revision import validate_revision_needs
        from agents.contracts import RevisionNeedAssessment
        c = self.prepare_revision()
        evidence = [key for key, item in c.evidence.items() if item.kind == 'observation']
        review = RevisionReviewDecision(assessments=[{'target_id': 'H1', 'verdict': 'uncertain', 'reason': 'Needs edit'}],
            need_assessments=[RevisionNeedAssessment(need_id='N2', verdict='uncertain', reason='Existing evidence, pending draft',
                gap_kind='revision', revision_targets=['H1'], evidence_ids=evidence,
                blocker={'cause_level': 'mechanism', 'affected_targets': ['H1'], 'missing_observation': 'Draft rewrite',
                         'decision_impact': 'Correct draft', 'resolution': 'not_planned'})],
            revision_requests=[{'target_id': 'H1', 'issue_type': 'reasoning', 'reason': 'Unsupported attribution', 'required_change': 'Rewrite'}])
        with self.assertRaises(ProtocolError) as error:
            validate_revision_needs(review, c.evidence)
        self.assertEqual(error.exception.details[0]['path'], 'need_assessments.0.blocker')
        self.assertEqual(review.need_assessments[0].verdict, 'uncertain')

    def test_uncertain_need_pending_revision_does_not_become_evidence_query(self):
        report = make_coordinator(scope_models(pending_need=True)).run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['scope_revision_rounds'], 1)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['tool_calls'], 4)
        pending = next(row for row in report['reviews'][1]['review']['need_assessments'] if row['need_id'] == 'N2')
        self.assertEqual(pending['verdict'], 'uncertain')
        self.assertEqual(pending['gap_kind'], 'revision')
        final = next(row for row in report['reviews'][2]['review']['need_assessments'] if row['need_id'] == 'N2')
        self.assertEqual(final['verdict'], 'supported')

    def test_revision_need_marker_cannot_hide_real_gap_or_missing_authorization(self):
        from workflow.revision import validate_revision_needs
        c = make_coordinator(scope_models(pending_need=True))
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.review_draft()
        c.diagnose()
        data = json.loads(c.models['reviewer'].invoke([SystemMessage(content='test'),
            HumanMessage(content=json.dumps({'input': c.reviewer_input()}))]).content)
        for kind in ('no_request', 'no_current_evidence', 'real_gap', 'wrong_target'):
            value = deepcopy(data)
            need = next(item for item in value['need_assessments'] if item['need_id'] == 'N2')
            if kind == 'no_request':
                value['revision_requests'] = []
            elif kind == 'no_current_evidence':
                need['evidence_ids'] = []
            elif kind == 'wrong_target':
                need['revision_targets'] = ['A1']
            else:
                value['request_evidence'] = {'need_id': 'N2', 'target_id': 'H1', 'missing_observation': 'New causal observation'}
            with self.subTest(kind=kind), self.assertRaises(ProtocolError):
                validate_revision_needs(RevisionReviewDecision.model_validate(value), c.evidence)

    def test_legacy_review_schema_has_no_revision_gap_marker(self):
        from agents.contracts import ReviewDecision
        self.assertNotIn('gap_kind', ReviewDecision.model_json_schema()['$defs']['NeedAssessment']['properties'])

    def test_non_target_hypothesis_correction_is_a_proposal_before_review(self):
        c = self.prepare_revision()
        from agents.contracts import RevisionDiagnosisSelection
        value = c.revision_task.model_input(c.references)['output_template']
        value['recommended_actions'][0]['action'] = 'Neutral check of actual records'
        value['hypotheses'][0]['cause'] = 'Alternative proposed correction'
        before = deepcopy(c.draft)
        result = c.revision_candidate(RevisionDiagnosisSelection.model_validate(value))
        self.assertEqual(result.draft.hypotheses, before.hypotheses)
        self.assertEqual(result.concerns[0]['candidate']['cause'], 'Alternative proposed correction')
        self.assertEqual(result.concerns[0]['origin'], 'scope_projection')
        self.assertEqual(c.draft, before)

    def test_explicit_non_target_proposal_is_validated_without_committing_it(self):
        c = self.prepare_revision()
        from agents.contracts import RevisionDiagnosisSelection
        value = c.revision_task.model_input(c.references)['output_template']
        value['recommended_actions'][0]['action'] = 'Neutral check of actual records'
        proposal = {**value['hypotheses'][0], 'cause': 'Proposed evidence-based correction'}
        value['revision_concerns'] = [{'target_id': 'H1', 'reason': 'Original attribution unsupported', 'candidate': proposal}]
        result = c.revision_candidate(RevisionDiagnosisSelection.model_validate(value))
        self.assertEqual(result.concerns[0]['origin'], 'explicit_proposal')
        self.assertEqual(result.draft.hypotheses, c.draft.hypotheses)
        value['revision_concerns'][0]['candidate']['support_refs'] = [{'reference_id': 'REF_' + '0' * 24}]
        with self.assertRaises(ProtocolError):
            c.revision_candidate(RevisionDiagnosisSelection.model_validate(value))

    def test_scope_projection_does_not_bypass_empty_edit_or_execution_guard(self):
        from agents.contracts import RevisionDiagnosisSelection
        c = self.prepare_revision()
        original = c.revision_task.model_input(c.references)['output_template']
        for mutation in ('noop', 'execution', 'count'):
            value = deepcopy(original)
            value['hypotheses'][0]['cause'] = 'New proposed mechanism'
            if mutation != 'noop':
                value['recommended_actions'][0]['action'] = 'Neutral actual check'
            if mutation == 'execution':
                value['recommended_actions'][0]['requires_approval'] = True
            if mutation == 'count':
                value['hypotheses'].append(deepcopy(value['hypotheses'][0]))
            with self.subTest(mutation=mutation), self.assertRaises(ProtocolError):
                c.revision_candidate(RevisionDiagnosisSelection.model_validate(value))

    def test_reviewer_authorizes_one_bound_scope_correction_then_full_review(self):
        report = make_coordinator(scope_models()).run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['pure_revision_rounds'], 1)
        self.assertEqual(report['scope_revision_rounds'], 1)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 7)
        self.assertEqual(report['run_summary']['tool_calls'], 4)
        self.assertEqual([row['revision'] for row in report['reviews']], [1, 2, 3])
        self.assertEqual(report['drafts'][1]['draft']['hypotheses'][0]['cause'], 'Unproven quota reduction')
        self.assertEqual(report['output']['hypotheses'][0]['cause'], 'Observed dependency unreachable')
        extension = next(e for e in report['events'] if e['type'] == 'revision_scope_extended')
        self.assertEqual(extension['target_ids'], ['H1'])
        self.assertFalse(extension['total_budget_changed'])

    def test_rejected_proposal_cannot_broaden_scope(self):
        report = make_coordinator(scope_models(disposition='keep')).run()
        self.assertEqual(report['scope_revision_rounds'], 0)
        self.assertEqual(report['drafts'][1]['draft']['hypotheses'][0]['cause'], 'Unproven quota reduction')
        self.assertFalse(any(e['type'] == 'revision_scope_extended' for e in report['events']))

    def test_real_new_evidence_gap_takes_priority_over_scope_extension(self):
        report = make_coordinator(scope_models(disposition='evidence')).run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['scope_revision_rounds'], 0)
        self.assertEqual(report['handoff']['reason'], 'external_information_required')

    def test_repeated_reasoning_revisions_cannot_loop(self):
        report = make_coordinator(scope_models(persistent=True)).run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['scope_revision_rounds'], 1)
        self.assertEqual(len(report['drafts']), 3)
        self.assertEqual(report['handoff']['reason'], 'revision_limit_or_budget')

    def test_low_budget_denies_scope_extension(self):
        report = make_coordinator(scope_models(), limits=Limits(model_calls=7, tool_calls=4, seconds=300)).run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['pure_revision_rounds'], 1)
        self.assertEqual(report['scope_revision_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 5)
        self.assertEqual(report['handoff']['reason'], 'revision_limit_or_budget')

    def test_stale_concern_cannot_start_an_independent_review(self):
        c = make_coordinator(scope_models())
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.review_draft()
        c.diagnose()
        before = c.context.counts['model_calls']
        c.executors['investigation'].check_results['stale'] = {'status': 'empty'}
        with self.assertRaises(ProtocolError) as error:
            c.review_draft()
        self.assertEqual(error.exception.code, 'STALE_REVISION_CONCERNS')
        self.assertEqual(c.context.counts['model_calls'], before)

    def test_unresolved_or_contradictory_scope_decisions_are_rejected(self):
        c = make_coordinator(scope_models())
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        c.review_draft()
        c.diagnose()
        value = json.loads(c.models['reviewer'].invoke([SystemMessage(content='test'),
            HumanMessage(content=json.dumps({'input': c.reviewer_input()}))]).content)
        for kind in ('missing', 'contradiction', 'unknown'):
            candidate = deepcopy(value)
            if kind == 'missing':
                candidate['scope_assessments'] = []
            elif kind == 'contradiction':
                candidate['scope_assessments'][0]['disposition'] = 'keep'
            else:
                candidate['scope_assessments'][0]['evidence_ids'] = ['EV_unknown']
            with self.subTest(kind=kind), self.assertRaises(ProtocolError):
                c.validate_review(RevisionReviewDecision.model_validate(candidate))
    def test_direct_collection_saves_one_planning_call(self):
        baseline = collaborate(planned_models(), FixtureProvider('case_001', P), P)
        c = make_coordinator()
        report = c.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['run_summary']['model_calls'], 3)
        self.assertEqual(baseline['run_summary']['model_calls'], 4)
        self.assertEqual(report['run_summary']['tool_calls'], baseline['run_summary']['tool_calls'] + 2)
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)
        self.assertEqual({k: v for k, v in report['output'].items() if k != 'missing_information'},
                         {k: v for k, v in baseline['output'].items() if k != 'missing_information'})
        self.assertEqual(sum(e['type'] == 'local_coverage_selected' for e in report['events']), 1)

    def test_revision_uses_no_queries_and_every_new_target_is_reviewed(self):
        c = make_coordinator(review_models())
        report = c.run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['pure_revision_rounds'], 1)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 5)
        self.assertEqual(report['run_summary']['tool_calls'], 4)
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
        self.assertEqual(report['run_summary']['tool_calls'], 5)
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
        # Complete the program-selected local baseline before entering judging.
        while c.next_node == 'investigation' and not c.stop:
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

    def test_revision_template_uses_current_reference_selectors_and_restores_draft(self):
        from agents.contracts import DiagnosisSelection
        c = self.prepare_revision()
        task = c.revision_task
        view = task.model_input(c.references)
        refs = view['output_template']['findings'][0]['refs']
        self.assertTrue(all(set(item) == {'reference_id'} and item['reference_id'] in c.references for item in refs))
        self.assertEqual(view['editable_target_ids'], ['A1'])
        from evidence.diagnosis import expand_diagnosis
        restored = expand_diagnosis(DiagnosisSelection.model_validate(view['output_template']), c)
        self.assertEqual(restored, task.draft)

    def test_unchanged_requested_target_cannot_finish_revision(self):
        c = self.prepare_revision()
        task = c.revision_task
        with self.assertRaises(ProtocolError) as error:
            task.validate_candidate(task.draft)
        self.assertEqual(error.exception.details[0]['code'], 'REQUESTED_TARGET_UNCHANGED')
        action = task.draft.recommended_actions[0].model_copy(update={'action': '核对当前协议与配置，未知结果保留'})
        task.validate_candidate(task.draft.model_copy(update={'recommended_actions': [action]}))

    def test_generation_schema_exposes_fixed_unrequested_targets_and_expression_fields(self):
        from agents.contracts import DiagnosisSelection
        c = self.prepare_revision()
        task = c.revision_task
        original = DiagnosisSelection.model_json_schema()
        schema = task.output_schema(original, c.references)
        template = task.model_input(c.references)['output_template']
        self.assertNotIn('prefixItems', original['properties']['hypotheses'])
        self.assertEqual(schema['properties']['hypotheses']['prefixItems'], [{'const': template['hypotheses'][0]}])
        self.assertEqual(schema['properties']['hypotheses']['maxItems'], 1)
        self.assertFalse(schema['properties']['recommended_actions']['items'])
        action = schema['properties']['recommended_actions']['prefixItems'][0]['allOf']
        self.assertEqual(action[1], {'not': {'const': template['recommended_actions'][0]}})
        self.assertEqual(action[2]['properties']['kind'], {'const': 'manual_check'})

    def test_pure_edit_rejects_new_hypothesis_or_changed_unrequested_mechanism(self):
        c = self.prepare_revision()
        task = c.revision_task
        original = task.draft
        for changes in ({'hypotheses': [*original.hypotheses, original.hypotheses[0]]},
                        {'hypotheses': [original.hypotheses[0].model_copy(update={'cause': 'Invented remote trigger'})]}):
            with self.subTest(changes=list(changes)), self.assertRaises(ProtocolError) as error:
                task.validate_candidate(original.model_copy(update=changes))
            self.assertEqual(error.exception.code, 'REVISION_SCOPE_CHANGED')

    def test_expression_edit_cannot_change_action_type_or_remove_prior_gap(self):
        c = self.prepare_revision()
        task = c.revision_task
        action = task.draft.recommended_actions[0].model_copy(update={'kind': 'read_only_check'})
        with self.assertRaises(ProtocolError):
            task.validate_candidate(task.draft.model_copy(update={'recommended_actions': [action]}))
        from dataclasses import replace
        with_gap = replace(task, draft=task.draft.model_copy(update={'missing_information': ['Keep unresolved origin']}))
        with self.assertRaises(ProtocolError):
            with_gap.validate_candidate(task.draft)

    def test_scope_violation_is_corrected_using_original_slot_without_tools(self):
        models = review_models()
        def drift(response, messages, calls):
            data = json.loads(response.content)
            if calls == 2:
                data['hypotheses'].append({'cause': 'Invented cause', 'level': 'trigger', 'support_refs': []})
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], drift)
        report = make_coordinator(models).run()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['pure_revision_rounds'], 1)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertEqual(report['run_summary']['tool_calls'], 4)
        self.assertEqual(report['repair_counts']['diagnosis'], 1)
        self.assertEqual(len(report['drafts'][1]['draft']['hypotheses']), 1)
        self.assertIn('REVISION_SCOPE_CHANGED', [e.get('reason') for e in report['events'] if e['type'] == 'validation_failure'])

    def test_persistent_revision_scope_drift_never_commits_partial_candidate(self):
        models = review_models()
        def drift(response, messages, calls):
            data = json.loads(response.content)
            if calls >= 2:
                data['hypotheses'].append({'cause': 'Invented cause', 'level': 'trigger', 'support_refs': []})
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], drift)
        report = make_coordinator(models).run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(len(report['drafts']), 1)
        self.assertEqual(report['handoff']['reason'], 'MODEL_OUTPUT_INVALID')
        self.assertFalse(report['output']['recommended_actions'])

    def test_trigger_unknown_cannot_block_mechanism_in_new_review(self):
        c = make_coordinator()
        c.supervise()
        c.execute_task('investigation')
        c.diagnose()
        from agents.contracts import Hypothesis
        c.draft.hypotheses.append(Hypothesis(hypothesis_id='H2', cause='Unknown origin', level='trigger',
            support_refs=[], counter_refs=[], pending_checks=['Ask remote owner']))
        response = c.models['reviewer'].invoke([SystemMessage(content='test'),
            HumanMessage(content=json.dumps({'input': c.reviewer_input()}))])
        data = json.loads(response.content)
        next(item for item in data['assessments'] if item['target_id'] == 'H2').update(
            verdict='uncertain', evidence_ids=[], reason='Unknown origin')
        need = next(item for item in data['need_assessments'] if item['need_id'] == 'N2')
        need.update(verdict='uncertain', blocker={'cause_level': 'mechanism', 'affected_targets': ['H2'],
            'missing_observation': 'Remote change history', 'decision_impact': 'Explain why it started',
            'resolution': 'not_planned'})
        data['request_evidence'] = {'target_id': 'H2', 'missing_observation': 'Remote change history'}
        review = RevisionReviewDecision.model_validate(data)
        previous = deepcopy(c.evidence_needs)
        with self.assertRaises(ProtocolError) as error:
            c.validate_review(review)
        self.assertEqual(error.exception.code, 'REVIEW_BLOCKER_SCOPE')
        self.assertEqual(c.evidence_needs, previous)
        self.assertIsNone(c.review)

    def test_target_only_request_for_real_mechanism_unknown_remains_blocking(self):
        c = make_coordinator(review_models(gap='not_planned'))
        report = c.run()
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(report['handoff']['blocking'])
        self.assertNotEqual(report['output']['hypotheses'][0]['status'], 'supported')


if __name__ == '__main__':
    unittest.main()
