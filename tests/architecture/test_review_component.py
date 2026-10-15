"""Review rules preserve evidence/needs and cannot execute reads or enlarge budgets."""
from dataclasses import replace
from datetime import timedelta
import unittest

from agents.contracts import (Assessment, DiagnosisDraft, EvidenceRequest, Hypothesis, NeedAssessment,
                              RecommendedAction, ReviewBlocker, ReviewDecision)
from evidence.contracts import Finding
from evidence.diagnosis import ProtocolError
from evidence.model_view import substantive
from providers.fixtures import FixtureProvider
from runtime.context import RunContext
from tests.tools.test_incident_tools import P
from tools.executor import ToolExecutor
from workflow.evidence_needs import initial_needs, reviewed_needs
from workflow.review import (ReviewAudit, ReviewContext, ReviewRoutingContext, additional_rework_eligible,
    completion_reason, decide_rework, resolve_handoff, validate_review)


class ReviewComponentTests(unittest.TestCase):
    def setUp(self):
        provider = FixtureProvider('case_001', P)
        self.ticket = provider.ticket()
        self.context = RunContext(scope=self.ticket.scope)
        self.executors = {role: ToolExecutor(provider, P, self.ticket.scope, self.context, role=role)
                          for role in ('investigation', 'knowledge')}
        window = self.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        executor = self.executors['investigation']
        executor.execute('get_service_metrics', {**window, 'metrics': ['db_cpu'], 'granularity': '1m'})
        self.evidence, self.references = executor.evidence, executor.references
        reference = next(ref for ref in self.references.values() if substantive(ref.field_path))
        evidence_id = reference.evidence_id
        self.needs = initial_needs(self.ticket)
        self.draft = DiagnosisDraft(findings=[Finding(statement='Recorded metric sample', refs=[reference])],
            hypotheses=[Hypothesis(hypothesis_id='H1', cause='Candidate mechanism', level='mechanism',
                support_refs=[reference], counter_refs=[], pending_checks=[])],
            recommended_actions=[RecommendedAction(action='Inspect the current service logs',
                condition='Read only', expected_result='Obtain actual logs', risk='No writes',
                requires_approval=False, kind='manual_check')], missing_information=[], escalation_team=None)
        self.review = ReviewDecision(assessments=[Assessment(target_id=key, verdict='supported',
            evidence_ids=[evidence_id], reason='Fixture assessment for boundary tests') for key in ('F1', 'H1', 'A1')],
            need_assessments=[NeedAssessment(need_id=key, verdict='supported', evidence_ids=[evidence_id],
                reason='Fixture assessment for boundary tests') for key in self.needs])
        self.targets = {'F1': self.draft.findings[0], 'H1': self.draft.hypotheses[0],
                        'A1': self.draft.recommended_actions[0]}
        self.view = ReviewContext(self.needs, self.evidence, self.references, self.executors,
            self.targets, self.ticket.scope, [], 'strict_v1', False, ())
        self.events = []

    def emit(self, kind, role, **fields):
        self.events.append({'type': kind, 'role': role, **fields})

    def validate(self, review, view=None, audit=None):
        return validate_review(review, view or self.view, audit=audit or ReviewAudit(), emit=self.emit)

    def routing(self, review=None, draft=None):
        reviewed = review or self.review
        return ReviewRoutingContext(draft or self.draft, reviewed, reviewed_needs(self.needs, reviewed.need_assessments),
            self.targets, 'diagnosis', 'strict_v1', 0, False, False)

    def uncertain(self, review, target='H1'):
        return review.model_copy(update={'assessments': [a.model_copy(update={'verdict': 'uncertain'})
            if a.target_id == target else a for a in review.assessments]})

    def test_validation_does_not_mutate_draft_needs_or_query_accounting(self):
        before = (self.draft.model_dump(), self.review.model_dump(),
                  {key: value.model_dump() for key, value in self.needs.items()}, dict(self.context.counts))
        result = self.validate(self.review)
        self.assertEqual(result, self.review)
        self.assertEqual((self.draft.model_dump(), self.review.model_dump(),
            {key: value.model_dump() for key, value in self.needs.items()}, self.context.counts), before)

    def test_missing_target_and_unknown_evidence_are_rejected(self):
        unknown = self.review.model_copy(update={'assessments': [self.review.assessments[0].model_copy(
            update={'evidence_ids': ['EV_MISSING']}), *self.review.assessments[1:]]})
        for review, expected in [(self.review.model_copy(update={'assessments': self.review.assessments[:-1]}),
                                  'REVIEW_TARGET_COVERAGE'), (unknown, 'UNKNOWN_OR_DUPLICATE_EVIDENCE')]:
            with self.subTest(expected=expected), self.assertRaises(ProtocolError) as error:
                self.validate(review)
            self.assertEqual(error.exception.code, expected)

    def test_historical_or_missing_support_cannot_establish_a_current_cause(self):
        hypothesis = self.draft.hypotheses[0].model_copy(update={'support_refs': []})
        view = replace(self.view, targets={**self.targets, 'H1': hypothesis})
        result = self.validate(self.review, view)
        self.assertEqual(next(a for a in result.assessments if a.target_id == 'H1').verdict, 'uncertain')
        self.assertEqual(self.review.assessments[1].verdict, 'supported')
        self.assertTrue(any(e.get('code') == 'NO_CURRENT_SUPPORT' for e in self.events))

    def test_core_blocking_flag_is_ignored_without_closing_the_need(self):
        review = self.review.model_copy(update={'need_assessments': [a.model_copy(update={
            'verdict': 'uncertain', 'blocking': False}) if a.need_id == 'N2' else a
            for a in self.review.need_assessments]})
        result = self.validate(review)
        assessment = next(a for a in result.need_assessments if a.need_id == 'N2')
        self.assertEqual(assessment.verdict, 'uncertain')
        self.assertIsNone(assessment.blocking)
        self.assertEqual(self.needs['N2'].status, 'pending')

    def test_ignored_hints_remain_auditable_when_a_later_guard_fails(self):
        window = self.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        review = self.uncertain(self.review).model_copy(update={
            'request_evidence': EvidenceRequest(need_id='N2', target_id='H1',
                missing_observation='Current failure record', checks=[{'tool': 'get_service_metrics',
                    'args': {**window, 'start': (self.ticket.scope.start - timedelta(minutes=1)).isoformat(),
                             'metrics': ['db_cpu']}}]),
            'need_assessments': [a.model_copy(update={'verdict': 'uncertain'}) if a.need_id == 'N1'
                                else a for a in self.review.need_assessments]})
        before = dict(self.context.counts)
        audit = ReviewAudit()
        with self.assertRaises(ProtocolError) as error:
            self.validate(review, replace(self.view, require_blockers=True), audit)
        self.assertEqual(error.exception.code, 'REVIEW_BLOCKER_REQUIRED')
        self.assertEqual(len(audit.hint_status), 1)
        self.assertEqual(audit.hint_status[0]['tool'], 'get_service_metrics')
        self.assertEqual(self.context.counts, before)
        self.assertEqual(len(review.request_evidence.checks), 1)

    def test_trigger_scope_detachment_does_not_promote_a_verdict(self):
        view = replace(self.view, targets={**self.targets,
            'H1': self.draft.hypotheses[0].model_copy(update={'level': 'trigger'})})
        review = self.uncertain(self.review).model_copy(update={'request_evidence': EvidenceRequest(
            need_id='N2', target_id='H1', missing_observation='Trigger record')})
        result = self.validate(review, view)
        self.assertIsNone(result.request_evidence.need_id)
        self.assertEqual(result.request_evidence.target_id, 'H1')
        self.assertEqual(result.assessments, review.assessments)
        self.assertEqual(result.need_assessments, review.need_assessments)

    def test_optional_trigger_is_deferred_only_after_material_gaps_are_clear(self):
        trigger = Hypothesis(hypothesis_id='H2', cause='Unknown trigger', level='trigger',
                             support_refs=[], counter_refs=[], pending_checks=['Trigger record'])
        draft = self.draft.model_copy(update={'hypotheses': [*self.draft.hypotheses, trigger]})
        review = self.review.model_copy(update={'assessments': [*self.review.assessments,
            Assessment(target_id='H2', verdict='uncertain', reason='Trigger remains unknown')],
            'request_evidence': EvidenceRequest(target_id='H2', missing_observation='Trigger record')})
        view = self.routing(review, draft)
        handoff = resolve_handoff(view, reviewed_gaps=lambda: [])
        self.assertIsNone(handoff.request)
        self.assertEqual(handoff.deferred_request, review.request_evidence)
        self.assertIsNotNone(view.review.request_evidence)
        blocked = resolve_handoff(view, reviewed_gaps=lambda: ['Mechanism incomplete'])
        self.assertEqual(blocked.request, review.request_evidence)
        self.assertIsNone(blocked.deferred_request)
        uncertain_action = replace(view, review=self.uncertain(review, 'A1'))
        self.assertIsNotNone(resolve_handoff(uncertain_action, reviewed_gaps=lambda: []).request)

    def test_rework_denials_do_not_consult_budget_after_protocol_or_round_limit(self):
        view = self.routing()
        def forbidden(additional):
            self.fail('Budget must not be consulted after an earlier denial')
        self.assertEqual(decide_rework(replace(view, protocol_fallback_used=True), can_collect=forbidden).kind,
                         'protocol_stop')
        self.assertEqual(decide_rework(replace(view, reworks=2), can_collect=forbidden).kind, 'limit_stop')
        self.assertEqual(decide_rework(view, can_collect=lambda additional: False).kind, 'budget_stop')
        decision = decide_rework(view, can_collect=lambda additional: True)
        self.assertEqual(decision.kind, 'plan')
        self.assertFalse(decision.authorize_additional)

    def test_second_rework_requires_a_material_blocker_and_original_budget(self):
        blocker = ReviewBlocker(cause_level='mechanism', affected_targets=['H1'],
            missing_observation='Current failure record', decision_impact='Changes mechanism judgment',
            resolution='not_planned')
        review = self.uncertain(self.review).model_copy(update={
            'need_assessments': [a.model_copy(update={'verdict': 'uncertain', 'blocker': blocker})
                                if a.need_id == 'N2' else a for a in self.review.need_assessments],
            'request_evidence': EvidenceRequest(need_id='N2', target_id='H1', missing_observation='Failure record')})
        view = replace(self.routing(review), reworks=1)
        self.assertTrue(additional_rework_eligible(view))
        self.assertEqual(decide_rework(view, can_collect=lambda additional: False).kind, 'budget_stop')
        requested = []
        decision = decide_rework(view, can_collect=lambda additional: requested.append(additional) or True)
        self.assertEqual(requested, [True])
        self.assertTrue(decision.authorize_additional)
        self.assertFalse(additional_rework_eligible(replace(view, reworks=2)))

    def test_completion_requires_supported_content_and_no_mechanical_gaps(self):
        def reason(review, **gaps):
            return completion_reason(self.draft, review, 'diagnosis', diagnosis_gaps=gaps.get('diagnosis', []),
                observation_gaps=gaps.get('observation', []), task_gaps=[], blocking_gaps=[])
        self.assertEqual(reason(self.review), 'FINISHED')
        self.assertEqual(reason(self.review, diagnosis=['Missing hypotheses']), 'DIAGNOSIS_INCOMPLETE')
        self.assertEqual(reason(self.review, observation=['Missing health control']), 'REVIEW_INCOMPLETE')
        self.assertEqual(reason(self.uncertain(self.review, 'A1')), 'REVIEW_INCOMPLETE')
