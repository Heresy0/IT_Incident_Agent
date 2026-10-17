"""Free artifact guards and same-report contract replay, without a model."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.closed_loop_cases import PRINCIPAL, SourceWorldProvider, build_cases, reference_targets
from runtime.context import Limits, RunContext
from scripts.compare_autonomous_controls import (ComparabilityError, compare_root, compare_stage, load_bundle,
    main, reproject_report, run_metrics)
from tools.executor import ToolExecutor
from workflow.v2.contracts import parse_draft
from workflow.v2.review import ReviewBatch, evidence_pack_version, parse_review


class ControlComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = build_cases()['followup_available']
        provider = SourceWorldProvider(self.case)
        context = RunContext(scope=provider.ticket().scope, limits=Limits(model_calls=12, tool_calls=8))
        context.emit = lambda event: None
        self.ex = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
        for check in self.case.initial_checks:
            self.ex.execute(check['tool'], check['args'])
        draft, errors = parse_draft({'claims': [{'statement': '固定模型判断的有限机制', 'refs': ['R1']}],
                                    'next_steps': [], 'gaps': ['触发条件仍未知']}, self.ex)
        self.assertEqual(errors, [])
        self.collection = [{'name': row['tool'], 'query_profile': row['args'], 'status': row['status'],
                            'truncated': row['truncated'], 'error': row['error'], 'evidence_ids': row['evidence_ids']}
                           for row in self.ex.check_results.values()]
        pack = evidence_pack_version(self.ex, collection=self.collection)
        items, errors = parse_review({'items': [{'target_id': 'C1', 'relation': 'supported',
            'refs': ['R1'], 'issue': '固定模型判定，非质量金标'}]}, draft, self.ex, evidence_pack_version=pack)
        self.assertEqual(errors, [])
        batch = ReviewBatch(items=list(items.values()), evidence_pack_version=pack, attempts=1)
        self.application = {'source_version': 'baseline', 'engine_version': 'autonomous_v2.3',
            'engine_source_sha256': 'old', 'review_policy_version': 'atomic_review_v2',
            'contract_version': 'atomic_draft_v1', 'challenge_policy_version': 'blind_scope_challenge_v1',
            'challenge_prompt_sha256': 'challenge-prompt', 'review_prompt_sha256': 'review-prompt',
            'review_source_sha256': 'review-source', 'challenge_source_sha256': 'challenge-source'}
        self.report = {'case_id': self.case.case_id, 'arm': 'closed_loop', 'repeat': 1,
            'status': 'partial', 'review_status': 'needs_information', 'execution_mode': 'live',
            'application': deepcopy(self.application), 'synthetic_reference': self.case.freeze(),
            'evidence': [item.model_dump(mode='json') for item in self.ex.evidence.values()],
            'collection': self.collection, 'drafts': [{'revision': 1, 'draft': draft.model_dump(mode='json')}],
            'reviews': [{'revision': 1, 'review': batch.model_dump(mode='json')}],
            'item_reviews': [item.model_dump(mode='json') for item in batch.items],
            'protocol_failures': [], 'events': [{'type': 'collection_completed', 'phase': 'collect',
                                                'reason': 'COLLECTION_BUDGET_EXHAUSTED'}],
            'run_summary': context.summary()}
        self.plan = {'version': 'closed-loop-evaluation-v1', 'stage': 'e2e', 'execution_mode': 'live',
            'cases': [self.case.case_id], 'arms': ['closed_loop'], 'repeats': 1, 'planned_runs': 1,
            'limits': asdict(context.limits), 'model_settings': {'model': 'qwen3.7-flash', 'temperature': 0},
            'configuration_sha256': 'frozen-config', 'dataset_version': 'controls-v1',
            'application': deepcopy(self.application), 'case_freezes': [self.case.freeze()],
            'evaluation_source_sha256': {'scripts/evaluate_autonomous_closed_loop.py': 'same-script',
                                         'evals/closed_loop_cases.py': 'same-cases'}}

    def bundle(self, name, *, plan=None, report=None):
        plan, report = deepcopy(plan or self.plan), deepcopy(report or self.report)
        report['application'] = plan['application']
        report['synthetic_reference'] = plan['case_freezes'][0]
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'report.json'
        path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        bundle = {'plan': plan, 'runs': [{'case_id': report['case_id'], 'arm': report['arm'],
            'repeat': report['repeat'], 'report_file': path.name,
            'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'metrics': {}}]}
        target = directory / 'bundle.json'
        target.write_text(json.dumps(bundle, ensure_ascii=False), encoding='utf-8')
        return target

    def compare(self, plan=None, report=None, **kwargs):
        return compare_stage(self.bundle('a'), self.bundle('b', plan=plan, report=report),
                             baseline_source=self.root / 'old-source', candidate_source=self.root / 'new-source', **kwargs)

    def test_source_and_engine_versions_can_differ_but_evidence_gain_remains_zero(self):
        plan = deepcopy(self.plan)
        plan['application'].update(source_version='candidate', engine_version='autonomous_v2.4', engine_source_sha256='new')
        report = deepcopy(self.report)
        report['status'] = 'completed'
        result = self.compare(plan, report)
        self.assertEqual(result['comparability'], 'passed')
        self.assertEqual(result['baseline']['completed'], 0)
        self.assertEqual(result['candidate']['completed'], 1)
        self.assertEqual(result['baseline']['key_observations_acquired'], result['candidate']['key_observations_acquired'])
        self.assertEqual(result['contract_reprojection']['empty_advice_gate_completion_gains'], 1)
        self.assertEqual(result['contract_reprojection']['projected_completed_among_available'], 1)
        self.assertEqual(result['contract_reprojection']['new_observations'], 0)
        self.assertEqual(result['human_semantic_gate'], 'pending')
        self.assertEqual(result['rollout_recommendation'], 'hold')

    def test_configuration_model_limits_and_world_changes_reject_comparison(self):
        for field, value in [('configuration_sha256', 'other-provider'), ('model_settings', {'model': 'qwen3.7-plus'}),
                             ('limits', {**self.plan['limits'], 'model_calls': 16}),
                             ('case_freezes', [{**self.case.freeze(), 'world_sha256': 'other-world'}])]:
            with self.subTest(field=field):
                plan = deepcopy(self.plan)
                plan[field] = value
                report = deepcopy(self.report)
                report['run_summary']['limits'] = plan['limits']
                with self.assertRaisesRegex(ComparabilityError, 'PLAN_MISMATCH:' + field):
                    self.compare(plan, report)

    def test_review_and_challenge_policy_changes_are_not_a_control_fix(self):
        for key in ['review_policy_version', 'review_prompt_sha256', 'challenge_prompt_sha256', 'contract_version']:
            with self.subTest(key=key):
                plan = deepcopy(self.plan)
                plan['application'][key] = 'changed'
                with self.assertRaisesRegex(ComparabilityError, 'POLICY_MISMATCH:' + key):
                    self.compare(plan)

    def test_only_verified_source_line_ending_difference_is_accepted(self):
        old = self.root / 'old-source' / 'app/workflow/v2/review.py'
        new = self.root / 'new-source' / 'app/workflow/v2/review.py'
        old.parent.mkdir(parents=True)
        new.parent.mkdir(parents=True)
        old.write_bytes(b'def review():\n    return True\n')
        new.write_bytes(b'def review():\r\n    return True\r\n')
        self.plan['application']['review_source_sha256'] = hashlib.sha256(old.read_bytes()).hexdigest()
        plan = deepcopy(self.plan)
        plan['application']['review_source_sha256'] = hashlib.sha256(new.read_bytes()).hexdigest()
        result = self.compare(plan)
        self.assertEqual(result['normalization_notes'][0]['difference'], 'line_endings_only')
        new.write_bytes(b'def review():\n    return False\n')
        plan['application']['review_source_sha256'] = hashlib.sha256(new.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ComparabilityError, 'CONTROL_SOURCE_CHANGED'):
            self.compare(plan)

    def test_missing_artifact_source_cannot_excuse_a_hash_difference(self):
        plan = deepcopy(self.plan)
        plan['evaluation_source_sha256']['evals/closed_loop_cases.py'] = 'different-source'
        with self.assertRaisesRegex(ComparabilityError, 'SOURCE_HASH_DIFFERENCE_WITHOUT_SOURCE'):
            self.compare(plan)

    def test_incomplete_repeat_set_cannot_silently_drop_a_failed_run(self):
        plan = deepcopy(self.plan)
        plan.update(repeats=2, planned_runs=2)
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_RUN_SET'):
            load_bundle(self.bundle('missing-repeat', plan=plan))

    def test_each_report_limits_and_integrity_are_verified(self):
        report = deepcopy(self.report)
        report['run_summary']['limits']['model_calls'] = 16
        with self.assertRaisesRegex(ComparabilityError, 'REPORT_LIMITS_MISMATCH'):
            load_bundle(self.bundle('wrong-limit', report=report))
        path = self.bundle('tampered')
        (path.parent / 'report.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ComparabilityError, 'REPORT_HASH_MISMATCH'):
            load_bundle(path)

    def test_scripted_reports_cannot_be_accepted_as_live_controls(self):
        self.plan['execution_mode'] = 'scripted_control_only'
        self.report['execution_mode'] = 'scripted_control_only'
        with self.assertRaisesRegex(ComparabilityError, 'LIVE_ARTIFACTS_REQUIRED'):
            self.compare()

    def test_contract_replay_does_not_invoke_a_model_or_add_observations(self):
        with patch('runtime.context.invoke_chat_model', side_effect=AssertionError('No model allowed')):
            result = reproject_report(self.report, self.case)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['projected_status'], 'completed')
        self.assertTrue(result['empty_advice_gate_completion_gain'])
        self.assertEqual(result['run_outcome']['investigation']['status'], 'limited')
        self.assertEqual(result['new_observations'], 0)
        self.assertEqual(result['paid_requests'], 0)
        self.assertIsNone(result['causal_accuracy_gain'])

    def test_replay_inconsistent_world_evidence_pack_or_facts_is_unavailable(self):
        changes = []
        bad_evidence = deepcopy(self.report)
        bad_evidence['evidence'][0]['hash'] = 'changed'
        changes.append((bad_evidence, 'REPLAY_EVIDENCE_MISMATCH'))
        bad_pack = deepcopy(self.report)
        bad_pack['reviews'][0]['review']['evidence_pack_version'] = 'stale'
        changes.append((bad_pack, 'REPLAY_REVIEW_PACK_MISMATCH'))
        bad_facts = deepcopy(self.report)
        bad_facts['drafts'][0]['draft']['facts'][0]['statement'] = 'Model invented source value'
        changes.append((bad_facts, 'REPLAY_FACTS_MISMATCH'))
        for report, reason in changes:
            with self.subTest(reason=reason):
                result = reproject_report(report, self.case)
                self.assertEqual(result['status'], 'not_available')
                self.assertEqual(result['reason'], reason)
                self.assertIsNone(result['projected_status'])

    def test_replay_preserves_unrecovered_failure_instead_of_declaring_contract_gain(self):
        report = deepcopy(self.report)
        report['protocol_failures'] = [{'stage': 'draft', 'category': 'shape', 'code': 'INVALID_JSON'}]
        result = reproject_report(report, self.case)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['projected_status'], 'partial')
        self.assertFalse(result['empty_advice_gate_completion_gain'])

    def test_budget_rejection_is_separate_from_calling_past_hard_cap(self):
        report = deepcopy(self.report)
        report['events'].append({'type': 'budget_rejected', 'dimension': 'model_calls'})
        report['run_summary']['model_calls'] = 12
        metrics = run_metrics(report, self.case, 'e2e')
        self.assertEqual(metrics['budget_rejections'], 1)
        self.assertEqual(metrics['hard_limit_violations'], [])
        report['run_summary']['model_calls'] = 13
        self.assertEqual(run_metrics(report, self.case, 'e2e')['hard_limit_violations'], ['model_calls'])

    def test_review_labels_are_checked_against_the_frozen_case(self):
        report = deepcopy(self.report)
        report['provisional_reference'] = reference_targets(self.case)
        metrics = run_metrics(report, self.case, 'review')
        self.assertTrue(metrics['review_controls'][0]['false_accept'])
        report['provisional_reference']['C1']['value'] = 'supported'
        with self.assertRaisesRegex(ComparabilityError, 'REVIEW_REFERENCE_MISMATCH'):
            run_metrics(report, self.case, 'review')

    def test_candidate_source_cannot_change_between_stages(self):
        (self.root / 'comparison-plan.json').write_text('{}', encoding='utf-8')
        common = {'comparability': 'passed', 'baseline_bundle': {'application': {'source_version': 'old'}},
                  'candidate_bundle': {'application': {'source_version': 'new'}},
                  'model_settings': {'model': 'same'}, 'configuration_sha256': 'same', 'limits': {'model_calls': 12}}
        stages = [deepcopy(common) for _ in range(3)]
        stages[1]['candidate_bundle']['application']['source_version'] = 'changed-after-e2e'
        with patch('scripts.compare_autonomous_controls._one_bundle', return_value=self.root / 'bundle.json'), \
             patch('scripts.compare_autonomous_controls.compare_stage', side_effect=stages):
            result = compare_root(self.root)
        self.assertEqual(result['comparability'], 'rejected')
        self.assertIn('CANDIDATE_APPLICATION_CHANGED_ACROSS_STAGES', result['cross_stage_errors'])
        self.assertEqual(result['synthetic_acceptance']['status'], 'not_met')

    def test_default_cli_is_a_free_plan_even_without_artifacts(self):
        with patch('builtins.print') as printer:
            self.assertEqual(main(['--root', str(self.root)]), 0)
        output = json.loads(printer.call_args.args[0])
        self.assertTrue(output['plan_only'])
        self.assertEqual(output['paid_requests_sent'], 0)

    def test_contract_change_cannot_hide_completion_regression_at_the_same_contract(self):
        (self.root / 'comparison-plan.json').write_text('{}', encoding='utf-8')
        summary = {'runs': 2, 'completed': 0, 'hard_limit_violation_runs': 0, 'execution_error_runs': 0,
                   'key_observations_acquired': 0, 'key_observations_omitted': 0,
                   'provisional_false_accepts': 0, 'unassessed_reference_items': 0}
        common = {'comparability': 'passed', 'baseline_bundle': {'application': {'source_version': 'old'}},
                  'candidate_bundle': {'application': {'source_version': 'new'}},
                  'model_settings': {'model': 'same'}, 'configuration_sha256': 'same', 'limits': {},
                  'baseline': deepcopy(summary), 'candidate': {**summary, 'completed': 1,
                      'key_observations_acquired': 1},
                  'contract_reprojection': {'available_runs': 2, 'projected_completed_among_available': 2}}
        with patch('scripts.compare_autonomous_controls._one_bundle', return_value=self.root / 'bundle.json'), \
             patch('scripts.compare_autonomous_controls.compare_stage', side_effect=[deepcopy(common) for _ in range(3)]):
            result = compare_root(self.root)
        gates = result['synthetic_acceptance']['conditions']
        self.assertTrue(gates['e2e_no_fewer_completed'])
        self.assertFalse(gates['e2e_no_fewer_completed_same_contract'])
        self.assertEqual(result['synthetic_acceptance']['status'], 'not_met')


if __name__ == '__main__':
    unittest.main()
