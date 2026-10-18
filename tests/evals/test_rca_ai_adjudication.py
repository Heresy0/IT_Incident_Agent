"""Synthetic records exercise integrity gates; they are not case judgments."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from evals.rca_scoring import ROOT_FIELDS, ReviewError, sha_file
from scripts.score_rca_ai_adjudication import score_adjudication, verify_citations


def inputs():
    origin = {'reviewer_type': 'ai', 'human_verified': False, 'is_gold': False, 'nonblind': True}
    case = dict(origin, schema_version='rca-ai-case-adjudication-v1', case_id='case1',
        truth={'identifiability': 'root_identifiable', 'rationale': 'synthetic root',
               'citations': [{'path': 'synthetic_fixture'}]}, native_runs=[], diagnostic_runs=[])
    native, diagnostic = [], []
    for repeat in range(1, 4):
        entry = {'report_id': f'N{repeat}', 'report_sha256': f'nh{repeat}', 'case_id': 'case1',
            'stage': 'e2e', 'repeat': repeat, 'system_id': 'test'}
        report = {'normalized': {'audit_items': [{'kind': 'hypothesis', 'text': 'fixture claim', 'refs': []}],
            'observations': []}, 'native_report': {'status': 'completed'}}
        native.append({'entry': entry, 'report': report})
        flags = {key: True for key in ROOT_FIELDS}
        flags.update(wrong_definitive_cause=False, unsupported_definitive_cause=False,
                     abstained_from_root=False, established_assertions_supported=True)
        case['native_runs'].append({**entry, 'judgment': flags,
            'rationale_by_field': {key: 'synthetic rationale' for key in flags},
            'decisive_citations': [{'report_path': 'synthetic_fixture'}]})
        item = {'target_id': 'C1', 'relation': 'supported', 'protocol_status': 'valid', 'refs': []}
        report = {'report_id': f'D{repeat}', 'case_id': 'case1', 'group_id': 'fixture_refs',
            'condition': 'full_refs', 'repeat': repeat, 'normalized': {'audit_items': [], 'observations': []},
            'native_report': {'evidence': [], 'primary_review': {'items': [deepcopy(item)]},
                              'final_review': {'items': [deepcopy(item)]}}}
        diagnostic.append({'report': report, 'sha256': f'dh{repeat}'})
        case['diagnostic_runs'].append({'report_id': f'D{repeat}', 'report_sha256': f'dh{repeat}',
            'group_id': 'fixture_refs', 'condition': 'full_refs', 'repeat': repeat,
            'eligible_for_semantic_accuracy': True, 'exclusion_reason': None,
            'expected_relation': 'supported', 'acceptable_relations': ['supported'],
            'primary_relation': 'supported', 'final_relation': 'supported',
            'rationale': 'synthetic support', 'citations': [{'path': 'synthetic_fixture'}]})
    document = dict(origin, schema_version='rca-ai-adjudication-v1', status='completed', cases=[case])
    return document, native, diagnostic


class AiAdjudicationTests(unittest.TestCase):
    def score(self, data):
        return score_adjudication(*data, repeats=3)

    def test_scores_completed_ai_review_without_human_promotion(self):
        result = self.score(inputs())
        self.assertEqual(result['status'], 'scored')
        self.assertFalse(result['human_verified'])
        self.assertFalse(result['is_gold'])
        self.assertIsNone(result['human_verified_formal_metrics'])
        self.assertEqual(result['ai_adjudicated_metrics']['test:e2e']['strict_root_location_run_rate']['numerator'], 3)

    def test_human_or_gold_identity_rejected_at_both_levels(self):
        for target in ('document', 'case'):
            for field, value in (('reviewer_type', 'human'), ('human_verified', True), ('is_gold', True), ('nonblind', False)):
                data = inputs()
                row = data[0] if target == 'document' else data[0]['cases'][0]
                row[field] = value
                with self.assertRaises(ReviewError):
                    self.score(data)

    def test_missing_reviews_and_duplicate_reviews_rejected(self):
        for panel in ('native_runs', 'diagnostic_runs'):
            for change in ('missing', 'duplicate'):
                data = inputs()
                rows = data[0]['cases'][0][panel]
                rows.pop() if change == 'missing' else rows.append(deepcopy(rows[0]))
                with self.assertRaises(ReviewError):
                    self.score(data)

    def test_report_hash_identity_and_verdict_tampering_rejected(self):
        for panel, field, value in (('native_runs', 'report_sha256', 'stale'),
            ('native_runs', 'repeat', 8), ('diagnostic_runs', 'report_sha256', 'stale'),
            ('diagnostic_runs', 'group_id', 'other'), ('diagnostic_runs', 'primary_relation', 'partial'),
            ('diagnostic_runs', 'final_relation', 'contradicted')):
            data = inputs()
            data[0]['cases'][0][panel][0][field] = value
            with self.assertRaises(ReviewError):
                self.score(data)

    def test_incomplete_boolean_or_rationale_rejected(self):
        for change in ('missing', 'string', 'reason'):
            data = inputs()
            row = data[0]['cases'][0]['native_runs'][0]
            if change == 'missing':
                row['judgment'].pop('mechanism_correct')
            elif change == 'string':
                row['judgment']['mechanism_correct'] = 'true'
            else:
                row['rationale_by_field'].pop('mechanism_correct')
            with self.assertRaises(ReviewError):
                self.score(data)

    def test_ambiguous_control_excluded_from_denominator_with_reason(self):
        data = inputs()
        row = data[0]['cases'][0]['diagnostic_runs'][0]
        row.update(eligible_for_semantic_accuracy=False, exclusion_reason='ambiguous fixture',
                   acceptable_relations=[], expected_relation=None)
        result = self.score(data)['diagnostic_semantics']
        self.assertEqual(result['excluded_reports'], 1)
        self.assertEqual(result['accuracy']['final'], {'numerator': 2, 'denominator': 2, 'rate': 1.0})
        row['exclusion_reason'] = None
        with self.assertRaises(ReviewError):
            self.score(data)

    def test_invalid_review_protocol_is_not_semantic_success(self):
        data = inputs()
        data[2][0]['report']['native_report']['final_review']['items'][0]['protocol_status'] = 'invalid'
        result = self.score(data)['diagnostic_semantics']['accuracy']
        self.assertEqual(result['primary']['numerator'], 3)
        self.assertEqual(result['final']['numerator'], 2)

    def test_incomplete_repeat_block_not_scored(self):
        data = inputs()
        data[1].pop()
        data[0]['cases'][0]['native_runs'].pop()
        with self.assertRaisesRegex(ReviewError, 'INCOMPLETE_CASE_REPEAT_BLOCK'):
            self.score(data)

    def test_ambiguous_legacy_control_cannot_be_included(self):
        data = inputs()
        for source, judgment in zip(data[2], data[0]['cases'][0]['diagnostic_runs']):
            source['report'].update(group_id='legacy_fixture_wording', condition='wording_v1')
            judgment.update(group_id='legacy_fixture_wording', condition='wording_v1')
        with self.assertRaisesRegex(ReviewError, 'AMBIGUOUS_LEGACY'):
            self.score(data)

    def test_incomplete_diagnostic_repeat_block_not_scored(self):
        data = inputs()
        data[2].pop()
        data[0]['cases'][0]['diagnostic_runs'].pop()
        with self.assertRaisesRegex(ReviewError, 'INCOMPLETE_DIAGNOSTIC_REPEAT_BLOCK'):
            self.score(data)

    def test_citation_paths_hashes_and_report_binding_are_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'report.json'
            target.write_text(json.dumps({'output': {'items': [1]}}), encoding='utf8')
            other = root / 'other.json'
            other.write_text('{}', encoding='utf8')
            citation = {'report_path': 'report.json', 'json_path': '$.output.items[0]'}
            truth = {'path': 'report.json', 'sha256': sha_file(target), 'json_path': '$.output'}
            document = {'cases': [{'truth': {'citations': [truth]}, 'native_runs': [
                {'report_id': 'N1', 'decisive_citations': [citation]}], 'diagnostic_runs': []}]}
            records = [{'entry': {'report_id': 'N1'}, 'report_path': str(target)}]
            self.assertEqual(verify_citations(document, records, artifact_root=root)['citations_checked'], 2)
            for key, value in (('path', 'missing.json'), ('sha256', 'stale'), ('json_path', '$.missing')):
                changed = deepcopy(document)
                changed['cases'][0]['truth']['citations'][0][key] = value
                with self.assertRaises(ReviewError):
                    verify_citations(changed, records, artifact_root=root)
            changed = deepcopy(document)
            changed['cases'][0]['native_runs'][0]['decisive_citations'][0]['report_path'] = 'other.json'
            with self.assertRaises(ReviewError):
                verify_citations(changed, records, artifact_root=root)


if __name__ == '__main__':
    unittest.main()
