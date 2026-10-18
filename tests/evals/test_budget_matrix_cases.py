"""Free validation of shared budget-study data and honest scoring denominators."""
from copy import deepcopy
import builtins
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from evidence.contracts import TOOL_ARGS
from evals.closed_loop_cases import SourceWorldProvider, build_cases
from evals.budget_matrix_cases import (ARMS, E2E_CASE_IDS, FIXED_REWORK_CASE_IDS,
    audit_rubric, blind_audit_schema, build_case_specs, fingerprint,
    freeze_case_spec, study_design)
from evals.frozen_version_cases import build_case_specs as original_specs


ROOT = Path(__file__).resolve().parents[2]


class BudgetMatrixCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = build_cases()
        cls.specs = build_case_specs(ROOT, source_cases=cls.sources)

    def test_schedule_matches_existing_e2e_and_actual_fixed_rework_cases(self):
        design = study_design()
        self.assertEqual(tuple(self.specs), E2E_CASE_IDS)
        self.assertEqual(len(E2E_CASE_IDS), 11)
        self.assertEqual(FIXED_REWORK_CASE_IDS,
                         ('world_followup_available', 'world_followup_unavailable'))
        self.assertNotIn('world_pool_probe_ambiguity', self.specs)
        self.assertEqual(design['stages']['e2e']['runs'], 132)
        self.assertEqual(design['stages']['fixed_rework']['runs'], 24)
        self.assertEqual(design['planned_runs'], 156)
        self.assertEqual(design['maximum_model_requests'], 2184)
        self.assertFalse(design['is_final_prespecified_plan'])
        self.assertEqual(design['paid_requests_sent'], 0)

    def test_only_two_budget_allowances_vary_across_four_arms(self):
        self.assertEqual(list(ARMS), ['m12_r2', 'm12_r4', 'm16_r2', 'm16_r4'])
        self.assertEqual(set(tuple(value.values()) for value in ARMS.values()),
                         {(12, 2), (12, 4), (16, 2), (16, 4)})
        for invalid in (0, 6, 1.5, True):
            with self.assertRaises(ValueError):
                study_design(invalid)

    def test_observation_worlds_and_channel_metadata_are_exact_existing_data(self):
        prior = original_specs(ROOT, source_cases=self.sources)
        for case_id, spec in self.specs.items():
            self.assertEqual(spec['world'], prior[case_id]['world'])
            self.assertEqual(spec['capability_metadata'], prior[case_id]['capability_metadata'])
            self.assertEqual(spec['key_observation_ids'], prior[case_id]['key_observation_ids'])
            self.assertFalse(spec['external_blind_test'])

    def test_fixed_prefix_expected_records_match_real_existing_provider_reads(self):
        for case_id in FIXED_REWORK_CASE_IDS:
            spec = self.specs[case_id]
            source = self.sources[spec['source_case_id']]
            provider = SourceWorldProvider(source)
            observed = []
            for check in spec['initial_checks']:
                args = TOOL_ARGS[check['tool']].model_validate_json(json.dumps(check['args']))
                rows, _ = provider.query(check['tool'], args, provider.ticket().scope)
                observed.extend(row['id'] for row in rows)
            self.assertEqual(observed, spec['initial_observation_ids'])
            self.assertEqual(spec['initial_checks'], list(source.initial_checks))

    def test_positive_followup_denominator_excludes_unavailable_world(self):
        available = self.specs['world_followup_available']['stage_acquisition']
        unavailable = self.specs['world_followup_unavailable']['stage_acquisition']
        self.assertEqual(available['fixed_rework']['initial_observation_ids'], ['generic-failure'])
        self.assertEqual(available['fixed_rework']['expected_new_key_observation_ids'], ['local-detail'])
        self.assertTrue(available['fixed_rework']['eligible_for_positive_new_acquisition_rate'])
        for stage in ('e2e', 'fixed_rework'):
            self.assertEqual(unavailable[stage]['expected_new_key_observation_ids'], [])
            self.assertFalse(unavailable[stage]['eligible_for_positive_new_acquisition_rate'])
            self.assertTrue(unavailable[stage]['unknown_is_legitimate'])
        for spec in self.specs.values():
            self.assertEqual(spec['stage_acquisition']['e2e']['initial_observation_ids'], [])
        fixture = self.specs['fixture_case_001']['stage_acquisition']['e2e']
        self.assertFalse(fixture['eligible_for_positive_acquisition_rate'])
        self.assertFalse(fixture['eligible_for_positive_new_acquisition_rate'])

    def test_fixed_draft_preserves_original_text_without_reference_label_leakage(self):
        forbidden = {'reference_relation', 'expected_accept', 'key_observation_ids',
                     'review_targets', 'reference_kind', 'relation'}
        for case_id in FIXED_REWORK_CASE_IDS:
            spec = self.specs[case_id]
            source = self.sources[spec['source_case_id']]
            draft = spec['fixed_draft']
            self.assertEqual([claim['statement'] for claim in draft['claims']],
                             [claim['statement'] for claim in source.review_claims])
            self.assertEqual([claim['selectors'] for claim in draft['claims']],
                             [[list(pair) for pair in claim['selectors']] for claim in source.review_claims])
            self.assertFalse(any(key in json.dumps(draft) for key in forbidden))
            self.assertEqual(len(spec['review_targets']), 1)
            self.assertFalse(spec['review_targets'][0]['expected_accept'])
            self.assertEqual(spec['review_targets'][0]['reference_relation'], 'insufficient')

    def test_stage_reference_and_fixed_statement_changes_are_hash_bound(self):
        spec = deepcopy(self.specs['world_followup_available'])
        before = freeze_case_spec(spec)
        spec['stage_acquisition']['fixed_rework']['expected_new_key_observation_ids'] = []
        changed = freeze_case_spec(spec)
        self.assertNotEqual(before['budget_reference_sha256'], changed['budget_reference_sha256'])
        self.assertEqual(before['world_sha256'], changed['world_sha256'])
        spec['fixed_draft']['claims'][0]['statement'] += ' changed'
        self.assertNotEqual(changed['fixed_draft_sha256'], freeze_case_spec(spec)['fixed_draft_sha256'])

    def test_specs_are_json_deep_copies_and_drafts_are_not_shared_with_sources(self):
        self.assertEqual(json.loads(json.dumps(self.specs, ensure_ascii=False)), self.specs)
        changed = build_case_specs(ROOT, source_cases=self.sources)
        changed['world_followup_available']['world']['logs'][0]['message'] = 'changed'
        self.assertNotEqual(changed['world_followup_available']['world'],
                            self.sources['followup_available'].observations)
        for spec in self.specs.values():
            self.assertFalse({'review_targets', 'key_observation_ids', 'stage_acquisition',
                              'correct_answer', 'gold'} & set(spec['world']))

    def test_blind_audit_has_no_budget_or_native_verdict_identity_and_is_not_gold(self):
        schema, rubric = blind_audit_schema(), audit_rubric()
        self.assertFalse(schema['human_verified'])
        self.assertFalse(schema['is_gold'])
        self.assertFalse(rubric['human_verified'])
        self.assertFalse(rubric['is_gold'])
        self.assertTrue({'arm', 'model_calls', 'rework_new_reads', 'review_verdict', 'repeat', 'stage'}
                        <= set(schema['forbidden_identity_fields']))
        self.assertIn('never count an already-read key as a successful',
                      rubric['budget_matrix']['primary_acquisition'])
        self.assertIn('no positive new-key denominator', rubric['budget_matrix']['unavailable'])
        self.assertIn('Program-derived findings are excluded', rubric['natural_outputs'])
        self.assertIn('final hypotheses', rubric['natural_outputs'])
        self.assertIn('pending recommendations', rubric['natural_outputs'])
        self.assertNotIn('Audit all final observations', rubric['natural_outputs'])
        self.assertIn('supported/partial/contradicted/insufficient', rubric['fixed_controls'])
        self.assertIn('There is no legacy contract mapping', rubric['fixed_controls'])
        self.assertEqual(len(fingerprint(rubric)), 64)

    def test_import_does_not_import_business_application_or_model_clients(self):
        original_import = builtins.__import__

        def block_application(name, *args, **kwargs):
            if name.split('.')[0] in {'api', 'agents', 'providers', 'runtime', 'workflow', 'langchain_openai'}:
                raise AssertionError('Budget case module imported application or model client')
            return original_import(name, *args, **kwargs)

        path = ROOT / 'evals' / 'budget_matrix_cases.py'
        loader = importlib.util.spec_from_file_location('isolated_budget_matrix_cases', path)
        module = importlib.util.module_from_spec(loader)
        with patch('builtins.__import__', side_effect=block_application):
            loader.loader.exec_module(module)
            self.assertEqual(module.study_design()['planned_runs'], 156)


if __name__ == '__main__':
    unittest.main()
