"""No-network checks of paired budget analysis and semantic boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.summarize_budget_matrix import (ARMS, FACTORS, ComparabilityError,
    build_blind_packets, control_summary, decision_metrics, factorial_effects,
    join_blind_audit, load_bundle, original_controls, run_metrics, summarize,
    validate_comparability)


class BudgetMatrixSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'snapshot'
        app = source / 'app/native.py'
        app.parent.mkdir(parents=True)
        app.write_text('FROZEN_APP = True\n', encoding='utf-8')
        self.application = {'source_root': str(source), 'app_root': str(source / 'app'),
            'source_version': 'exact-app-version', 'app_source_sha256': {'app/native.py': self.sha(app)}}
        evaluator = self.root / 'scripts/runner.py'
        evaluator.parent.mkdir()
        evaluator.write_text('FIXED_PROMPTS_AND_ADAPTER = True\n', encoding='utf-8')
        base = {'model_calls': 12, 'tool_calls': 8, 'web_calls': 12, 'seconds': 180,
            'reserve_model_calls': 2, 'reserve_seconds': 15, 'input_chars': 48000}
        configs = {arm: {'limits': {**base, 'model_calls': models}, 'max_new_reads': reads}
            for arm, (models, reads) in FACTORS.items()}
        self.world = {'case_id': 'source_available', 'world_sha256': 'world-sha',
            'metadata_sha256': 'metadata-sha', 'key_observation_ids': ['key'],
            'eligible_for_positive_acquisition_rate': True,
            'eligible_for_observation_coverage_rate': True,
            'stage_acquisition': {'fixed_rework': {'expected_new_key_observation_ids': ['key'],
                'eligible_for_positive_new_acquisition_rate': True}}}
        self.target = {'target_id': 'C1', 'kind': 'factual_relation',
            'original_text_sha256': hashlib.sha256('原始命题'.encode()).hexdigest(),
            'expected_accept': False, 'reference_relation': 'insufficient'}
        self.plan = {'version': 'budget-matrix-v1', 'execution_mode': 'live', 'stage': 'fixed_rework',
            'arms': list(ARMS), 'arm_configs': configs, 'application': self.application,
            'model': 'qwen3.7-flash', 'model_settings': {'temperature': 0},
            'provider_configuration_sha256': 'fixed-provider', 'jobs': 4,
            'evaluation_source_sha256': {'scripts/runner.py': self.sha(evaluator)},
            'cases': ['source_available'], 'repeats': 2, 'planned_runs': 8,
            'world_freezes': [self.world], 'review_targets': {'source_available': [self.target]},
            'adapter_policy': {'changed': 'review_rework_max_new_reads_only'}}

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def report(self, arm=ARMS[0], repeat=1, stage='fixed_rework'):
        limits = deepcopy(self.plan['arm_configs'][arm]['limits'])
        control = {'target_id': 'C1', 'kind': 'factual_relation',
            'original_text_sha256': self.target['original_text_sha256'],
            'native_verdict': 'insufficient', 'accept': False, 'protocol_status': 'valid'}
        return {'stage': stage, 'case_id': 'source_available', 'arm': arm, 'repeat': repeat,
            'execution_mode': 'live', 'status': 'completed', 'application': deepcopy(self.application),
            'world_freeze': deepcopy(self.world), 'observed_world_sha256': self.world['world_sha256'],
            'observed_capability_sha256': self.world['metadata_sha256'], 'arm_limits': limits,
            'loaded_application_modules': {'native': str(Path(self.application['app_root']) / 'native.py')},
            'model_settings': deepcopy(self.plan['model_settings']),
            'provider_configuration_sha256': self.plan['provider_configuration_sha256'],
            'run_summary': {'limits': deepcopy(limits), 'model_calls': 2, 'tool_calls': 2,
                'duration_ms': 1234, 'termination_reason': 'FINISHED'},
            'worker_wall_seconds': 2.0, 'initial_observation_ids': ['initial'],
            'decision_trace': [{'stage': 'collect', 'step_json': {'kind': 'query'}},
                {'stage': 'collect', 'step_json': {'kind': 'finish'}}],
            'native_report': {'events': [{'type': 'query_reused', 'role': 'investigation',
                    'reason': 'QUERY_ALREADY_ATTEMPTED'},
                {'type': 'collection_completed', 'phase': 'review_rework', 'reason': 'FINISHED'}]},
            'budget_adapter_audit': {'policy': deepcopy(self.plan['adapter_policy']), 'arm': arm,
                'max_new_reads': FACTORS[arm][1], 'calls': [{'phase': 'review_rework',
                    'native_max_new_reads': 2, 'effective_max_new_reads': FACTORS[arm][1]}]},
            'model_call_log': [{'model': 'qwen3.7-flash', 'charged_request': True,
                'request_sha256': 'request-hash', 'output_sha256': 'response-hash',
                'usage': {'input_tokens': 1000, 'output_tokens': 100}, 'seconds': .5}],
            'normalized': {'observations': [
                {'observation_id': 'initial', 'evidence_id': 'EV0', 'kind': 'observation', 'payload': {'initial': True}},
                {'observation_id': 'key', 'evidence_id': 'EV1', 'kind': 'observation', 'payload': {'message': 'local-detail'}},
                {'observation_id': 'key', 'evidence_id': 'EV2', 'kind': 'observation', 'payload': {'message': 'local-detail'}}],
                'queries': [], 'failures': [], 'coverage': {'normalization_complete': True},
                'final_claims': [{'id': 'C1', 'statement': '修订的新命题', 'refs': [{'evidence_id': 'EV1'}],
                    'published_stance': 'accepted'}],
                'review_controls_primary': [deepcopy(control)], 'review_controls_first_pass': [deepcopy(control)],
                'audit_items': [
                    {'id': 'F1', 'kind': 'finding', 'text': '程序生成来源记录', 'refs': [{'evidence_id': 'EV0'}]},
                    {'id': 'C1', 'kind': 'hypothesis', 'text': '修订的新命题', 'condition': '',
                        'refs': [{'evidence_id': 'EV1'}], 'published_stance': 'accepted'},
                    {'id': 'A1', 'kind': 'action', 'text': '检查配置', 'condition': '若获授权', 'refs': [],
                        'native_approval': {'submitted_decision': 'appropriate', 'protocol_status': 'valid'}}]}}

    def bundle(self, name, *, stage='fixed_rework', mutate=None, plan_mutate=None):
        directory = self.root / name
        directory.mkdir()
        plan = deepcopy(self.plan)
        plan['stage'] = stage
        if plan_mutate:
            plan_mutate(plan)
        entries = []
        for repeat in range(1, plan['repeats'] + 1):
            for arm in ARMS:
                report = self.report(arm, repeat, stage)
                if mutate:
                    mutate(report)
                path = directory / f'{arm}-{repeat}.json'
                self.write(path, report)
                entries.append({'case_id': report['case_id'], 'arm': arm, 'repeat': repeat, 'stage': stage,
                    'report_file': path.name, 'report_sha256': self.sha(path)})
        plan['schedule'] = [{key: row[key] for key in ('case_id', 'arm', 'repeat')} for row in entries]
        path = directory / 'bundle.json'
        self.write(path, {'plan': plan, 'runs': entries})
        return path

    def test_complete_four_arm_both_stage_matrix(self):
        paths = [self.bundle('e2e', stage='e2e'), self.bundle('fixed')]
        result = summarize(paths, evaluator_root=self.root)
        self.assertEqual(result['comparability']['status'], 'passed')
        self.assertFalse(result['default_budget_changed'])
        self.assertEqual(result['production_accuracy'], 'not_established')
        self.assertEqual(result['stages']['fixed_rework']['arms'][ARMS[0]]['new_key_records_acquired'], 2)

    def test_missing_stage_or_arm_or_repeat_is_not_silently_accepted(self):
        path = self.bundle('only-fixed')
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_PRESPECIFIED_STAGE_SET'):
            summarize([path], evaluator_root=self.root)
        value = json.loads(path.read_text())
        value['runs'].pop()
        self.write(path, value)
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_FOUR_ARM_RUN_SET'):
            load_bundle(path, evaluator_root=self.root)

    def test_source_evaluator_report_or_module_origin_tamper_rejected(self):
        path = self.bundle('hashes')
        app = Path(self.application['app_root']) / 'native.py'
        app.write_text('ALTERED = True\n')
        with self.assertRaisesRegex(ComparabilityError, 'SOURCE_HASH_MISMATCH'):
            load_bundle(path, evaluator_root=self.root)
        app.write_text('FROZEN_APP = True\n')
        extra = app.parent / 'extra.py'
        extra.write_text('EXTRA=True\n')
        with self.assertRaisesRegex(ComparabilityError, 'APP_SOURCE_SET_CHANGED'):
            load_bundle(path, evaluator_root=self.root)
        extra.unlink()
        (self.root / 'scripts/runner.py').write_text('CHANGED=True\n')
        with self.assertRaisesRegex(ComparabilityError, 'SOURCE_HASH_MISMATCH'):
            load_bundle(path, evaluator_root=self.root)

    def test_world_application_limit_model_or_settings_change_rejected(self):
        changes = [
            (lambda report: report['world_freeze'].update(world_sha256='changed'), 'REPORT_WORLD_MISMATCH'),
            (lambda report: report['application'].update(source_version='changed'), 'REPORT_APPLICATION_MISMATCH'),
            (lambda report: report['run_summary']['limits'].update(model_calls=99), 'REPORT_LIMITS_MISMATCH'),
            (lambda report: report['model_call_log'][0].update(model='plus'), 'ACTUAL_MODEL_MISMATCH'),
            (lambda report: report['model_settings'].update(temperature=1), 'REPORT_SETTINGS_MISMATCH'),
            (lambda report: report.update(observed_world_sha256='different'), 'WORKER_OBSERVED_WORLD_MISMATCH'),
            (lambda report: report['loaded_application_modules'].update(native=str(self.root / 'outside.py')), 'WORKER_APPLICATION_IMPORT_ESCAPED')]
        for index, (mutate, code) in enumerate(changes):
            with self.subTest(code=code), self.assertRaisesRegex(ComparabilityError, code):
                load_bundle(self.bundle('tamper-' + str(index), mutate=mutate), evaluator_root=self.root)

    def test_only_two_budget_factors_may_differ(self):
        for index, (field, value) in enumerate([('tool_calls', 9), ('seconds', 200), ('input_chars', 96000)]):
            with self.subTest(field=field), self.assertRaisesRegex(ComparabilityError, 'UNDECLARED_BUDGET_DIFFERENCE'):
                load_bundle(self.bundle('extra-' + str(index), plan_mutate=lambda plan:
                    plan['arm_configs']['m16_r4']['limits'].update({field: value})), evaluator_root=self.root)

    def test_budget_adapter_may_override_only_declared_rework_read_limit(self):
        for index, (mutate, code) in enumerate([
            (lambda report: report['budget_adapter_audit'].update(max_new_reads=8), 'BUDGET_ADAPTER_MISMATCH'),
            (lambda report: report['budget_adapter_audit']['calls'][0].update(phase='collect'), 'UNDECLARED_ADAPTER_OVERRIDE'),
            (lambda report: report['budget_adapter_audit']['calls'][0].update(native_max_new_reads=3), 'UNDECLARED_ADAPTER_OVERRIDE')]):
            with self.subTest(code=code), self.assertRaisesRegex(ComparabilityError, code):
                load_bundle(self.bundle('adapter-' + str(index), mutate=mutate), evaluator_root=self.root)

    def test_free_smoke_explicit_opt_in_never_silently_looks_live(self):
        path = self.bundle('free', plan_mutate=lambda plan: plan.update(execution_mode='free_smoke'),
            mutate=lambda report: report.update(execution_mode='free_smoke'))
        with self.assertRaisesRegex(ComparabilityError, 'LIVE_ARTIFACTS_REQUIRED'):
            load_bundle(path, evaluator_root=self.root)
        self.assertEqual(load_bundle(path, evaluator_root=self.root, allow_free_smoke=True)['plan']['execution_mode'], 'free_smoke')

    def test_shared_cross_stage_fields_and_prespecified_full_cases_verified(self):
        fixed = load_bundle(self.bundle('shared-fixed'), evaluator_root=self.root)
        e2e = load_bundle(self.bundle('shared-e2e', stage='e2e'), evaluator_root=self.root)
        e2e['plan']['model_settings']['temperature'] = 1
        with self.assertRaisesRegex(ComparabilityError, 'PLAN_MISMATCH:model_settings'):
            validate_comparability([fixed, e2e])
        prespecified = {'stages': {'fixed_rework': {'cases': ['extra-case'], 'repeats': 2}}}
        with self.assertRaisesRegex(ComparabilityError, 'PRESPECIFIED_RUN_SET_MISMATCH'):
            validate_comparability([fixed], prespecified)

    def test_duplicate_and_schedule_set_and_execution_order_verified(self):
        path = self.bundle('duplicate')
        value = json.loads(path.read_text())
        value['runs'][-1] = deepcopy(value['runs'][0])
        self.write(path, value)
        with self.assertRaisesRegex(ComparabilityError, 'DUPLICATE_RUN'):
            load_bundle(path, evaluator_root=self.root)
        path = self.bundle('order')
        value = json.loads(path.read_text())
        value['runs'].reverse()
        self.write(path, value)
        with self.assertRaisesRegex(ComparabilityError, 'EXECUTION_SCHEDULE_MISMATCH'):
            load_bundle(path, evaluator_root=self.root)

    def test_repeat_rate_uses_all_collection_decisions_not_tool_executions(self):
        report = self.report()
        report['decision_trace'].append({'stage': 'revise', 'role': 'investigation', 'step_json': {'changed': True}})
        result = decision_metrics(report)
        self.assertEqual(result['investigation_decision_attempts'], 2)
        self.assertEqual(result['repeated_or_covered_fraction'], .5)
        report['decision_trace'] = []
        with self.assertRaisesRegex(ComparabilityError, 'REPEATED_QUERY_DENOMINATOR_INVALID'):
            decision_metrics(report)

    def test_independent_observations_deduplicate_source_identity(self):
        metrics = run_metrics(self.report(), self.plan, self.world)
        self.assertEqual(metrics['new_independent_observations'], 1)
        self.assertEqual(metrics['new_key_source']['acquired_count'], 1)
        self.assertEqual(metrics['source']['cited_by_final_claim_refs_ids'], ['key'])
        self.assertIsNone(metrics['source']['local_mechanism_use'])

    def test_prefix_already_read_and_unavailable_keys_are_not_new_successes(self):
        report = self.report()
        report['initial_observation_ids'].append('key')
        metrics = run_metrics(report, self.plan, self.world)
        self.assertFalse(metrics['new_key_source']['eligible'])
        self.assertIsNone(metrics['new_key_source']['all_required_acquired'])
        self.assertEqual(metrics['source']['acquired_count'], 1)
        world = deepcopy(self.world)
        world['stage_acquisition']['fixed_rework'].update(expected_new_key_observation_ids=[],
            eligible_for_positive_new_acquisition_rate=False)
        metrics = run_metrics(self.report(), self.plan, world)
        self.assertFalse(metrics['new_key_source']['eligible'])

    def test_fixed_first_pass_does_not_score_revised_claim_with_old_label(self):
        report = self.report()
        metrics = run_metrics(report, self.plan, self.world)
        self.assertEqual(metrics['original_controls_first_pass'][0]['native_verdict'], 'insufficient')
        self.assertFalse(metrics['original_controls_first_pass'][0]['relation_mismatch'])
        report['normalized']['review_controls_first_pass'][0]['original_text_sha256'] = 'changed'
        with self.assertRaisesRegex(ComparabilityError, 'FIXED_TARGET_TEXT_CHANGED'):
            original_controls(report, self.plan)

    def test_missing_fixed_target_is_unassessed_not_correct_and_confusion_visible(self):
        report = self.report()
        report['normalized']['review_controls_first_pass'] = []
        result = control_summary(original_controls(report, self.plan))
        self.assertEqual(result['unassessed_items'], 1)
        self.assertEqual(result['relation_mismatches'], 0)
        report['normalized']['review_controls_first_pass'] = [{**report['normalized']['review_controls_primary'][0],
            'native_verdict': 'contradicted'}]
        result = control_summary(original_controls(report, self.plan))
        self.assertEqual(result['missing_support_misclassified_as_contradiction'], 1)
        self.assertEqual(result['false_accepts'], 0)
        partial = deepcopy(self.plan)
        partial['review_targets']['source_available'][0]['reference_relation'] = 'partial'
        self.assertEqual(control_summary(original_controls(report, partial))[
            'missing_support_misclassified_as_contradiction'], 1)

    def test_unknown_usage_and_latency_are_not_filled_with_zero(self):
        report = self.report()
        report['model_call_log'][0]['usage'] = None
        report.pop('worker_wall_seconds')
        metrics = run_metrics(report, self.plan, self.world)
        self.assertIsNone(metrics['input_tokens'])
        self.assertIsNone(metrics['estimated_cost_CNY'])
        self.assertEqual(metrics['unknown_usage_calls'], 1)
        self.assertIsNone(metrics['worker_wall_seconds'])

    def test_factorial_main_effect_and_interaction_are_within_case_repeat(self):
        rows = []
        for arm, value in zip(ARMS, (1, 3, 4, 10)):
            metrics = run_metrics(self.report(arm), self.plan, self.world)
            metrics['tool_calls'] = value
            rows.append({'case_id': 'same', 'repeat': 1, 'arm': arm, 'metrics': metrics})
        result = factorial_effects(rows)['blocks'][0]['effects']['tool_calls']
        self.assertEqual(result['model_limit_main_effect'], 5)
        self.assertEqual(result['local_read_main_effect'], 4)
        self.assertEqual(result['interaction_difference_in_differences'], 4)
        rows.pop()
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_FACTORIAL_BLOCK'):
            factorial_effects(rows)

    def make_packets(self, *, no_claims=False):
        def mutate(report):
            if no_claims:
                report['normalized']['audit_items'] = []
        bundle = load_bundle(self.bundle('audit', mutate=mutate), evaluator_root=self.root)
        public, private = build_blind_packets([bundle], rubric={'rules': ['source-bound']}, seed=3)
        packet_path, linkage_path = self.root / 'packets.json', self.root / 'linkage.json'
        self.write(packet_path, public)
        private['packets_sha256'] = self.sha(packet_path)
        self.write(linkage_path, private)
        audit = {'packets_sha256': private['packets_sha256'], 'rubric_sha256': public['rubric_sha256'],
            'blinded': True, 'human_verified': False, 'is_gold': False, 'reports': []}
        for packet in public['packets']:
            audit['reports'].append({'opaque_report_id': packet['opaque_report_id'],
                'audit_status': 'reviewed' if packet['items'] else 'no_claims', 'items': [
                    {'opaque_item_id': item['opaque_item_id'], 'audit_status': 'reviewed',
                        'source_support': 'partially_entailed', 'source_ids': [],
                        'local_mechanism_use': False, 'unknown_boundary_preserved': True}
                    for item in packet['items']]})
        judgment_path = self.root / 'judgments.json'
        self.write(judgment_path, audit)
        return public, private, audit, packet_path, linkage_path, judgment_path

    def test_blind_packets_cover_all_reports_but_only_model_hypotheses_and_actions(self):
        public, private, audit, packet, linkage, judgments = self.make_packets()
        self.assertEqual(len(public['packets']), 8)
        self.assertTrue(all(len(row['items']) == 2 for row in public['packets']))
        self.assertEqual(public['scope']['excluded_item_kinds'], {'finding': 8})
        text = json.dumps(public, ensure_ascii=False)
        for forbidden in ARMS + ('first_pass', 'published_stance', 'native_approval', 'report_path'):
            self.assertNotIn(forbidden, text)
        result = join_blind_audit(packet, linkage, judgments)
        for arm in ARMS:
            self.assertEqual(result['by_arm'][arm]['candidate_unsupported_acceptances'], 4)
            self.assertIsNone(result['by_arm'][arm]['semantic_accuracy'])

    def test_missing_blind_report_or_item_or_wrong_source_cannot_be_joined(self):
        public, private, audit, packet, linkage, judgments = self.make_packets()
        modified = deepcopy(audit)
        modified['reports'].pop()
        self.write(judgments, modified)
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_BLIND_REPORT_SET'):
            join_blind_audit(packet, linkage, judgments)
        modified = deepcopy(audit)
        modified['reports'][0]['items'].pop()
        self.write(judgments, modified)
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_BLIND_ITEM_SET'):
            join_blind_audit(packet, linkage, judgments)
        modified = deepcopy(audit)
        modified['reports'][0]['items'][0]['source_ids'] = ['guessed-hidden-source']
        self.write(judgments, modified)
        with self.assertRaisesRegex(ComparabilityError, 'AUDIT_SOURCE_ID_NOT_ACQUIRED'):
            join_blind_audit(packet, linkage, judgments)

    def test_no_claim_reports_explicitly_covered_and_not_semantic_successes(self):
        public, private, audit, packet, linkage, judgments = self.make_packets(no_claims=True)
        result = join_blind_audit(packet, linkage, judgments)
        self.assertEqual(result['by_arm'][ARMS[0]]['reports_with_no_claims'], 2)
        self.assertEqual(result['by_arm'][ARMS[0]]['native_accepted_assessed_items'], 0)
        self.assertIsNone(result['by_arm'][ARMS[0]]['semantic_accuracy'])
        audit['reports'][0]['audit_status'] = 'reviewed'
        self.write(judgments, audit)
        with self.assertRaisesRegex(ComparabilityError, 'EMPTY_REPORT_NOT_EXPLICITLY_COVERED'):
            join_blind_audit(packet, linkage, judgments)

    def test_audit_statement_condition_and_native_report_bytes_bound(self):
        public, private, audit, packet, linkage, judgments = self.make_packets()
        public['packets'][0]['items'][0]['condition'] = 'changed condition'
        self.write(packet, public)
        private['packets_sha256'] = self.sha(packet)
        audit['packets_sha256'] = self.sha(packet)
        self.write(linkage, private)
        self.write(judgments, audit)
        with self.assertRaisesRegex(ComparabilityError, 'AUDITED_ITEM_CONDITION_MISMATCH'):
            join_blind_audit(packet, linkage, judgments)


if __name__ == '__main__':
    unittest.main()
