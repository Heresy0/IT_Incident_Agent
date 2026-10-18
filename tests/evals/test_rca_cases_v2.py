from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from evals.rca_cases import validate_reachability
from evals.rca_cases_v2 import (ATOMIC_UNKNOWN_STATEMENT, DIRECT_COUNTEREVIDENCE,
    SOURCE_FINGERPRINT, V2_MANIFEST, build_revised_cases, write_dataset)
from evals.rca_contracts import DEFAULT_MANIFEST, canonical_sha, load_dataset, sha_file


class RCAAtomicQuestionRevisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = load_dataset(DEFAULT_MANIFEST)
        cls.revised = load_dataset(V2_MANIFEST)

    def test_frozen_source_and_all_runtime_worlds_are_unchanged(self):
        self.assertEqual(self.source['fingerprint'], SOURCE_FINGERPRINT)
        self.assertEqual(len(self.revised['cases']), 38)
        for old, new in zip(self.source['cases'], self.revised['cases'], strict=True):
            self.assertEqual(old['case_id'], new['case_id'])
            self.assertEqual(old['public_sha256'], new['public_sha256'])
            self.assertEqual(old['public'], new['public'])
            self.assertEqual(old['public_diagnostics']['source_queries'],
                             new['public_diagnostics']['source_queries'])
            self.assertEqual(old['truth']['causal_conditions'], new['truth']['causal_conditions'])
            self.assertEqual(old['truth']['evidence_criteria'], new['truth']['evidence_criteria'])

    def test_candidate_questions_separate_unknown_process_state_from_proof_claim(self):
        for case in self.revised['cases']:
            if case['split'] != 'candidate_acceptance':
                continue
            control = next(c for c in case['truth']['fixed_controls'] if c['control_id'] == 'control_02')
            self.assertEqual(control['statement'], ATOMIC_UNKNOWN_STATEMENT)
            self.assertEqual(control['expected_relation'], 'insufficient')
            for phrase in ('已证明', '唯一', '所有失败', '记录中明确'):
                self.assertNotIn(phrase, control['statement'])
            old = next(c for c in self.source['cases'] if c['case_id'] == case['case_id'])
            self.assertEqual(case['truth']['fixed_controls'][0], old['truth']['fixed_controls'][0])

    def test_three_added_questions_have_explicit_opposite_observations(self):
        added = []
        for case in self.revised['cases']:
            if case['split'] != 'candidate_acceptance':
                continue
            controls = [c for c in case['truth']['fixed_controls'] if c['control_id'] == 'control_03']
            if not controls:
                continue
            self.assertEqual(len(controls), 1)
            added.append(case['case_id'])
            control = controls[0]
            direct = DIRECT_COUNTEREVIDENCE[case['case_id']]
            self.assertEqual(control['expected_relation'], 'contradicted')
            self.assertEqual(control['statement'], direct['statement'])
            row_id, field = control['selectors'][0]
            row = next(r for r in case['public']['world']['logs'] if r['id'] == row_id)
            self.assertIn(direct['counterevidence'], row[field])
            self.assertNotIn('已证明', control['statement'])
        self.assertEqual(sorted(added), sorted(DIRECT_COUNTEREVIDENCE))

    def test_calibration_and_all_human_gold_statuses_are_preserved(self):
        for old, new in zip(self.source['cases'], self.revised['cases'], strict=True):
            if old['split'] == 'calibration':
                self.assertEqual(old, new)
            provenance = new['truth']['provenance']
            self.assertFalse(provenance['human_verified'])
            self.assertFalse(provenance['is_gold'])
            self.assertEqual(provenance['reviewers'], [])
            self.assertEqual(provenance['reproduction_evidence'], [])
            self.assertEqual(new['truth']['truth_status'], 'pending_human_review')
        self.assertFalse(self.revised['manifest']['human_verified'])
        self.assertFalse(self.revised['manifest']['holdout_sealed'])
        self.assertEqual(self.revised['validation']['human_gold_cases'], 0)

    def test_revision_metadata_discloses_new_task_and_no_historical_relabelling(self):
        revision = self.revised['manifest']['question_revision']
        self.assertFalse(revision['historical_results_relabelled'])
        self.assertTrue(revision['public_observations_byte_identical'])
        self.assertEqual(revision['source_manifest_sha256'], sha_file(DEFAULT_MANIFEST))
        self.assertEqual(revision['relation_reference_status'], 'proposed_pending_human_review')
        self.assertIn('new task', revision['comparison_note'])

    def test_actual_native_tools_reach_original_witnesses_and_new_control_fields(self):
        result = validate_reachability(self.revised)
        self.assertEqual(len(result['cases']), 38)
        self.assertEqual(result['model_calls'], 0)
        self.assertFalse(result['causal_truth_validated'])

    def test_deterministic_write_never_overwrites_source_or_existing_version(self):
        source_sha = sha_file(DEFAULT_MANIFEST)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'new-version'
            written = write_dataset(folder)
            self.assertEqual(written['fingerprint'], self.revised['fingerprint'])
            with self.assertRaises(FileExistsError):
                write_dataset(folder)
        with self.assertRaisesRegex(ValueError, 'must not overwrite'):
            write_dataset(DEFAULT_MANIFEST.parent)
        self.assertEqual(sha_file(DEFAULT_MANIFEST), source_sha)

    def test_unknown_source_version_requires_explicit_new_revision(self):
        changed = deepcopy(self.source)
        changed['fingerprint'] = canonical_sha({'different_source': True})
        with self.assertRaisesRegex(ValueError, 'frozen rca_v1'):
            build_revised_cases(changed)


if __name__ == '__main__':
    unittest.main()
