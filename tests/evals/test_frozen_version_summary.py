"""Free paired-version integrity and interpretation-boundary tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.summarize_frozen_versions import (ARMS, ComparabilityError, arm_summary,
    binary_controls, binary_summary, build_blind_packets, classify_failures, distribution, estimate_cost,
    join_blind_audit,
    load_bundle, main, run_metrics, source_metrics, summarize, validate_comparability)


class FrozenVersionSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.applications = {}
        for arm in ARMS:
            source = self.root / arm
            app = source / 'app/native.py'
            app.parent.mkdir(parents=True)
            app.write_text(f'VERSION = {arm!r}\n', encoding='utf-8')
            self.applications[arm] = {'source_root': str(source), 'app_root': str(source / 'app'),
                'source_version': arm + '-frozen', 'app_source_sha256': {'app/native.py': self.sha(app)}}
        evaluator = self.root / 'scripts/runner.py'
        evaluator.parent.mkdir()
        evaluator.write_text('FROZEN_EVALUATOR = True\n', encoding='utf-8')
        self.world = {'case_id': 'source_available', 'world_sha256': 'fixed-world',
            'metadata_sha256': 'fixed-metadata',
            'key_observation_ids': ['key'], 'eligible_for_positive_acquisition_rate': True,
            'eligible_for_observation_coverage_rate': True, 'key_metric_kind': 'source_key_acquisition',
            'world_kind': 'source_world', 'matched_world_group': 'source_available'}
        self.target = {'target_id': 'C1', 'kind': 'factual_relation',
            'original_text_sha256': 'a' * 64, 'expected_accept': False,
            'reference_relation': 'insufficient'}
        self.limits = {'model_calls': 12, 'tool_calls': 8, 'web_calls': 12, 'seconds': 180,
                       'input_chars': 48000}
        self.plan = {'version': 'frozen-study-v1', 'execution_mode': 'live', 'stage': 'review',
            'arms': list(ARMS), 'applications': self.applications, 'model': 'qwen3.7-flash',
            'model_settings': {'temperature': 0}, 'provider_configuration_sha256': 'same-provider',
            'repeats': 2, 'planned_runs': 4, 'cases': ['source_available'], 'limits': self.limits,
            'evaluation_source_sha256': {'scripts/runner.py': self.sha(evaluator)},
            'world_freezes': [self.world], 'review_targets': {'source_available': [self.target]}}

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def report(self, arm='original', repeat=1):
        return {'case_id': 'source_available', 'arm': arm, 'repeat': repeat,
            'execution_mode': 'live', 'status': 'completed',
            'application': deepcopy(self.applications[arm]), 'world_freeze': deepcopy(self.world),
            'observed_world_sha256': self.world['world_sha256'], 'observed_capability_sha256': self.world['metadata_sha256'],
            'loaded_application_modules': {'native': str(Path(self.applications[arm]['app_root']) / 'native.py')},
            'native_report': {'native_verdict': 'uncertain'},
            'run_summary': {'limits': deepcopy(self.limits), 'model_calls': 1, 'tool_calls': 1,
                            'duration_ms': 1234},
            'model_call_log': [{'model': 'qwen3.7-flash', 'role': 'native', 'stage': 'review',
                'charged_request': True, 'request_sha256': 'request', 'output_sha256': 'output',
                'usage': {'input_tokens': 1000, 'output_tokens': 100}, 'seconds': 1.1}],
            'normalized': {'observations': [{'observation_id': 'key', 'evidence_id': 'EV_KEY',
                'locator': 'private/engine/current/key', 'kind': 'observation', 'payload': {'message': 'local validation failed'}}],
                'queries': [], 'final_claims': [{'id': 'native-C', 'statement': '原生命题',
                    'refs': [{'evidence_id': 'EV_KEY'}], 'published_stance': 'pending'}],
                'review_controls': [{'target_id': 'C1', 'native_target_id': 'H1' if arm == 'original' else 'C1',
                    'kind': 'factual_relation', 'original_text_sha256': 'a' * 64,
                    'native_verdict': 'uncertain' if arm == 'original' else 'insufficient',
                    'accept': False, 'protocol_status': 'valid'}],
                'audit_items': [{'id': 'H1' if arm == 'original' else 'C1', 'kind': 'hypothesis',
                    'text': '原生命题', 'condition': '核查条件', 'refs': [{'evidence_id': 'EV_KEY',
                        'field_path': 'message', 'value': 'local validation failed'}], 'counter_refs': [],
                    'native_approval': {'verdict': 'uncertain' if arm == 'original' else 'insufficient'}}],
                'failures': [], 'coverage': {'normalization_complete': True, 'unsupported_fields': []}}}

    def bundle(self, name, *, plan=None, mutate=None):
        plan = deepcopy(plan or self.plan)
        directory = self.root / name
        directory.mkdir()
        entries = []
        for arm in ARMS:
            for repeat in range(1, plan['repeats'] + 1):
                report = self.report(arm, repeat)
                report['run_summary']['limits'] = deepcopy(plan['limits'])
                if mutate:
                    mutate(report)
                path = directory / f'{arm}-{repeat}.json'
                path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
                entries.append({'case_id': report['case_id'], 'arm': arm, 'repeat': repeat,
                    'report_file': path.name, 'report_sha256': self.sha(path)})
        target = directory / 'bundle.json'
        target.write_text(json.dumps({'plan': plan, 'runs': entries}), encoding='utf-8')
        return target

    def test_full_snapshot_pairing_allows_different_application_bytes(self):
        result = summarize([self.bundle('valid')], evaluator_root=self.root)
        self.assertEqual(result['comparability']['status'], 'passed')
        self.assertEqual(result['comparability']['legacy_four_class_mapping'], 'not_performed')
        self.assertEqual(result['rollout_recommendation'], 'hold')
        self.assertIsNone(result['stages']['review']['arms']['original']['natural_claim_semantic_accuracy'])

    def test_either_snapshot_tamper_or_added_file_is_rejected(self):
        path = self.bundle('source-tamper')
        source = Path(self.applications['original']['app_root']) / 'native.py'
        original = source.read_bytes()
        source.write_bytes(original + b'# changed\n')
        with self.assertRaisesRegex(ComparabilityError, 'SOURCE_HASH_MISMATCH'):
            load_bundle(path, evaluator_root=self.root)
        source.write_bytes(original)
        extra = source.parent / 'extra.py'
        extra.write_text('EXTRA = True\n')
        with self.assertRaisesRegex(ComparabilityError, 'APP_SOURCE_SET_CHANGED'):
            load_bundle(path, evaluator_root=self.root)

    def test_evaluator_tamper_is_rejected(self):
        path = self.bundle('eval-tamper')
        (self.root / 'scripts/runner.py').write_text('DIFFERENT = True\n')
        with self.assertRaisesRegex(ComparabilityError, 'SOURCE_HASH_MISMATCH'):
            load_bundle(path, evaluator_root=self.root)

    def test_changed_world_native_application_or_budget_rejects_pair(self):
        changes = [(lambda report: report['world_freeze'].update(world_sha256='different'), 'REPORT_WORLD_MISMATCH'),
                   (lambda report: report['application'].update(source_version='different'), 'REPORT_APPLICATION_MISMATCH'),
                   (lambda report: report['run_summary']['limits'].update(model_calls=16), 'REPORT_LIMITS_MISMATCH')]
        for index, (mutate, code) in enumerate(changes):
            with self.subTest(code=code):
                with self.assertRaisesRegex(ComparabilityError, code):
                    load_bundle(self.bundle(str(index), mutate=mutate), evaluator_root=self.root)

    def test_wrong_actual_model_and_unknown_paid_admission_rejects_pair(self):
        for index, change in enumerate([{'model': 'qwen3.7-plus'}, {'charged_request': None}]):
            with self.assertRaises(ComparabilityError):
                load_bundle(self.bundle('model-' + str(index), mutate=lambda report: report['model_call_log'][0].update(change)), evaluator_root=self.root)

    def test_worker_must_load_only_its_own_snapshot_and_exact_world(self):
        changes = [
            (lambda report: report['loaded_application_modules'].update(native=str(Path(self.applications['current' if report['arm'] == 'original' else 'original']['app_root']) / 'native.py')), 'WORKER_APPLICATION_IMPORT_ESCAPED'),
            (lambda report: report.update(observed_world_sha256='other-world'), 'WORKER_OBSERVED_WORLD_MISMATCH')]
        for index, (mutate, code) in enumerate(changes):
            with self.subTest(code=code):
                with self.assertRaisesRegex(ComparabilityError, code):
                    load_bundle(self.bundle('worker-' + str(index), mutate=mutate), evaluator_root=self.root)

    def test_missing_repeat_or_tampered_report_cannot_be_dropped(self):
        path = self.bundle('missing')
        value = json.loads(path.read_text())
        value['runs'].pop()
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_PAIRED_RUN_SET'):
            load_bundle(path, evaluator_root=self.root)
        path = self.bundle('tamper')
        (path.parent / 'original-1.json').write_text('{}')
        with self.assertRaisesRegex(ComparabilityError, 'REPORT_HASH_MISMATCH'):
            load_bundle(path, evaluator_root=self.root)

    def test_cross_stage_shared_model_settings_provider_limits_must_match(self):
        a = load_bundle(self.bundle('base'), evaluator_root=self.root)
        for field, value in [('model', 'other'), ('model_settings', {'temperature': 1}),
                             ('provider_configuration_sha256', 'other'), ('limits', {'model_calls': 20})]:
            b = deepcopy(a)
            b['plan']['stage'] = 'e2e'
            b['plan'][field] = value
            with self.assertRaisesRegex(ComparabilityError, 'PLAN_MISMATCH:' + field):
                validate_comparability([a, b])

    def test_native_uncertain_is_withheld_binary_acceptance_without_four_class_label(self):
        report = self.report()
        controls = binary_controls(report, self.plan)
        self.assertEqual(controls[0]['native_verdict'], 'uncertain')
        self.assertFalse(controls[0]['accept'])
        self.assertIsNone(controls[0]['four_class_relation'])
        self.assertTrue(controls[0]['correct_binary_boundary'])
        positive_plan = deepcopy(self.plan)
        positive_plan['review_targets']['source_available'][0]['expected_accept'] = True
        self.assertTrue(binary_controls(report, positive_plan)[0]['false_reject_or_withheld'])

    def test_edited_fixed_target_text_is_not_scored_against_original_reference(self):
        report = self.report()
        report['normalized']['review_controls'][0]['original_text_sha256'] = 'b' * 64
        with self.assertRaisesRegex(ComparabilityError, 'FIXED_TARGET_TEXT_CHANGED'):
            binary_controls(report, self.plan)

    def test_missing_fixed_target_is_unassessed_not_correct(self):
        report = self.report()
        report['normalized']['review_controls'] = []
        controls = binary_controls(report, self.plan)
        rows = [{'arm': 'original', 'case_id': 'source_available', 'metrics': {'binary_controls': controls}}]
        result = binary_summary(rows)
        self.assertEqual(result['reference_items'], 1)
        self.assertEqual(result['unassessed_items'], 1)
        self.assertEqual(result['binary_boundary_agreement'], 0)

    def test_provider_errors_and_recovered_repairs_are_not_terminal_protocol_interruptions(self):
        report = self.report()
        report['normalized']['failures'] = [
            {'stage': 'query', 'category': 'provider', 'code': 'SOURCE_NOT_CONFIGURED', 'recovered': False, 'terminal': True},
            {'stage': 'review', 'category': 'model_protocol', 'code': 'INVALID_JSON', 'recovered': True, 'terminal': False}]
        result = classify_failures(report)
        self.assertEqual(result['provider_failures'], 1)
        self.assertEqual(result['recovered_model_protocol_failures'], 1)
        self.assertFalse(result['terminal_structured_interruption'])
        report['normalized']['failures'].append({'stage': 'draft', 'category': 'model_protocol',
            'code': 'INVALID_JSON', 'recovered': False, 'terminal': True})
        self.assertTrue(classify_failures(report)['terminal_structured_interruption'])

    def test_key_acquisition_claim_citation_and_semantic_use_stay_separate(self):
        report = self.report()
        result = source_metrics(report, self.world)
        self.assertEqual(result['acquired_count'], 1)
        self.assertEqual(result['cited_by_final_claim_refs_ids'], ['key'])
        self.assertIsNone(result['local_mechanism_use'])
        report['normalized']['final_claims'] = []
        result = source_metrics(report, self.world)
        self.assertEqual(result['acquired_count'], 1)
        self.assertEqual(result['cited_by_final_claim_refs_ids'], [])

    def test_fixture_coverage_and_unavailable_world_do_not_inflate_positive_acquisition(self):
        plan = deepcopy(self.plan)
        plan['stage'] = 'e2e'
        fixture = {**self.world, 'eligible_for_positive_acquisition_rate': False,
                   'key_metric_kind': 'prespecified_observation_coverage'}
        report = self.report()
        metrics = run_metrics(report, plan, fixture)
        rows = [{'metrics': metrics, 'report': report}]
        result = arm_summary(rows)
        self.assertEqual(result['positive_acquisition_eligible_runs'], 0)
        self.assertEqual(result['key_records_acquired'], 0)
        self.assertEqual(result['coverage_records_acquired'], 1)
        unavailable = {**fixture, 'key_observation_ids': [], 'eligible_for_observation_coverage_rate': False}
        self.assertIsNone(source_metrics(report, unavailable)['all_required_acquired'])

    def test_unknown_tokens_remain_unknown_and_unpaid_attempt_is_not_billed(self):
        report = self.report()
        report['model_call_log'][0]['usage'] = None
        metrics = run_metrics(report, self.plan, self.world)
        self.assertEqual(metrics['unknown_usage_calls'], 1)
        self.assertIsNone(metrics['input_tokens'])
        report['model_call_log'][0]['charged_request'] = False
        metrics = run_metrics(report, self.plan, self.world)
        self.assertEqual(metrics['actual_charged_calls'], 0)
        self.assertEqual(metrics['unknown_usage_calls'], 0)
        self.assertEqual(metrics['unpaid_invoke_attempts'], 1)

    def test_price_tiers_unknown_usage_and_scope_matching(self):
        scope = {'region': 'china_beijing'}
        rate = {'currency': 'CNY', 'pricing_scope': scope, 'effective_date': '2026-10-17',
            'source_url': 'https://example.test/pricing', 'models': {'qwen3.7-flash': {'tiers': [
                {'max_input_tokens': 32000, 'input_per_million': .2, 'output_per_million': .8},
                {'min_input_tokens_exclusive': 32000, 'max_input_tokens': 256000,
                 'input_per_million': .6, 'output_per_million': 2.4}]}}}
        calls = self.report()['model_call_log']
        self.assertIsNone(estimate_cost(calls, rate)['estimated_total'])
        self.assertAlmostEqual(estimate_cost(calls, rate, pricing_scope=scope)['estimated_total'], .00028)
        calls[0]['usage']['input_tokens'] = 32001
        self.assertAlmostEqual(estimate_cost(calls, rate, pricing_scope=scope)['estimated_total'], .0194406)
        calls[0]['usage']['input_tokens'] = 256001
        self.assertIsNone(estimate_cost(calls, rate, pricing_scope=scope)['estimated_total'])
        calls[0]['usage'] = None
        result = estimate_cost(calls, rate, pricing_scope=scope)
        self.assertEqual(result['unknown_usage_calls'], 1)
        self.assertIsNone(result['estimated_total'])

    def test_statistics_keep_small_samples_and_unknown_values_explicit(self):
        result = distribution([1, 3, None, 2])
        self.assertEqual((result['known'], result['unknown']), (3, 1))
        self.assertEqual((result['mean'], result['median'], result['p95']), (2, 2, 3))
        self.assertTrue(result['small_sample'])
        self.assertIsNone(distribution([None])['sum'])

    def test_cli_is_offline_and_retains_hold_and_completion_caveat(self):
        path = self.bundle('cli')
        target = self.root / 'summary.json'
        self.assertEqual(main(['--bundle', str(path), '--evaluator-root', str(self.root), '--output', str(target)]), 0)
        result = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(result['human_semantic_gate'], 'pending')
        self.assertEqual(result['rollout_recommendation'], 'hold')
        self.assertTrue(result['stages']['review']['arms']['original']['completed_is_not_accuracy'])

    def blind_artifacts(self, mutate=None):
        plan = deepcopy(self.plan)
        plan['stage'] = 'e2e'
        bundle = load_bundle(self.bundle('blind', plan=plan, mutate=mutate), evaluator_root=self.root)
        public, private = build_blind_packets([bundle], rubric={'rubric': 'source-only'}, seed=123)
        packet_path, map_path, judgments_path = [self.root / name for name in ('packet.json', 'private.json', 'judgments.json')]
        packet_path.write_text(json.dumps(public, ensure_ascii=False), encoding='utf-8')
        private['packets_sha256'] = self.sha(packet_path)
        map_path.write_text(json.dumps(private), encoding='utf-8')
        judgments = {'packets_sha256': self.sha(packet_path), 'rubric_sha256': public['rubric_sha256'],
            'blinded': True, 'human_verified': False, 'is_gold': False,
            'reports': [{'opaque_report_id': packet['opaque_report_id'], 'audit_status': 'reviewed',
                'items': [{'opaque_item_id': item['opaque_item_id'], 'audit_status': 'reviewed',
                           'source_support': 'fully_entailed', 'local_mechanism_use': True} for item in packet['items']]}
                        for packet in public['packets']]}
        judgments_path.write_text(json.dumps(judgments), encoding='utf-8')
        return public, private, judgments, packet_path, map_path, judgments_path

    def test_blind_projection_strips_native_identity_and_stance_preserving_exact_words(self):
        public, private, _, _, _, _ = self.blind_artifacts()
        forbidden = {'arm', 'model', 'engine', 'source_version', 'report_path', 'run_id',
                     'native_target_id', 'status', 'review_verdict', 'published_stance', 'native_approval'}
        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(child) for child in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(child) for child in value))
            return set()
        # Query quality's status is allowed; no queries in this focused fixture.
        self.assertFalse(keys(public) & forbidden)
        packet = public['packets'][0]
        self.assertEqual(packet['items'][0]['original_text'], '原生命题')
        self.assertEqual(packet['items'][0]['condition'], '核查条件')
        self.assertEqual(packet['items'][0]['support_refs'][0]['value'], 'local validation failed')
        self.assertTrue(packet['items'][0]['support_refs'][0]['opaque_source_id'].startswith('S'))
        self.assertTrue(private['do_not_provide_to_blind_auditor'])
        self.assertEqual(len(private['reports']), 4)

    def test_blind_join_requires_complete_hash_bound_report_and_item_coverage(self):
        _, _, judgments, packet, linkage, path = self.blind_artifacts()
        joined = join_blind_audit(packet, linkage, path)
        self.assertFalse(joined['human_verified'])
        self.assertFalse(joined['is_gold'])
        self.assertEqual(joined['by_arm']['original']['assessed_items'], 2)
        self.assertEqual(joined['by_arm']['current']['local_mechanism_use_items'], 2)
        judgments['reports'][0]['items'] = []
        path.write_text(json.dumps(judgments))
        with self.assertRaisesRegex(ComparabilityError, 'INCOMPLETE_BLIND_ITEM_SET'):
            join_blind_audit(packet, linkage, path)

    def test_blind_join_cannot_use_a_different_packet_or_changed_native_report(self):
        _, private, judgments, packet, linkage, path = self.blind_artifacts()
        judgments['packets_sha256'] = 'other'
        path.write_text(json.dumps(judgments))
        with self.assertRaisesRegex(ComparabilityError, 'AUDIT_INPUT_HASH_MISMATCH'):
            join_blind_audit(packet, linkage, path)
        judgments['packets_sha256'] = self.sha(packet)
        path.write_text(json.dumps(judgments))
        Path(private['reports'][0]['report_path']).write_text('{}')
        with self.assertRaisesRegex(ComparabilityError, 'AUDITED_REPORT_HASH_MISMATCH'):
            join_blind_audit(packet, linkage, path)

    def test_findings_do_not_dilute_hypothesis_candidate_acceptance_denominator(self):
        def mutate(report):
            report['normalized']['audit_items'][0]['published_stance'] = 'accepted'
            report['normalized']['audit_items'].extend({'id': 'F' + str(index), 'kind': 'finding',
                'text': '来源原文', 'condition': '', 'refs': [], 'counter_refs': [],
                'published_stance': 'accepted'} for index in range(3))
        public, _, judgments, packet, linkage, path = self.blind_artifacts(mutate)
        kinds = {item['opaque_item_id']: item['item_kind'] for row in public['packets'] for item in row['items']}
        for row in judgments['reports']:
            for item in row['items']:
                if kinds[item['opaque_item_id']] == 'hypothesis':
                    item['source_support'] = 'insufficient'
        path.write_text(json.dumps(judgments))
        result = join_blind_audit(packet, linkage, path)
        original = result['by_arm']['original']
        self.assertEqual(original['reports_with_candidate_unsupported_acceptance'], 2)
        self.assertEqual(original['by_item_kind']['hypothesis']['candidate_unsupported_acceptances'], 2)
        self.assertEqual(original['by_item_kind']['hypothesis']['native_accepted_assessed'], 2)
        self.assertEqual(original['by_item_kind']['finding']['native_accepted_assessed'], 6)
        self.assertEqual(original['by_item_kind']['finding']['candidate_unsupported_acceptances'], 0)

    def test_blind_ticket_context_retains_business_scope_but_requires_frozen_world(self):
        ticket = {'title': '问题', 'symptoms': '请求失败', 'scope': {'service': 'svc', 'service_version': '1.2', 'start': '06:00'}}
        world = {'ticket': ticket}
        self.world['world_sha256'] = hashlib.sha256(json.dumps(world, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
        plan = deepcopy(self.plan)
        plan['stage'] = 'e2e'
        bundle = load_bundle(self.bundle('context', plan=plan), evaluator_root=self.root)
        specs = {'source_available': {'world': world}}
        public, _ = build_blind_packets([bundle], rubric={}, case_specs=specs, seed=1)
        self.assertEqual(public['packets'][0]['ticket_question']['scope']['service_version'], '1.2')
        specs['source_available']['world']['ticket']['symptoms'] = '改写'
        with self.assertRaisesRegex(ComparabilityError, 'AUDIT_CONTEXT_WORLD_MISMATCH'):
            build_blind_packets([bundle], rubric={}, case_specs=specs)


if __name__ == '__main__':
    unittest.main()
