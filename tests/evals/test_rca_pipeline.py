"""Cross-module, no-network checks of native execution and pending human review."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evals.rca_contracts import DEFAULT_MANIFEST, load_dataset


ROOT = Path(__file__).resolve().parents[2]


class RcaPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = ROOT / 'output' / 'rca-pipeline-tests'
        parent.mkdir(parents=True, exist_ok=True)
        cls.folder = tempfile.TemporaryDirectory(dir=parent)
        cls.path = Path(cls.folder.name).resolve()
        assert cls.path.is_relative_to(parent.resolve())
        case = next(row for row in load_dataset()['cases'] if row['split'] == 'candidate_acceptance')
        result = subprocess.run([sys.executable, 'scripts/evaluate_rca.py',
            '--scripted', '--application-root', str(ROOT), '--dataset', str(DEFAULT_MANIFEST),
            '--split', 'candidate', '--cases', case['case_id'],
            '--stages', 'e2e', 'sufficient_evidence', 'fixed_review',
            '--repeats', '1', '--jobs', '1', '--output', str(cls.path / 'runs')],
            cwd=ROOT, text=True, encoding='utf-8', capture_output=True, timeout=180)
        if result.returncode:
            raise AssertionError('Native no-network pipeline failed: ' + result.stdout[-3000:] + result.stderr[-3000:])
        paths = list((cls.path / 'runs').glob('*/bundle.json'))
        assert len(paths) == 1
        cls.bundle_path = paths[0]

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def inputs(self):
        from scripts.review_rca import load_bundles, build_review_materials
        bundles, dataset, records, sources, comparability = load_bundles([self.bundle_path])
        cases = {row['case_id']: row for row in dataset['cases']}
        material = build_review_materials(records, cases,
            bundle_sha256=sources[0]['sha256'] if len(sources) == 1 else '')
        return bundles, dataset, records, sources, comparability, cases, material

    def test_all_three_native_modes_remain_engineering_only(self):
        bundles, _, records, _, _, _, _ = self.inputs()
        self.assertEqual({r['entry']['stage'] for r in records},
                         {'e2e', 'sufficient_evidence', 'fixed_review'})
        self.assertEqual(bundles[0]['execution_mode'], 'scripted_control_only')
        self.assertIsNone(bundles[0]['paid_budget'])
        for row in records:
            self.assertNotIn('harness_failure', row['report'])
            self.assertEqual(row['report']['native_arm'], 'current')
            self.assertIsNone(row['report']['runtime_budget_adapter'])

    def test_pending_truth_and_empty_human_reviews_do_not_become_accuracy(self):
        from scripts.review_rca import import_reviews
        from scripts.summarize_rca import build_summary
        bundles, dataset, records, sources, comparable, _, material = self.inputs()
        joined = import_reviews(material, [])
        summary = build_summary(bundles, dataset, records, sources, comparable,
                                human=joined, materials=material)
        self.assertIsNone(summary['formal_metrics'])
        self.assertFalse(summary['human_verified'])
        self.assertFalse(summary['is_gold'])
        for name in ('evidence-review.template.json', 'root-review.template.json', 'truth-review.template.json'):
            template = material[name]
            self.assertIsNone(template['reviewer_type'])
            self.assertTrue(all(row['approved'] is False and row['status'] == 'pending'
                                for row in template['rows']))

    def test_evidence_packet_is_blind_and_changed_text_cannot_be_imported(self):
        from scripts.review_rca import verify_report_bindings
        _, _, records, sources, _, cases, material = self.inputs()
        packet = material['evidence-packets.json']
        for row in packet['packets']:
            self.assertNotIn('candidate_truth', row)
            self.assertNotIn('identity', row)
            self.assertNotIn('system_id', row)
            self.assertNotIn('identifiability', row)
        changed = deepcopy(material)
        first = changed['evidence-packets.json']['packets'][0]
        if first['items']:
            first['items'][0]['visible_fields']['text'] += ' Changed after review.'
        else:
            first['question']['title'] += ' Changed after review.'
        with self.assertRaises(ValueError):
            verify_report_bindings(changed, records, cases, bundle_sha256=sources[0]['sha256'])


if __name__ == '__main__':
    unittest.main()
