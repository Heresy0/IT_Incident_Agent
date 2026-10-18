"""Actual local execution and publication integrity, never provider calls."""
import json
from pathlib import Path
import tempfile
import unittest

from evals.rca_contracts import load_dataset, sha_file
from evals.rca_independent_cases import CASE_IDS, execute_experiments, publish_dataset


class IndependentCasePublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.execution = cls.root / 'execution'
        cls.manifest = execute_experiments(cls.execution, repeats=3)
        cls.dataset_path = cls.root / 'dataset'
        cls.dataset = publish_dataset(cls.execution / 'manifest.json', cls.dataset_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_eight_distinct_executed_mechanisms_not_repeat_count(self):
        self.assertEqual(self.manifest['status'], 'passed')
        self.assertEqual(self.manifest['model_calls'], 0)
        self.assertEqual(tuple(c['case_id'] for c in self.dataset['cases']), CASE_IDS)
        self.assertEqual(len({c['independence_group'] for c in self.dataset['cases']}), 8)
        self.assertEqual(self.dataset['validation']['machine_experiment_cases'], 8)
        self.assertFalse(self.dataset['manifest']['human_verified'])
        for item in self.manifest['cases']:
            raw = json.loads((self.execution / item['path']).read_text(encoding='utf8'))
            self.assertEqual(len(raw['trials']), 3)
            self.assertTrue(all(check['passed'] for check in raw['checks']))

    def test_initial_ticket_is_neutral_and_required_evidence_requires_queries(self):
        symptoms = set()
        for case in self.dataset['cases']:
            ticket = case['public']['world']['ticket']
            symptoms.add(ticket['symptoms'])
            self.assertNotIn('row_01', json.dumps(ticket))
            eligibility = case['truth']['acquisition_eligibility']
            self.assertTrue(eligibility['required_evidence_available'])
            self.assertFalse(eligibility['initial_ticket_contains_required_evidence'])
            reachable = {oid for q in case['truth']['oracle_queries'] for oid in q['expected_observation_ids']}
            required = set(case['truth']['reference_mechanism']['key_observation_ids'])
            self.assertTrue(required <= reachable)
            self.assertGreaterEqual(len(case['truth']['oracle_queries']), 2)
        self.assertEqual(len(symptoms), 1)

    def test_public_rows_match_retained_measurements_and_fit_snippets(self):
        for case in self.dataset['cases']:
            raw = json.loads((self.dataset_path / 'machine' / (case['case_id'] + '.json')).read_text(encoding='utf8'))
            measured = {r['observation_id']: r for r in raw['public_rows']}
            for row in case['public']['world']['logs']:
                self.assertEqual(json.loads(row['message']), measured[row['id']]['message'])
                self.assertLessEqual(len(row['message']), 1100)
                self.assertEqual(row['category'], measured[row['id']]['category'])
            self.assertNotIn('reference_mechanism', json.dumps(case['public']))

    def test_saved_execution_source_is_bound_and_output_is_not_overwritten(self):
        source = self.dataset_path / 'machine/source/rca_independent_experiments_a.py'
        raw = json.loads((self.dataset_path / 'machine/rca_independent_001.json').read_text(encoding='utf8'))
        self.assertEqual(sha_file(source), raw['source_sha256']['evals/rca_independent_experiments_a.py'])
        with self.assertRaises(FileExistsError):
            publish_dataset(self.execution / 'manifest.json', self.dataset_path)
        with self.assertRaises(FileExistsError):
            execute_experiments(self.execution)

    def test_source_tampering_blocks_dataset_load(self):
        source = self.dataset_path / 'machine/source/rca_independent_experiments_a.py'
        original = source.read_bytes()
        try:
            source.write_bytes(original + b'\n# changed\n')
            with self.assertRaises(ValueError):
                load_dataset(self.dataset_path / 'manifest.json')
        finally:
            source.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
