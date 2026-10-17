"""Free checks for a frozen, native-contract comparison; no model invocation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from evals.closed_loop_cases import build_cases
from evals.frozen_version_cases import (FIXTURE_COVERAGE_IDS, FIXTURE_IDS, audit_rubric,
    blind_audit_schema, build_case_specs, fingerprint, freeze_case_spec, opaque)


ROOT = Path(__file__).resolve().parents[2]


class FrozenVersionCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_cases = build_cases()
        cls.specs = build_case_specs(ROOT, source_cases=cls.source_cases)

    def test_predeclared_cases_and_repeats_have_honest_world_counts(self):
        specs = self.specs
        self.assertEqual(len(specs), 16)
        self.assertEqual(sum('e2e' in spec['stages'] for spec in specs.values()), 11)
        self.assertEqual(sum('review' in spec['stages'] for spec in specs.values()), 8)
        self.assertEqual(sum(len(spec['review_controls']) for spec in specs.values()), 16)
        for cid in FIXTURE_IDS:
            original, anonymous = specs['fixture_' + cid], specs['anonymous_' + cid]
            self.assertEqual(original['matched_world_group'], anonymous['matched_world_group'])
            self.assertFalse(original['external_blind_test'])
            self.assertFalse(anonymous['external_blind_test'])

    def test_original_fixture_worlds_are_byte_source_identical_data(self):
        for cid in FIXTURE_IDS:
            original = json.loads((ROOT / 'fixtures' / 'incident' / f'{cid}.json').read_text(encoding='utf-8'))
            spec = self.specs['fixture_' + cid]
            self.assertEqual(spec['world'], original)
            self.assertEqual(spec['key_observation_ids'], list(FIXTURE_COVERAGE_IDS[cid]))
            self.assertEqual(spec['key_metric_kind'], 'prespecified_observation_coverage')
            self.assertFalse(spec['eligible_for_positive_acquisition_rate'])
            self.assertTrue(spec['eligible_for_observation_coverage_rate'])

    def test_anonymous_worlds_match_the_preexisting_identifier_only_transform(self):
        from scripts.evaluate_autonomous import FrozenWorld
        prior = FrozenWorld(list(FIXTURE_IDS), anonymize=True)
        for cid in FIXTURE_IDS:
            spec = self.specs['anonymous_' + cid]
            self.assertEqual(spec['world'], prior.payloads[cid])
            self.assertEqual(spec['capability_metadata'], prior.capability_metadata[cid])
            self.assertEqual(spec['key_observation_ids'],
                [opaque('row_', row) for row in FIXTURE_COVERAGE_IDS[cid]])

    def test_source_worlds_and_fixed_claims_retain_original_observation_data(self):
        for cid, source in self.source_cases.items():
            spec = self.specs['world_' + cid]
            self.assertEqual(spec['world'], source.observations)
            self.assertEqual(spec['key_observation_ids'], list(source.key_observation_ids))
            claims = [row for row in spec['review_controls'] if row['kind'] == 'factual_relation']
            self.assertEqual([row['statement'] for row in claims],
                             [row['statement'] for row in source.review_claims])
            for control in spec['review_controls']:
                self.assertEqual(control['expected_accept'],
                    control['reference_relation'] in {'supported', 'appropriate'})
        self.assertFalse(self.specs['world_followup_unavailable']['eligible_for_positive_acquisition_rate'])
        self.assertEqual(self.specs['world_followup_unavailable']['key_observation_ids'], [])

    def test_control_denominators_do_not_treat_missing_future_results_as_negative(self):
        controls = [row for spec in self.specs.values() for row in spec['review_controls']]
        self.assertEqual(sum(row['expected_accept'] for row in controls), 6)
        self.assertEqual(sum(not row['expected_accept'] for row in controls), 10)
        action = self.specs['world_future_manual_check']['review_controls'][1]
        self.assertEqual(action['kind'], 'check_appropriateness')
        self.assertTrue(action['expected_accept'])
        self.assertIn('授权', action['action'])

    def test_reference_changes_are_frozen_without_leaking_labels_into_worlds(self):
        for spec in self.specs.values():
            world = spec['world']
            self.assertFalse({'gold', 'correct_answer', 'review_controls', 'expected_accept',
                              'key_observation_ids', 'reference_relation'} & world.keys())
        spec = deepcopy(self.specs['world_followup_available'])
        before = freeze_case_spec(spec)
        spec['review_controls'][0]['expected_accept'] = True
        after = freeze_case_spec(spec)
        self.assertEqual(before['world_sha256'], after['world_sha256'])
        self.assertEqual(before['metadata_sha256'], after['metadata_sha256'])
        self.assertNotEqual(before['reference_sha256'], after['reference_sha256'])
        spec['capability_metadata']['log_metadata']['configuration']['description'] += ' changed'
        self.assertNotEqual(after['metadata_sha256'], freeze_case_spec(spec)['metadata_sha256'])

    def test_specs_and_audit_contract_are_plain_json_and_deep_copies(self):
        restored = json.loads(json.dumps(self.specs, ensure_ascii=False))
        self.assertEqual(restored, self.specs)
        self.assertEqual(len(fingerprint(audit_rubric())), 64)
        changed = audit_rubric()
        changed['scope_pitfalls'].clear()
        self.assertTrue(audit_rubric()['scope_pitfalls'])
        self.assertFalse(blind_audit_schema()['human_verified'])
        self.assertFalse(blind_audit_schema()['is_gold'])

    def test_blind_audit_contract_preserves_unknown_and_excludes_identity_and_stance(self):
        contract, rubric = blind_audit_schema(), audit_rubric()
        self.assertIn('abstain', contract['source_support_values'])
        self.assertIn('report_unavailable', contract['audit_status_values'])
        self.assertIn('no_claims', contract['audit_status_values'])
        self.assertTrue({'arm', 'model', 'version', 'status', 'native_target_id', 'published_stance'}
                        <= set(contract['forbidden_identity_fields']))
        self.assertIn('actually acquired', rubric['source_scope'])
        self.assertIn('Missing future check results', rubric['manual_check'])
        self.assertIn('never reproject legacy', rubric['engineering'])

    def test_importing_case_module_does_not_import_either_business_application(self):
        import builtins
        original_import = builtins.__import__

        def block_application(name, *args, **kwargs):
            if name.split('.')[0] in {'api', 'agents', 'providers', 'runtime', 'workflow'}:
                raise AssertionError('Case module imported application code at import time')
            return original_import(name, *args, **kwargs)

        path = ROOT / 'evals' / 'frozen_version_cases.py'
        spec = importlib.util.spec_from_file_location('isolated_frozen_version_cases', path)
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=block_application):
            spec.loader.exec_module(module)
            self.assertEqual(module.audit_rubric()['version'], audit_rubric()['version'])


if __name__ == '__main__':
    unittest.main()
