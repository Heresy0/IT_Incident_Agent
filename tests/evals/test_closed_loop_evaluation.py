"""No network: evaluate boundaries, reference isolation and honest acceptance gates."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from config.incident import IncidentConfig
from evals.closed_loop_cases import (PRINCIPAL, SourceWorldProvider, acquired_key_observations,
                                     build_cases, fixed_draft, reference_targets)
from runtime.context import Limits, RunContext
from scripts.evaluate_autonomous_closed_loop import (main, parse_args, rework_metrics,
                                                     run_e2e, run_fixed_rework, run_review, score_review, summarize)
from tools.executor import ToolExecutor


class ReviewControlModel:
    """Free response routing only; the fixture oracle is kept outside this double."""
    json_output = True

    def __init__(self, judgments, challenges=None):
        self.judgments, self.challenges, self.inputs = judgments, challenges or {}, []

    def invoke(self, messages):
        from langchain_core.messages import AIMessage
        data = json.loads(messages[-1].content)
        self.inputs.append(data)
        refs = [option['reference_id'] for option in data['reference_options']][:1]
        items = []
        for target in data['targets']:
            if 'challenge_policy_version' in data:
                coverage = self.challenges.get(target['id'], 'covered')
                items.append({'target_id': target['id'], 'coverage': coverage,
                    'uncovered_assertion': 'The full component scope was not observed.' if coverage == 'uncovered' else '',
                    'missing_evidence': 'Business route observations covering the claimed component scope.' if coverage == 'uncovered' else '',
                    'refs': refs, 'reason': 'Free challenge wiring control, not a semantic score.'})
                continue
            field = 'decision' if target['review_kind'] == 'check_appropriateness' else 'relation'
            items.append({'target_id': target['id'], field: self.judgments[target['id']],
                          'refs': refs, 'issue': 'Free wiring control; not a semantic model judgment.'})
        return AIMessage(content=json.dumps({'items': items}), response_metadata={'finish_reason': 'stop'})


class ClosedLoopEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cases = build_cases()
        self.limits = Limits(model_calls=12, tool_calls=8, seconds=180)

    def executor(self, case):
        provider = SourceWorldProvider(case)
        context = RunContext(scope=provider.ticket().scope, limits=self.limits)
        executor = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
        for check in case.initial_checks:
            result = executor.execute(check['tool'], check['args'])
            self.assertNotEqual(result.status, 'error', case.case_id)
        return executor

    def test_case_coverage_and_reference_labels_are_separate_from_runtime_worlds(self):
        self.assertEqual(set(self.cases), {'probe_scope', 'compound_assertion', 'temporal_counterevidence',
            'contribution_and_uniqueness', 'future_manual_check', 'followup_available', 'followup_unavailable',
            'pool_probe_ambiguity'})
        self.assertGreaterEqual(len({c.observations['ticket']['scope']['service'] for c in self.cases.values()}), 2)
        for case in self.cases.values():
            provider = SourceWorldProvider(case)
            self.assertTrue(provider._data['synthetic'])
            self.assertFalse({'gold', 'reference', 'review_claims', 'review_actions', 'key_observation_ids',
                              'expected_relation', 'case_id', 'fault_profile'} & provider._data.keys())
            self.assertEqual(case.freeze()['reference_kind'], 'synthetic_source_constructed_provisional')
            self.assertEqual(case.freeze()['human_review'], 'pending')
            self.assertFalse(case.freeze()['external_blind_test'])

    def test_pool_probe_case_is_explicit_known_regression_without_sql_or_lock_observations(self):
        case = self.cases['pool_probe_ambiguity']
        self.assertEqual(case.scenario_origin, 'known_regression_from_v22_failure')
        self.assertTrue(all(c.scenario_origin == 'new_author_constructed_world'
                            for key, c in self.cases.items() if key != case.case_id))
        self.assertEqual([row['relation'] for row in case.review_claims], ['supported', 'partial', 'insufficient'])
        self.assertEqual(len(case.review_claims), 3)
        provider = SourceWorldProvider(case)
        self.assertFalse(any('sql' in name or 'lock' in name or 'hold' in name
                             for name in provider.binding.observations['metrics']))
        self.assertEqual(sorted(row['value'] for row in case.observations['metrics'] if row['metric'] == 'observed_6'), [35, 100])
        self.assertEqual({row['value'] for row in case.observations['metrics'] if row['metric'] == 'observed_2'}, {20})

    def test_all_fixed_drafts_resolve_only_actually_acquired_source_refs(self):
        from workflow.v2.contracts import parse_draft
        for case in self.cases.values():
            with self.subTest(case=case.case_id):
                executor = self.executor(case)
                raw, expected = fixed_draft(case, executor)
                draft, errors = parse_draft(raw, executor)
                self.assertFalse(errors)
                self.assertEqual(len(draft.claims), len(case.review_claims))
                self.assertEqual(len(draft.next_steps), len(case.review_actions))
                self.assertEqual(expected, reference_targets(case))
                self.assertNotIn('relation', json.dumps(raw))
                self.assertNotIn('decision', json.dumps(raw))

    def test_unseen_world_can_answer_unplanned_legal_followup(self):
        case = self.cases['followup_available']
        executor = self.executor(case)
        first_ids = {e.locator.rsplit('/', 1)[-1] for e in executor.evidence.values()}
        self.assertNotIn('local-detail', first_ids)
        scope = executor.scope.model_dump(mode='json')
        result = executor.execute('get_service_logs', {'start': scope['start'], 'end': scope['end'],
                                                      'category': 'configuration'})
        self.assertEqual(result.status, 'ok')
        self.assertTrue(any(e.locator.endswith('/local-detail') for e in result.evidence))
        rows = acquired_key_observations(case, {'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()]})
        self.assertEqual(rows['omitted_ids'], [])
        self.assertFalse(rows['human_verified'])

    def test_unavailable_world_does_not_invent_remote_observations(self):
        case = self.cases['followup_unavailable']
        executor = self.executor(case)
        scope = executor.scope.model_dump(mode='json')
        result = executor.execute('get_recent_changes', {k: scope[k] for k in ('start', 'end')})
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.error.code, 'SOURCE_NOT_CONFIGURED')
        self.assertFalse(case.acquisition_available)
        denied = executor.execute('get_service_owner', {'alias': 'unregistered-remote-service'})
        self.assertEqual(denied.error.code, 'SERVICE_DENIED')

    def test_fixed_review_runs_same_review_api_without_revealing_provisional_answers(self):
        case = self.cases['future_manual_check']
        judgments = {key: row['value'] for key, row in reference_targets(case).items()}
        model = ReviewControlModel(judgments)
        report = run_review(case, model, self.limits)
        self.assertNotIn('execution_error', report)
        self.assertEqual(len(model.inputs), 1)
        self.assertNotIn('provisional_reference', model.inputs[0])
        self.assertNotIn('human', model.inputs[0])
        self.assertEqual({row['observed'] for row in score_review(report)}, {'insufficient', 'appropriate'})
        self.assertTrue(all(row['exact_agreement'] for row in score_review(report)))

    def test_challenged_arm_preserves_primary_and_scores_final_overlay(self):
        case = self.cases['probe_scope']
        model = ReviewControlModel({'C1': 'supported', 'C2': 'supported'}, {'C1': 'uncovered'})
        report = run_review(case, model, self.limits, arm='challenged_review')
        self.assertEqual(report['primary_batch']['items'][0]['relation'], 'supported')
        self.assertEqual(report['item_reviews'][0]['relation'], 'partial')
        self.assertTrue(report['challenge_audit']['called'])
        self.assertFalse(report['challenge_audit']['is_ground_truth'])
        self.assertFalse(report['challenge_is_ground_truth'])
        self.assertEqual(len(model.inputs), 2)
        self.assertNotIn('primary_batch', model.inputs[1])
        self.assertTrue(all(item['exact_agreement'] for item in score_review(report)))

    def test_fixed_rework_uses_engine_helper_and_does_not_score_revised_text_with_old_labels(self):
        case = self.cases['followup_available']
        calls = []

        def fake(model, draft, reviews, executor, context, *, repairs, enable_challenge):
            calls.append((executor, enable_challenge))
            return draft, reviews, {'performed': True, 'changed_ids': ['C1'], 'withdrawn_ids': [],
                'revision_audit': [{'target_id': 'C1', 'operation': 'replace'}], 'failures': []}

        with patch('workflow.v2.engine.execute_review_rework', new=fake):
            report = run_fixed_rework(case, 'closed_loop', ReviewControlModel({'C1': 'insufficient'}), self.limits)
        self.assertNotIn('execution_error', report)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1])
        self.assertTrue(report['first_pass']['evidence'])
        self.assertEqual(report['rework_rounds'], 1)
        self.assertTrue(report['final_draft'])
        self.assertNotIn('provisional_reference', report)
        self.assertEqual(score_review(report), [])
        self.assertIn('pending_human_review', report['revised_claim_accuracy'])

    def test_score_distinguishes_false_accept_false_reject_and_unassessed(self):
        report = {'provisional_reference': {'C1': {'kind': 'factual_relation', 'value': 'partial'},
            'C2': {'kind': 'factual_relation', 'value': 'supported'},
            'C3': {'kind': 'factual_relation', 'value': 'supported'}},
            'item_reviews': [{'target_id': 'C1', 'protocol_status': 'valid', 'relation': 'supported'},
                             {'target_id': 'C2', 'protocol_status': 'valid', 'relation': 'insufficient'},
                             {'target_id': 'C3', 'protocol_status': 'not_run', 'relation': None}]}
        result = score_review(report)
        self.assertTrue(result[0]['false_accept'])
        self.assertTrue(result[1]['false_reject'])
        self.assertFalse(result[2]['false_reject'])
        self.assertEqual(result[2]['protocol_status'], 'not_run')

    def test_synthetic_gate_never_fabricates_human_pass_or_rollout_approval(self):
        items = [{'target_id': 'C1', 'kind': 'factual_relation', 'expected': 'supported', 'observed': 'supported',
                  'protocol_status': 'valid', 'false_accept': False, 'false_reject': False, 'exact_agreement': True},
                 {'target_id': 'C2', 'kind': 'factual_relation', 'expected': 'partial', 'observed': 'partial',
                  'protocol_status': 'valid', 'false_accept': False, 'false_reject': False, 'exact_agreement': True}]
        runs = [{'case_id': 'x', 'arm': 'review', 'repeat': n,
                 'metrics': {'review_controls': deepcopy(items), 'execution_error': None}} for n in (1, 2)]
        result = summarize(runs, execution_mode='live', expected_runs=2)
        self.assertEqual(result['synthetic_gate']['status'], 'passed')
        self.assertEqual(result['human_semantic_gate'], 'pending')
        self.assertEqual(result['rollout_recommendation'], 'hold')
        self.assertEqual(result['production_accuracy'], 'not_established')
        self.assertEqual(summarize(runs, execution_mode='scripted_control_only', expected_runs=2)['synthetic_gate']['status'], 'not_evaluated')
        runs[0]['metrics']['review_controls'][1].update(observed='supported', false_accept=True, exact_agreement=False)
        failed = summarize(runs, execution_mode='live', expected_runs=2)
        self.assertEqual(failed['provisional_false_accepts'], 1)
        self.assertEqual(failed['synthetic_gate']['status'], 'not_met')
        self.assertEqual(failed['groups_with_relation_changes'], 1)

    def test_missing_runs_and_no_repeats_cannot_pass_synthetic_stability_gate(self):
        result = summarize([], execution_mode='live', expected_runs=2)
        self.assertFalse(result['synthetic_gate']['conditions']['complete_execution'])
        self.assertFalse(result['synthetic_gate']['conditions']['repeated_relation_flip_rate_at_most_20_percent'])
        self.assertNotEqual(result['synthetic_gate']['status'], 'passed')

    def test_rework_metrics_identify_new_key_sources_and_keep_narrowing_human_pending(self):
        case = self.cases['followup_available']
        report = {'evidence': [{'evidence_id': 'E1', 'locator': 'get_service_logs/generic-failure'},
                              {'evidence_id': 'E2', 'locator': 'get_service_logs/local-detail'}],
            'first_pass': {'collection': [{'evidence_ids': ['E1']}]}, 'rework_rounds': 1,
            'reworks': [{'changed_ids': ['C1'], 'withdrawn_ids': ['C2'], 'revision_audit': [{'kind': 'narrow'}]}]}
        metrics = rework_metrics(case, report)
        self.assertEqual(metrics['key_observations_acquired_after_review'], ['local-detail'])
        self.assertEqual(metrics['changed_target_ids'], ['C1'])
        self.assertIsNone(metrics['effective_narrowing_human_judgment'])

    def test_e2e_arms_share_limits_and_use_explicit_engine_switch(self):
        calls = []

        def fake(model, provider, principal, *, context, event_log, enable_rework):
            calls.append((enable_rework, context.limits, provider.ticket().scope))
            return {'run_summary': context.summary(), 'evidence': []}

        with patch('workflow.v2.engine.investigate', new=fake):
            for arm in ('no_rework', 'closed_loop'):
                run_e2e(self.cases['followup_available'], arm, object(), self.limits)
        self.assertEqual([row[0] for row in calls], [False, True])
        self.assertEqual(calls[0][1:], calls[1][1:])

    def test_default_plan_is_free_and_secrets_are_hashed(self):
        config = IncidentConfig(api_key='private-key', base_url='https://private-endpoint', extra_body={'secret': 'hidden-extra'})
        with patch('scripts.evaluate_autonomous_closed_loop.IncidentConfig.from_file', return_value=config), \
             patch('agents.models.build_model', side_effect=AssertionError('model initialized')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--plan', '--cases', 'probe_scope']), 0)
        raw = output.getvalue()
        plan = json.loads(raw)
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paid_requests_sent'], 0)
        self.assertTrue(plan['not_original_v22_comparison'])
        for secret in ('private-key', 'private-endpoint', 'hidden-extra'):
            self.assertNotIn(secret, raw)

    def test_live_requires_shared_global_budget_and_valid_stage_arms(self):
        for args in (['--live'], ['--stage', 'review', '--arms', 'closed_loop'],
                     ['--stage', 'e2e', '--arms', 'review'], ['--cases', 'unknown']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)
        self.assertEqual(parse_args(['--stage', 'review'])[1].arms, ['review', 'challenged_review'])
        self.assertEqual(parse_args(['--stage', 'fixed_rework'])[1].arms, ['no_rework', 'closed_loop'])


if __name__ == '__main__':
    unittest.main()
