"""Free integrity, attribution, semantic-scope and usage-accounting checks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.closed_loop_cases import build_cases, reference_targets
from scripts.summarize_autonomous_roles import (ARMS, ROLES, FLASH, PLUS, ComparabilityError,
    arm_summary, distribution, estimate_cost, expected_model_maps, load_bundle, main,
    review_summary, run_metrics, source_utilization, summarize, summarize_stage,
    validate_comparability)


class RoleSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        app, evaluator = self.source / 'app/frozen.py', self.source / 'scripts/runner.py'
        app.parent.mkdir(parents=True)
        evaluator.parent.mkdir(parents=True)
        app.write_text('FROZEN = True\n', encoding='utf-8')
        evaluator.write_text('FROZEN_RUNNER = True\n', encoding='utf-8')
        self.case = build_cases()['followup_available']
        self.limits = {'model_calls': 12, 'tool_calls': 8, 'web_calls': 12, 'seconds': 180,
                       'input_chars': 48000}
        self.settings = {'temperature': 0, 'max_output_tokens': 2200}
        self.plan = {'version': 'autonomous-role-ablation-v1', 'stage': 'review',
            'execution_mode': 'live', 'cases': [self.case.case_id], 'arms': list(ARMS),
            'repeats': 2, 'planned_runs': 6, 'limits': self.limits,
            'model_settings': self.settings,
            'role_model_settings': {role: self.settings for role in ROLES},
            'model_maps': expected_model_maps(), 'provider_configuration_sha256': 'provider',
            'configuration_sha256': 'configuration', 'dataset_version': 'same-worlds',
            'application': {'source_version': 'frozen-app'},
            'app_source_sha256': {'app/frozen.py': self.sha(app)},
            'evaluation_source_sha256': {'scripts/runner.py': self.sha(evaluator)},
            'case_freezes': [self.case.freeze()]}

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def report(self, arm, repeat=1, *, stage='review'):
        reference = reference_targets(self.case)
        items = [{'target_id': target, 'protocol_status': 'valid', 'relation': value['value']}
                 for target, value in reference.items()]
        batch = {'items': items}
        role = 'reviewer'
        report = {'case_id': self.case.case_id, 'arm': arm, 'repeat': repeat,
            'stage': stage, 'execution_mode': 'live', 'status': 'completed',
            'application': deepcopy(self.plan['application']),
            'synthetic_reference': self.case.freeze(), 'model_map': expected_model_maps()[arm],
            'model_call_log': [{'role': role, 'model': expected_model_maps()[arm][role],
                'request_sha256': 'request', 'output_sha256': 'output', 'charged_request': True,
                'usage': {'input_tokens': 1000, 'output_tokens': 100}, 'seconds': 1.25}],
            'run_summary': {'limits': deepcopy(self.limits), 'model_calls': 1, 'tool_calls': 2,
                            'duration_ms': 1500},
            'evidence': [{'evidence_id': 'EV_LOCAL', 'locator': 'synthetic/log/local-detail'}],
            'final_draft': {'facts': [{'refs': [{'evidence_id': 'EV_LOCAL'}]}], 'claims': []},
            'protocol_failures': [], 'primary_batch': deepcopy(batch), 'review_batch': deepcopy(batch),
            'item_reviews': deepcopy(items), 'provisional_reference': reference}
        if stage == 'fixed_rework':
            report['first_pass_provisional_reference'] = report.pop('provisional_reference')
            report['first_pass'] = {'primary_batch': deepcopy(batch), 'review': deepcopy(batch)}
        return report

    def bundle(self, name, *, plan=None, mutate=None):
        plan = deepcopy(plan or self.plan)
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        entries = []
        for arm in plan['arms']:
            for repeat in range(1, plan['repeats'] + 1):
                report = self.report(arm, repeat, stage=plan['stage'])
                report['application'] = deepcopy(plan['application'])
                report['run_summary']['limits'] = deepcopy(plan['limits'])
                if mutate:
                    mutate(report)
                path = directory / f'{arm}-{repeat}.json'
                path.write_text(json.dumps(report), encoding='utf-8')
                entries.append({'case_id': self.case.case_id, 'arm': arm, 'repeat': repeat,
                    'report_file': path.name, 'report_sha256': self.sha(path), 'metrics': {}})
        path = directory / 'bundle.json'
        path.write_text(json.dumps({'plan': plan, 'runs': entries}), encoding='utf-8')
        return path

    def test_full_integrity_checked_summary_is_offline_and_never_opens_rollout(self):
        path = self.bundle('valid')
        with patch('agents.models.build_model', side_effect=AssertionError('No model')):
            result = summarize([path], source_root=self.source)
        self.assertEqual(result['comparability']['status'], 'passed')
        self.assertEqual(result['human_semantic_gate'], 'pending')
        self.assertEqual(result['rollout_recommendation'], 'hold')
        arm = result['stages']['review']['arms']['investigator_plus']
        self.assertEqual(arm['actual_plus_calls'], 0)
        self.assertFalse(arm['plus_benefit_attribution_supported_by_exposure'])
        self.assertEqual(result['stages']['review']['arms']['reviewer_plus']['actual_plus_calls'], 2)

    def test_modified_application_or_evaluator_source_rejects_comparison(self):
        for relative in ('app/frozen.py', 'scripts/runner.py'):
            with self.subTest(relative=relative):
                path = self.bundle(relative.replace('/', '-'))
                original = (self.source / relative).read_bytes()
                (self.source / relative).write_bytes(original + b'# changed\n')
                with self.assertRaisesRegex(ComparabilityError, 'SOURCE_HASH_MISMATCH'):
                    load_bundle(path, source_root=self.source)
                (self.source / relative).write_bytes(original)

    def test_unfrozen_new_application_file_is_rejected(self):
        path = self.bundle('new-file')
        (self.source / 'app/extra.py').write_text('NEW = 1\n', encoding='utf-8')
        with self.assertRaisesRegex(ComparabilityError, 'APP_SOURCE_SET_CHANGED'):
            load_bundle(path, source_root=self.source)

    def test_report_hash_and_complete_repeat_set_are_checked(self):
        path = self.bundle('hash')
        (path.parent / 'flash_all-1.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ComparabilityError, 'REPORT_HASH_MISMATCH'):
            load_bundle(path, source_root=self.source)
        path = self.bundle('incomplete')
        value = json.loads(path.read_text())
        value['runs'].pop()
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_RUN_SET'):
            load_bundle(path, source_root=self.source)

    def test_report_world_limits_and_actual_role_must_match_frozen_plan(self):
        mutations = [
            (lambda report: report['synthetic_reference'].update(world_sha256='changed'), 'REPORT_WORLD_MISMATCH'),
            (lambda report: report['run_summary']['limits'].update(model_calls=13), 'REPORT_LIMITS_MISMATCH'),
            (lambda report: report['model_call_log'][0].update(model=PLUS if report['arm'] == 'flash_all' else FLASH), 'ACTUAL_ROLE_MODEL_MISMATCH')]
        for index, (mutate, code) in enumerate(mutations):
            with self.subTest(code=code):
                with self.assertRaisesRegex(ComparabilityError, code):
                    load_bundle(self.bundle(str(index), mutate=mutate), source_root=self.source)

    def test_challenger_or_diagnosis_substitution_is_not_this_experiment(self):
        for role in ('challenger', 'diagnosis'):
            plan = deepcopy(self.plan)
            plan['model_maps']['reviewer_plus'][role] = PLUS
            with self.assertRaisesRegex(ComparabilityError, 'ROLE_MODEL_MAP_MISMATCH'):
                load_bundle(self.bundle(role, plan=plan), source_root=self.source)

    def test_shared_role_settings_must_be_identical(self):
        plan = deepcopy(self.plan)
        plan['role_model_settings']['investigation'] = {'temperature': 1}
        with self.assertRaisesRegex(ComparabilityError, 'ROLE_MODEL_SETTINGS_MISMATCH'):
            load_bundle(self.bundle('temperature', plan=plan), source_root=self.source)

    def test_cross_stage_limits_settings_provider_and_source_must_match(self):
        a = load_bundle(self.bundle('review'), source_root=self.source)
        for field, value in [('limits', {**self.limits, 'model_calls': 13}),
                             ('provider_configuration_sha256', 'other'),
                             ('model_settings', {'temperature': 1}),
                             ('application', {'source_version': 'other'})]:
            b = deepcopy(a)
            b['plan']['stage'] = 'fixed_rework'
            b['plan'][field] = value
            with self.assertRaisesRegex(ComparabilityError, 'PLAN_MISMATCH:' + field):
                validate_comparability([a, b])

    def test_original_reference_labels_cannot_be_rewritten_by_a_report(self):
        report = self.report('flash_all')
        report['provisional_reference']['C1']['value'] = 'supported'
        with self.assertRaisesRegex(ComparabilityError, 'REVIEW_REFERENCE_MISMATCH'):
            run_metrics(report, self.case, 'review')

    def test_execution_failure_is_retained_as_unassessed_instead_of_dropped(self):
        report = self.report('flash_all', stage='fixed_rework')
        report.update(execution_error={'code': 'MODEL_CALL_FAILED'}, status='failed')
        report.pop('first_pass_provisional_reference')
        report.pop('first_pass')
        report.pop('primary_batch')
        metrics = run_metrics(report, self.case, 'fixed_rework')
        self.assertTrue(metrics['execution_error'])
        self.assertFalse(metrics['completed'])
        self.assertEqual(metrics['primary_review_controls'][0]['protocol_status'], 'not_run')

    def test_revised_claim_is_never_scored_against_original_gold(self):
        report = self.report('flash_all', stage='fixed_rework')
        report['final_review'] = {'items': [{'target_id': 'C1', 'protocol_status': 'valid', 'relation': 'supported'}]}
        report['item_reviews'] = report['final_review']['items']
        metrics = run_metrics(report, self.case, 'fixed_rework')
        self.assertFalse(metrics['final_review_controls'][0]['false_accept'])
        self.assertEqual(metrics['final_review_controls'][0]['observed'], 'insufficient')
        report['first_pass']['review']['items'][0]['relation'] = 'supported'
        self.assertTrue(run_metrics(report, self.case, 'fixed_rework')['final_review_controls'][0]['false_accept'])

    def test_acquired_key_record_in_program_facts_does_not_count_as_claim_use(self):
        report = self.report('flash_all', stage='e2e')
        first = source_utilization(self.case, report)
        self.assertEqual(first['acquired_count'], 1)
        self.assertEqual(first['cited_by_final_claim_refs_ids'], [])
        report['final_draft']['claims'] = [{'refs': [{'evidence_id': 'EV_LOCAL'}], 'counter_refs': []}]
        second = source_utilization(self.case, report)
        self.assertEqual(second['cited_by_final_claim_refs_ids'], ['local-detail'])
        self.assertTrue(second['citation_does_not_establish_causal_use_or_accuracy'])

    def test_flip_groups_do_not_mix_arms_and_include_protocol_changes_separately(self):
        rows = []
        for arm, relations in [('flash_all', ['supported', 'supported']),
                               ('reviewer_plus', ['insufficient', 'insufficient'])]:
            for repeat, relation in enumerate(relations, 1):
                report = self.report(arm, repeat)
                report['review_batch']['items'][0]['relation'] = relation
                rows.append({'arm': arm, 'case_id': self.case.case_id, 'metrics': run_metrics(report, self.case, 'review')})
        result = review_summary(rows, 'final_review_controls')
        self.assertEqual(result['repeated_groups'], 2)
        self.assertEqual(result['groups_with_relation_flips'], 0)
        rows[-1]['metrics']['final_review_controls'][0].update(protocol_status='not_run', observed=None)
        result = review_summary(rows, 'final_review_controls')
        self.assertEqual(result['fully_assessed_repeated_groups'], 1)
        self.assertEqual(result['groups_with_relation_or_protocol_changes'], 1)

    def test_unknown_usage_is_not_zero_and_unpaid_attempt_is_not_plus_exposure(self):
        report = self.report('reviewer_plus')
        report['model_call_log'][0].update(usage=None)
        metrics = run_metrics(report, self.case, 'review')
        self.assertIsNone(metrics['input_tokens'])
        self.assertEqual(metrics['usage_unknown_calls'], 1)
        report['model_call_log'][0].update(charged_request=False, error={'code': 'PAID_BUDGET_CLOSED'})
        metrics = run_metrics(report, self.case, 'review')
        self.assertEqual(metrics['actual_plus_call_count'], 0)
        self.assertEqual(metrics['usage_unknown_calls'], 0)
        self.assertEqual(metrics['unpaid_admission_rejections'], 1)

    def test_distribution_reports_known_counts_and_small_sample_p95(self):
        result = distribution([1, None, 3, 2])
        self.assertEqual(result['known'], 3)
        self.assertEqual(result['unknown'], 1)
        self.assertEqual(result['median'], 2)
        self.assertEqual(result['p95'], 3)
        self.assertTrue(result['small_sample'])
        self.assertIsNone(distribution([None])['sum'])

    def rate_card(self):
        return {'currency': 'CNY', 'pricing_scope': {'region': 'china_beijing'},
            'effective_date': '2026-10-17', 'source_url': 'https://example.test/official-pricing',
            'models': {FLASH: {'input_per_million': .2, 'output_per_million': .8, 'max_input_tokens': 32000},
                       PLUS: {'input_per_million': 2, 'output_per_million': 8, 'max_input_tokens': 256000}}}

    def test_cost_requires_matching_public_scope_and_tracks_token_tiers(self):
        calls = self.report('flash_all')['model_call_log']
        rate = self.rate_card()
        self.assertIsNone(estimate_cost(calls, rate)['estimated_total'])
        result = estimate_cost(calls, rate, pricing_scope=rate['pricing_scope'])
        self.assertAlmostEqual(result['estimated_total'], .00028)
        self.assertFalse(result['is_actual_vendor_bill'])
        calls[0]['usage']['input_tokens'] = 32001
        result = estimate_cost(calls, rate, pricing_scope=rate['pricing_scope'])
        self.assertEqual(result['input_tier_unmatched_calls'], 1)
        self.assertIsNone(result['estimated_total'])
        calls[0]['usage'] = None
        result = estimate_cost(calls, rate, pricing_scope=rate['pricing_scope'])
        self.assertEqual(result['unknown_usage_calls'], 1)
        self.assertIsNone(result['estimated_total'])

    def test_rework_reason_and_text_only_revision_are_reported_per_case(self):
        def mutate(report):
            report['reworks'] = [{'performed': report['arm'] != 'reviewer_plus',
                'reason': 'NO_CURRENT_REVIEW_OBJECTION' if report['arm'] == 'reviewer_plus' else 'REWORK_LIMIT_REACHED',
                'new_observation_ids': [], 'changed_ids': ['C1'], 'withdrawn_ids': []}]
        plan = deepcopy(self.plan)
        plan['stage'] = 'fixed_rework'
        result = summarize_stage(load_bundle(self.bundle('fixed', plan=plan, mutate=mutate), source_root=self.source))
        self.assertEqual(result['cases'][self.case.case_id]['reviewer_plus']['rework']['performed_runs'], 0)
        self.assertEqual(result['arms']['flash_all']['rework']['text_revision_without_new_observation_rounds'], 2)
        self.assertEqual(result['arms']['reviewer_plus']['rework']['stop_reason_counts'], {'NO_CURRENT_REVIEW_OBJECTION': 2})

    def test_cli_can_write_offline_summary_and_reject_tampered_artifact(self):
        path = self.bundle('cli')
        target = self.root / 'summary.json'
        args = ['--bundle', str(path), '--source-root', str(self.source), '--output', str(target)]
        self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(target.read_text())['rollout_recommendation'], 'hold')
        (path.parent / 'flash_all-1.json').write_text('{}')
        self.assertEqual(main(args), 2)


if __name__ == '__main__':
    unittest.main()
