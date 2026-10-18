"""Execution artifacts strengthen provenance without manufacturing human gold."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from evals.rca_contracts import load_dataset, read_json, sha_file, validate_dataset


class MachineArtifactTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / 'output' / 'rca-machine-contract-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.dataset = deepcopy(load_dataset())
        self.case = self.dataset['cases'][8]
        self.dataset['cases'] = [self.case]
        self.dataset['manifest']['cases'] = [{k: v for k, v in self.case.items()
            if k not in {'public', 'truth', 'public_diagnostics'}}]
        self.dataset['manifest_path'] = str(self.root / 'manifest.json')
        machine = self.root / 'machine'
        machine.mkdir()
        (machine / 'source.py').write_text('# Contract fixture, not an actual experiment\n', encoding='utf-8')
        sources = {'C:/unit-test-only/source.py': sha_file(machine / 'source.py')}
        raw = {'schema_version': 'rca-program-experiments-v1',
            'case_id': self.case['case_id'], 'origin': 'local_controlled_program_execution',
            'human_verified': False, 'is_gold': False, 'human_review_status': 'pending',
            'causal_truth_validated': False, 'status': 'passed',
            'parameters': {'fixture_only': True}, 'source_sha256': sources,
            'trials': [{'unit_test_fixture_only': True}],
            'checks': [{'check': 'unit_fixture_shape', 'passed': True,
                        'observed': {'unit_test_fixture_only': True}}]}
        (machine / 'raw.json').write_text(json.dumps(raw), encoding='utf-8')
        execution = {'schema_version': 'rca-program-experiments-v1',
            'origin': 'local_controlled_program_execution', 'human_verified': False,
            'is_gold': False, 'human_review_status': 'pending', 'model_calls': 0,
            'source_unchanged_during_run': True, 'business_application_imported': False,
            'original_incident_reproduced': False, 'source_sha256': sources,
            'status': 'passed', 'cases': [{'case_id': self.case['case_id'],
                'path': 'raw.json', 'sha256': sha_file(machine / 'raw.json'), 'status': 'passed'}]}
        (machine / 'manifest.json').write_text(json.dumps(execution), encoding='utf-8')
        self.case['truth']['origin'] = 'executed_synthetic_experiment'
        self.case['truth']['machine_evidence'] = {
            'status': 'executed_not_human_adjudicated',
            'experiment_manifest': self.binding('machine/manifest.json'),
            'artifacts': [{**self.binding('machine/raw.json'), 'kind': 'raw_execution'},
                          {**self.binding('machine/source.py'), 'kind': 'executed_program_source'}],
            'case_parameters': {'fixture_only': True},
            'original_authored_world_confirmed': False,
            'human_verified': False, 'is_gold': False,
        }
        self.materialize()

    def binding(self, relative):
        return {'path': relative, 'sha256': sha_file(self.root / relative)}

    def refresh_manifest(self, value):
        path = self.root / 'machine/manifest.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        self.case['truth']['machine_evidence']['experiment_manifest']['sha256'] = sha_file(path)

    def refresh_raw(self, value):
        path = self.root / 'machine/raw.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        digest = sha_file(path)
        self.case['truth']['machine_evidence']['artifacts'][0]['sha256'] = digest
        execution = read_json(self.root / 'machine/manifest.json')
        execution['cases'][0]['sha256'] = digest
        self.refresh_manifest(execution)

    def materialize(self):
        for label, field in (('public', 'public'), ('truth', 'truth'), ('diagnostics', 'public_diagnostics')):
            path = self.root / self.case[label + '_path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.case[field], ensure_ascii=False), encoding='utf-8')
            self.case[label + '_sha256'] = sha_file(path)
        self.dataset['manifest']['cases'] = [{k: v for k, v in self.case.items()
            if k not in {'public', 'truth', 'public_diagnostics'}}]
        (self.root / 'manifest.json').write_text(json.dumps(self.dataset['manifest']), encoding='utf-8')

    def test_bound_execution_files_include_raw_and_source_but_not_human_gold(self):
        loaded = load_dataset(self.root / 'manifest.json')
        self.assertEqual(len(loaded['bound_artifacts']), 3)
        self.assertEqual(loaded['validation']['machine_experiment_cases'], 1)
        self.assertEqual(loaded['validation']['human_gold_cases'], 0)
        self.assertFalse(loaded['validation']['formal_accuracy_available'])
        from scripts.evaluate_rca import dataset_freeze
        frozen = dataset_freeze(loaded)
        self.assertEqual(frozen['files_sha256']['machine/source.py'], sha_file(self.root / 'machine/source.py'))

    def test_raw_tampering_and_changed_bound_evidence_are_rejected(self):
        from scripts.evaluate_rca import dataset_freeze
        loaded = load_dataset(self.root / 'manifest.json')
        (self.root / 'machine/raw.json').write_text('{"changed":true}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            load_dataset(self.root / 'manifest.json')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            dataset_freeze(loaded)

    def test_path_escape_and_absolute_bindings_are_rejected(self):
        for name in ('machine/../machine/raw.json', str((self.root / 'machine/raw.json').resolve())):
            changed = deepcopy(self.dataset)
            changed['cases'][0]['truth']['machine_evidence']['artifacts'][0]['path'] = name
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, 'Unsafe'):
                validate_dataset(changed)

    def test_execution_cannot_promote_human_identity_or_causal_confirmation(self):
        for key, value in (('human_verified', True), ('is_gold', True),
                           ('causal_confirmation_status', 'confirmed'), ('reviewers', ['AI-as-human'])):
            changed = deepcopy(self.dataset)
            changed['cases'][0]['truth']['provenance'][key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_dataset(changed)

    def test_empty_missing_or_unmarked_machine_evidence_is_rejected(self):
        for field, value in (('artifacts', []), ('experiment_manifest', None),
                             ('status', 'human_verified'), ('case_parameters', None)):
            changed = deepcopy(self.dataset)
            changed['cases'][0]['truth']['machine_evidence'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_dataset(changed)

    def test_authored_case_cannot_relabel_itself_as_machine_executed(self):
        self.case['truth']['origin'] = 'authored_synthetic_provisional'
        with self.assertRaisesRegex(ValueError, 'masquerade'):
            validate_dataset(self.dataset)

    def test_machine_only_materials_still_export_pending_human_review(self):
        from scripts.review_rca import build_review_materials, import_reviews
        loaded = load_dataset(self.root / 'manifest.json')
        material = build_review_materials([], {self.case['case_id']: loaded['cases'][0]}, bundle_sha256='unit-test')
        template = material['truth-review.template.json']
        self.assertIsNone(template['reviewer_type'])
        self.assertTrue(all(r['status'] == 'pending' and not r['approved'] for r in template['rows']))
        self.assertFalse(import_reviews(material, [])['formal_ready'])

    def test_nested_machine_review_or_gold_claims_are_rejected(self):
        for field in ('human_verified', 'is_gold'):
            for nested in (False, True):
                changed = deepcopy(self.dataset)
                target = changed['cases'][0]['truth']['machine_evidence']
                if nested:
                    target['case_parameters']['unexpected_review_claim'] = {field: True}
                else:
                    target[field] = True
                with self.subTest(field=field, nested=nested), self.assertRaisesRegex(ValueError, 'human review or gold'):
                    validate_dataset(changed)

    def test_manifest_schema_execution_status_and_human_flags_are_checked(self):
        original = read_json(self.root / 'machine/manifest.json')
        for field, value in (('schema_version', 'wrong'), ('status', 'failed'),
                ('human_verified', True), ('is_gold', True), ('model_calls', 1),
                ('source_unchanged_during_run', False), ('original_incident_reproduced', True),
                ('business_application_imported', True), ('origin', 'authored')):
            execution = deepcopy(original)
            execution[field] = value
            self.refresh_manifest(execution)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_dataset(self.dataset)

    def test_current_case_manifest_identity_raw_path_and_hash_mapping_are_checked(self):
        original = read_json(self.root / 'machine/manifest.json')
        for field, value in (('case_id', 'different-case'), ('path', 'different.json'),
                ('path', '../raw.json'), ('sha256', '0' * 64), ('status', 'failed')):
            execution = deepcopy(original)
            execution['cases'][0][field] = value
            self.refresh_manifest(execution)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_dataset(self.dataset)

    def test_raw_schema_case_checks_parameters_and_source_map_are_checked(self):
        original = read_json(self.root / 'machine/raw.json')
        for field, value in (('schema_version', 'wrong'), ('case_id', 'different-case'),
                ('status', 'failed'), ('human_verified', True), ('is_gold', True),
                ('causal_truth_validated', True), ('origin', 'authored'),
                ('checks', []), ('checks', [{'check': 'failed', 'passed': False, 'observed': 1}]),
                ('trials', []), ('parameters', {'fixture_only': 'changed'}),
                ('source_sha256', {'C:/unit-test-only/source.py': '0' * 64})):
            raw = deepcopy(original)
            raw[field] = value
            self.refresh_raw(raw)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_dataset(self.dataset)

    def test_rehashed_unrelated_source_snapshot_cannot_replace_executed_source(self):
        path = self.root / 'machine/source.py'
        path.write_text('# Different fixture source, not the declared executed source\n', encoding='utf-8')
        self.case['truth']['machine_evidence']['artifacts'][1]['sha256'] = sha_file(path)
        with self.assertRaisesRegex(ValueError, 'source snapshot does not match'):
            validate_dataset(self.dataset)

    def test_rehashed_machine_parameters_cannot_disagree_with_raw_execution(self):
        self.case['truth']['machine_evidence']['case_parameters']['fixture_only'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'parameters differ'):
            validate_dataset(self.dataset)

    def test_nested_attached_manifest_or_raw_gold_claims_are_rejected(self):
        execution = read_json(self.root / 'machine/manifest.json')
        execution['cases'][0]['human_verified'] = True
        self.refresh_manifest(execution)
        with self.assertRaisesRegex(ValueError, 'human review or gold'):
            validate_dataset(self.dataset)
        del execution['cases'][0]['human_verified']
        self.refresh_manifest(execution)
        raw = read_json(self.root / 'machine/raw.json')
        raw['trials'][0]['is_gold'] = True
        self.refresh_raw(raw)
        with self.assertRaisesRegex(ValueError, 'human review or gold'):
            validate_dataset(self.dataset)


if __name__ == '__main__':
    unittest.main()
