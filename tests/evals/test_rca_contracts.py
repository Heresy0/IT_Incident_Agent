from copy import deepcopy
import builtins
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals.rca_contracts import (DEFAULT_MANIFEST, canonical_sha, canonical_sha256,
    load_dataset, read_json, sha_file, validate_dataset, validate_public, validate_public_diagnostics)


class RCAContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()

    def test_canonical_hash_ignores_dictionary_order_and_rejects_nonfinite(self):
        self.assertEqual(canonical_sha({'b': 2, 'a': ['中文']}), canonical_sha256({'a': ['中文'], 'b': 2}))
        self.assertNotEqual(canonical_sha({'value': 1}), canonical_sha({'value': 2}))
        with self.assertRaises(ValueError):
            canonical_sha({'value': float('nan')})

    def test_public_case_and_diagnostics_do_not_include_reference_fields(self):
        for case in self.dataset['cases']:
            self.assertTrue(validate_public(case['public']))
            self.assertTrue(validate_public_diagnostics(case['public_diagnostics']))
            self.assertEqual(set(case['public']), {'schema_version', 'world', 'capability_metadata'})
            for control in case['public_diagnostics']['fixed_controls']:
                self.assertNotIn('expected_relation', control)

    def test_nested_reference_metadata_and_runtime_diagnostic_merge_are_rejected(self):
        public = deepcopy(self.dataset['cases'][0]['public'])
        public['world']['logs'][0]['expected_relation'] = 'supported'
        with self.assertRaisesRegex(ValueError, 'Private reference'):
            validate_public(public)
        public = deepcopy(self.dataset['cases'][0]['public'])
        public['diagnostics'] = self.dataset['cases'][0]['public_diagnostics']
        with self.assertRaises(ValueError):
            validate_public(public)

    def test_authored_references_cannot_claim_human_review_or_causal_confirmation(self):
        for field, value in [('human_verified', True), ('is_gold', True),
                             ('reproduction_evidence', ['fabricated verification']),
                             ('causal_confirmation_status', 'confirmed')]:
            changed = deepcopy(self.dataset)
            changed['cases'][0]['truth']['provenance'][field] = value
            with self.assertRaises(ValueError):
                validate_dataset(changed)

    def test_related_group_cannot_cross_splits(self):
        changed = deepcopy(self.dataset)
        candidate = changed['cases'][8]
        candidate['independence_group'] = changed['cases'][0]['independence_group']
        candidate['truth']['independence_group'] = candidate['independence_group']
        with self.assertRaisesRegex(ValueError, 'cross'):
            validate_dataset(changed)

    def test_hash_tamper_and_escaping_path_are_rejected(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'dataset'
            shutil.copytree(DEFAULT_MANIFEST.parent, folder)
            manifest = read_json(folder / 'manifest.json')
            public = folder / manifest['cases'][0]['public_path']
            public.write_text(public.read_text(encoding='utf-8') + ' ', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                load_dataset(folder / 'manifest.json')
            manifest['cases'][0]['public_path'] = '../outside.json'
            (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                load_dataset(folder / 'manifest.json')

    def test_criteria_must_bind_real_observation_ids(self):
        changed = deepcopy(self.dataset)
        changed['cases'][8]['truth']['evidence_criteria'][0]['acceptable_observation_sets'] = [['invented']]
        with self.assertRaisesRegex(ValueError, 'absent source'):
            validate_dataset(changed)

    def test_loader_is_offline_without_application_or_model_imports(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.split('.')[0] in {'api', 'agents', 'providers', 'workflow', 'runtime', 'openai', 'langchain_openai'}:
                raise AssertionError('Application or model import in data loader')
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('isolated_rca_contracts',
            Path(__file__).resolve().parents[2] / 'evals' / 'rca_contracts.py')
        module = importlib.util.module_from_spec(spec)
        with patch('builtins.__import__', side_effect=guarded):
            spec.loader.exec_module(module)
            loaded = module.load_dataset(DEFAULT_MANIFEST)
        self.assertEqual(len(loaded['cases']), 38)
        self.assertEqual(loaded['manifest_sha256'], sha_file(DEFAULT_MANIFEST))


if __name__ == '__main__':
    unittest.main()
