"""Synthetic reviewer fixtures exercise validation; they are not delivered gold."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest

from evals.rca_scoring import EVIDENCE_FIELDS, ROOT_FIELDS, ReviewError, canonical_sha, sha_file
from scripts.review_rca import (ATTESTATION, build_review_materials, import_reviews,
    verify_report_bindings, visible_items)


def fixture():
    truth = {'case_id': 'test-case', 'identifiability': 'root_identifiable',
        'truth_status': 'pending_human_review', 'provenance': {'human_verified': False}}
    case = {'case_id': 'test-case', 'public_sha256': 'w' * 64, 'truth_sha256': 't' * 64,
        'truth': truth, 'public': {'world': {'ticket': {'title': 'Synthetic test outage', 'scope': {'service': 'test'}}}}}
    source = {'evidence_id': 'EV1', 'observation_id': 'O1', 'payload': {'value': 7},
        'source_fields': {'service': 'test', 'observed_at': '2026-10-18T00:00:00Z'}}
    reference = {'evidence_id': 'EV1', 'field_path': 'value', 'value': 7}
    report = {'report_id': 'native-report', 'case_id': 'test-case',
        'normalized': {'observations': [source], 'audit_items': [
            {'id': 'F1', 'kind': 'finding', 'text': 'Source says 7', 'refs': [reference]},
            {'id': 'C1', 'kind': 'hypothesis', 'text': 'Synthetic cause.', 'refs': [reference],
             'native_approval': {'relation': 'supported'}, 'published_stance': 'accepted'}]},
        'native_report': {'status': 'completed', 'output': {'hypotheses': [
            {'cause': 'Synthetic cause.', 'evidence_explanation': 'Extra causal assertion.',
             'pending_checks': ['Check discriminator.'], 'status': 'supported'}],
             'recommended_actions': [{'risk': 'A risk statement.'}], 'missing_information': ['Need upstream cause.']}}}
    entry = {'report_id': 'native-report', 'case_id': 'test-case', 'repeat': 1, 'system_id': 'base',
        'split': 'calibration', 'stage': 'e2e', 'control_id': None, 'report_sha256': 'r' * 64}
    record = {'report': report, 'entry': entry}
    cases = {'test-case': case}
    materials = build_review_materials([record], cases, bundle_sha256='b' * 64)
    return materials, record, cases


def completed(materials, panel, person, artifact=None):
    name = {'evidence': 'evidence-review.template.json', 'root_match': 'root-review.template.json',
        'truth': 'truth-review.template.json'}[panel]
    document = deepcopy(materials[name])
    document.update(reviewer_type='human', reviewer_id=person, reviewed_at='2026-10-18T00:00:00Z',
        attestation=ATTESTATION)
    for row in document['rows']:
        row.update(status='reviewed', approved=True, reason='Synthetic test reviewer rationale.')
        if panel == 'truth':
            row.update(origin_confirmed=True, provenance_confirmed=True, identifiability_confirmed=True,
                scope_confirmed=True, identifiability='root_identifiable', causal_confirmation='reproduced_injection',
                limitations='Synthetic test only; not real gold.', reproduction_artifacts=[{
                    'path': str(artifact), 'sha256': sha_file(artifact), 'description': 'Synthetic attachment fixture.'}])
        else:
            row.update({name: True for name in EVIDENCE_FIELDS if panel == 'evidence'})
            row.update({name: True for name in ROOT_FIELDS if panel == 'root_match'})
            if panel == 'evidence':
                source = materials['evidence-packets.json']['packets'][0]['acquired_sources'][0]['source_id']
            else:
                row.update(wrong_definitive_cause=False, unsupported_definitive_cause=False, abstained_from_root=False)
                row['established_item_ids'] = [item['item_id'] for item in materials['root-match-packets.private.json']['packets'][0]['items'] if item['kind'] == 'hypothesis']
            for item in row['items']:
                item['reason'] = 'Synthetic item rationale.'
                if panel == 'evidence':
                    item.update(source_support='fully_entailed', source_ids=[source])
    return document


class RcaReviewTests(unittest.TestCase):
    def test_harness_failure_remains_exportable_as_no_output(self):
        _, record, cases = fixture()
        record['report'] = {'harness_failure': {'code': 'TIMEOUT'}, 'normalized': {},
            'native_report': {'status': 'harness_failed', 'execution_error': {'code': 'TIMEOUT'}}}
        materials = build_review_materials([record], cases, bundle_sha256='b' * 64)
        self.assertEqual(materials['evidence-packets.json']['packets'][0]['items'], [])
        self.assertFalse(import_reviews(materials, [])['formal_ready'])

    def test_query_quality_and_published_action_prose_are_bound(self):
        _, record, cases = fixture()
        record['report']['normalized']['queries'] = [{'name': 'logs', 'query_profile': {'start': 'now'},
            'status': 'error', 'error': {'code': 'SOURCE_UNAVAILABLE'}, 'truncated': False}]
        record['report']['native_report']['output']['recommended_actions'][0].update(
            condition='Actual condition', expected_result='Actual causal prediction')
        record['report']['native_report']['pending_actions'] = [{'action': 'Pending action',
            'condition': 'Pending condition', 'expected_result': 'Pending expected result',
            'risk': 'Pending risk', 'review_reason': 'Visible causal review explanation'}]
        materials = build_review_materials([record], cases, bundle_sha256='b' * 64)
        packet = materials['evidence-packets.json']['packets'][0]
        self.assertEqual(packet['query_quality'][0]['error']['code'], 'SOURCE_UNAVAILABLE')
        for value in ('Actual condition', 'Actual causal prediction', 'Pending action', 'Pending condition',
                'Pending expected result', 'Pending risk', 'Visible causal review explanation'):
            self.assertIn(value, str(packet))
        self.assertTrue(verify_report_bindings(materials, [record], cases, bundle_sha256='b' * 64))

    def test_supported_or_contradicted_item_requires_source(self):
        materials, _, _ = fixture()
        doc = completed(materials, 'evidence', 'synthetic-A')
        doc['rows'][0]['items'][0]['source_ids'] = []
        with self.assertRaisesRegex(ReviewError, 'SOURCE_SUPPORT_REQUIRES_ACQUIRED_SOURCE'):
            import_reviews(materials, [doc])

    def test_root_stance_is_second_step_only_and_refuted_alternative_does_not_taint_source_chain(self):
        from evals.rca_scoring import score_run
        _, record, cases = fixture()
        record['report']['normalized']['audit_items'].append({'id': 'C2', 'kind': 'hypothesis',
            'text': 'Wrong alternative.', 'refs': []})
        record['report']['native_report']['output']['hypotheses'].append(
            {'cause': 'Wrong alternative.', 'status': 'refuted'})
        materials = build_review_materials([record], cases, bundle_sha256='b' * 64)
        self.assertNotIn('refuted', str(materials['evidence-packets.json']))
        self.assertIn('refuted', str(materials['root-match-packets.private.json']))
        e = completed(materials, 'evidence', 'synthetic-A')
        r = completed(materials, 'root_match', 'synthetic-A')
        wrong_id = next(i['item_id'] for i in materials['evidence-packets.json']['packets'][0]['items']
            if i['visible_fields']['text'] == 'Wrong alternative.')
        next(i for i in e['rows'][0]['items'] if i['item_id'] == wrong_id)['source_support'] = 'contradicted'
        r['rows'][0]['established_item_ids'].remove(wrong_id)
        r['evidence_review_sha256'] = canonical_sha(e)
        joined = import_reviews(materials, [e, r], allow_single_reviewer=True)
        root = next(iter(joined['outcomes']['root_match'].values()))['judgment']
        self.assertTrue(root['established_assertions_supported'])
        self.assertTrue(score_run({}, root, 'root_identifiable', has_output=True,
            established_assertions_supported=root['established_assertions_supported'])['strict_root_success'])
        # If the output confirms the wrong alternative, the human second step
        # must identify that error; the application status is never itself gold.
        root['wrong_definitive_cause'] = True
        self.assertFalse(score_run({}, root, 'root_identifiable', has_output=True)['strict_root_success'])

    def test_human_case_provenance_does_not_approve_control_relation(self):
        materials, record, cases = fixture()
        cases['test-case']['truth']['fixed_controls'] = [{'control_id': 'T1', 'statement': 'Fixed claim', 'expected_relation': 'supported'}]
        materials = build_review_materials([record], cases, bundle_sha256='b' * 64)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'fixture.txt'
            artifact.write_text('SYNTHETIC TEST', encoding='utf-8')
            truth = completed(materials, 'truth', 'synthetic-A', artifact)
            joined = import_reviews(materials, [truth], allow_single_reviewer=True)
            self.assertIsNone(joined['outcomes']['truth']['test-case']['judgment']['fixed_controls']['T1'])
            truth['rows'][0]['fixed_controls'][0].update(status='reviewed', human_relation='partial', reason='Synthetic control rationale')
            joined = import_reviews(materials, [truth], allow_single_reviewer=True)
            self.assertEqual(joined['outcomes']['truth']['test-case']['judgment']['fixed_controls']['T1'], 'partial')

    def test_explicit_independent_human_adjudication_can_resolve_dispute(self):
        materials, _, _ = fixture()
        a, b = completed(materials, 'evidence', 'synthetic-A'), completed(materials, 'evidence', 'synthetic-B')
        b['rows'][0]['items'][0]['source_support'] = 'partially_entailed'
        c = completed(materials, 'evidence', 'synthetic-adjudicator')
        c.update(review_role='adjudicator', adjudication_of=[canonical_sha(a), canonical_sha(b)])
        joined = import_reviews(materials, [a, b, c])
        self.assertEqual(next(iter(joined['outcomes']['evidence'].values()))['status'], 'human_adjudicated')
        self.assertFalse(joined['formal_ready'])  # Other panels remain pending.
        c['adjudication_of'] = [canonical_sha(a)]
        with self.assertRaisesRegex(ReviewError, 'ADJUDICATION_REQUIRES'):
            import_reviews(materials, [a, b, c])

    def test_export_is_pending_and_blind_to_native_labels(self):
        materials, _, _ = fixture()
        packet = materials['evidence-packets.json']
        encoded = str(packet)
        for forbidden in ('native_approval', 'published_stance', 'completed', 'supported',
                'identifiability', 'truth_status', 'system_id', 'test-case'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(len(packet['packets'][0]['read_only_source_facts']), 1)
        self.assertEqual(len(packet['packets'][0]['items']), 5)
        for name in ('evidence-review.template.json', 'root-review.template.json', 'truth-review.template.json'):
            self.assertIsNone(materials[name]['reviewer_type'])
            self.assertTrue(all(row['approved'] is False and row['status'] == 'pending' for row in materials[name]['rows']))

    def test_all_user_visible_explanation_gap_and_risk_are_included(self):
        _, record, _ = fixture()
        text = str(visible_items(record['report']))
        for expected in ('Extra causal assertion.', 'Check discriminator.', 'A risk statement.', 'Need upstream cause.'):
            self.assertIn(expected, text)
        self.assertNotIn('Source says 7', text)

    def test_no_reviews_yields_pending_not_zero_accuracy(self):
        materials, _, _ = fixture()
        joined = import_reviews(materials, [])
        self.assertFalse(joined['formal_ready'])
        self.assertIsNone(next(iter(joined['outcomes']['evidence'].values()))['judgment'])

    def test_ai_cannot_self_declare_human_gold(self):
        materials, _, _ = fixture()
        doc = deepcopy(materials['evidence-review.template.json'])
        doc.update(reviewer_type='assistant', reviewer_id='AI', reviewed_at='today', attestation=ATTESTATION)
        with self.assertRaisesRegex(ReviewError, 'HUMAN_REVIEW_REQUIRED'):
            import_reviews(materials, [doc])
        doc['reviewer_type'] = 'human'
        doc['ai_generated_gold'] = True
        with self.assertRaisesRegex(ReviewError, 'AI_CANNOT_SUPPLY_GOLD'):
            import_reviews(materials, [doc])

    def test_source_ids_must_be_lists_of_actual_acquired_sources(self):
        materials, _, _ = fixture()
        for value in (['invented'], 'not-a-list', [None], ['duplicate', 'duplicate']):
            doc = completed(materials, 'evidence', 'synthetic-A')
            doc['rows'][0]['items'][0]['source_ids'] = value
            with self.assertRaisesRegex(ReviewError, 'UNKNOWN_OR_INVALID_REVIEW_SOURCE_ID'):
                import_reviews(materials, [doc])

    def test_complete_item_coverage_and_exact_hash_required(self):
        materials, _, _ = fixture()
        doc = completed(materials, 'evidence', 'synthetic-A')
        doc['rows'][0]['items'].pop()
        with self.assertRaisesRegex(ReviewError, 'INCOMPLETE_ITEM_COVERAGE'):
            import_reviews(materials, [doc])
        doc = completed(materials, 'evidence', 'synthetic-A')
        doc['rows'][0]['items'][0]['item_sha256'] = 'wrong'
        with self.assertRaisesRegex(ReviewError, 'ITEM_HASH_CHANGED'):
            import_reviews(materials, [doc])

    def test_root_step_requires_saved_evidence_by_same_reviewer(self):
        materials, _, _ = fixture()
        e = completed(materials, 'evidence', 'synthetic-A')
        r = completed(materials, 'root_match', 'synthetic-A')
        with self.assertRaisesRegex(ReviewError, 'ROOT_REQUIRES'):
            import_reviews(materials, [e, r])
        r['evidence_review_sha256'] = canonical_sha(e)
        self.assertFalse(import_reviews(materials, [e, r])['formal_ready'])
        r['reviewer_id'] = 'synthetic-B'
        with self.assertRaisesRegex(ReviewError, 'ROOT_REQUIRES'):
            import_reviews(materials, [e, r])

    def test_human_approval_without_provenance_cannot_upgrade_truth(self):
        materials, _, _ = fixture()
        doc = deepcopy(materials['truth-review.template.json'])
        doc.update(reviewer_type='human', reviewer_id='synthetic-A', reviewed_at='today', attestation=ATTESTATION)
        doc['rows'][0].update(status='reviewed', approved=True, reason='Just checked yes.')
        with self.assertRaisesRegex(ReviewError, 'TRUTH_PROVENANCE_NOT_CONFIRMED'):
            import_reviews(materials, [doc])

    def test_double_review_consensus_and_provenance_attachments(self):
        materials, _, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'synthetic-confirmation.txt'
            artifact.write_text('SYNTHETIC UNIT FIXTURE', encoding='utf-8')
            docs = []
            for person in ('synthetic-A', 'synthetic-B'):
                e = completed(materials, 'evidence', person)
                r = completed(materials, 'root_match', person)
                r['evidence_review_sha256'] = canonical_sha(e)
                docs.extend([e, r, completed(materials, 'truth', person, artifact)])
            joined = import_reviews(materials, docs)
            self.assertTrue(joined['formal_ready'])
            self.assertFalse(joined['ai_judgments_used_as_gold'])
            self.assertFalse(materials['truth-packets.private.json']['cases'][0]['candidate_truth']['provenance']['human_verified'])
            artifact.write_text('TAMPERED', encoding='utf-8')
            with self.assertRaisesRegex(ReviewError, 'CONFIRMATION_ARTIFACT_HASH_MISMATCH'):
                import_reviews(materials, docs)

    def test_disagreement_blocks_metrics_and_single_review_is_explicit(self):
        materials, _, _ = fixture()
        a, b = completed(materials, 'evidence', 'synthetic-A'), completed(materials, 'evidence', 'synthetic-B')
        b['rows'][0]['items'][0]['source_support'] = 'partially_entailed'
        joined = import_reviews(materials, [a, b])
        self.assertEqual(next(iter(joined['outcomes']['evidence'].values()))['status'], 'disputed')
        self.assertFalse(joined['formal_ready'])
        joined = import_reviews(materials, [a], allow_single_reviewer=True)
        self.assertEqual(joined['review_design'], 'single_human_explicitly_disclosed')
        self.assertEqual(next(iter(joined['outcomes']['evidence'].values()))['status'], 'human_consensus')

    def test_projection_rebuild_detects_source_output_and_reference_tampering(self):
        materials, record, cases = fixture()
        self.assertTrue(verify_report_bindings(materials, [record], cases, bundle_sha256='b' * 64))
        for change in ('text', 'source', 'reference'):
            altered = deepcopy(record)
            if change == 'text':
                altered['report']['normalized']['audit_items'][1]['text'] = 'Changed cause'
            elif change == 'source':
                altered['report']['normalized']['observations'][0]['payload']['value'] = 8
            else:
                altered['report']['normalized']['audit_items'][1]['refs'][0]['value'] = 8
            with self.assertRaisesRegex(ReviewError, 'REVIEW_SOURCE_OUTPUT_OR_REFERENCE_CHANGED'):
                verify_report_bindings(materials, [altered], cases, bundle_sha256='b' * 64)

    def test_packet_and_template_tampering_rejected_before_import(self):
        materials, _, _ = fixture()
        changed = deepcopy(materials)
        changed['evidence-packets.json']['packets'][0]['items'][0]['visible_fields']['text'] = 'Changed'
        with self.assertRaisesRegex(ReviewError, 'PANEL_HASH_MISMATCH'):
            import_reviews(changed, [])
        changed = deepcopy(materials)
        changed['evidence-review.template.json']['rows'][0]['items'].pop()
        with self.assertRaisesRegex(ReviewError, 'TEMPLATE_ITEM_BINDING_CHANGED'):
            import_reviews(changed, [])


if __name__ == '__main__':
    unittest.main()
