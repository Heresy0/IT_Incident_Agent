import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evals import rca_cross_case_scoring as scoring
from evals.rca_scoring import ReviewError, sha_file


class CrossCaseScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(scoring, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.limits = {'model_calls': 16, 'tool_calls': 8, 'web_calls': 12, 'seconds': 180}
        self.header = {'reviewer_type': 'ai', 'nonblind': True, 'human_verified': False, 'is_gold': False}
        self.cases = []
        for n in (1, 2):
            cid = 'case_' + str(n)
            observations = [{'id': 'row_' + str(i), 'message': 'measured fact ' + str(i),
                'service': cid, 'category': 'resource', 'visibility': 'public_synthetic'} for i in (1, 2, 3)]
            self.cases.append({'case_id': cid, 'independence_group': 'group_' + str(n),
                'scenario_family': 'family_' + str(n), 'public': {'world': {
                    'ticket': {'symptoms': 'Failed operation'}, 'logs': observations,
                    **{k: [] for k in ('metrics', 'changes', 'owners', 'runbooks', 'incidents')}}},
                'truth': {'identifiability': 'root_identifiable', 'evidence_criteria': [{
                    'criterion_id': 'causal_chain', 'required': True,
                    'acceptable_observation_sets': [['row_1', 'row_2'], ['row_1', 'row_3']]}],
                    'acquisition_eligibility': {'required_evidence_available': True,
                        'initial_ticket_contains_required_evidence': False, 'rationale': 'Neutral ticket, reachable sources.'}}})
        self.dataset = {'fingerprint': 'dataset-sha', 'cases': self.cases}
        plan = {'model': 'qwen3.7-flash', 'model_settings': {'temperature': 0},
            'application': {'source_version': 'frozen'}, 'provider_configuration_sha256': 'provider-sha',
            'limits': self.limits, 'stages': ['e2e', 'sufficient_evidence'], 'repeats': 3,
            'cases': ['case_1', 'case_2'], 'planned_runs': 12}
        self.bundle = {'plan': plan, 'execution_mode': 'live_observation_only', 'runs': [], 'scheduled_pending': []}
        self.records = []
        for case in self.cases:
            for stage in plan['stages']:
                for repeat in (1, 2, 3):
                    rid = case['case_id'] + '_' + stage + '_' + str(repeat)
                    identity = {'report_id': rid, 'case_id': case['case_id'], 'stage': stage,
                        'repeat': repeat, 'system_id': 'frozen', 'control_id': None}
                    report = {**identity, **{k: plan[k] for k in ('model_settings', 'application', 'provider_configuration_sha256')},
                        'dataset_fingerprint': self.dataset['fingerprint'], 'worker_wall_seconds': repeat,
                        'run_summary': {'limits': self.limits, 'duration_ms': repeat * 1000,
                            'model_calls': 1, 'tool_calls': 2, 'web_calls': 0},
                        'native_report': {'status': 'completed', 'output': {}, 'events': []},
                        'normalized': {'observations': [self.observation(row) for row in case['public']['world']['logs']],
                            'audit_items': [{'kind': 'hypothesis', 'text': 'The local causal condition causes failure.', 'refs': []}],
                            'queries': [], 'failures': []},
                        'model_call_log': [{'model': plan['model'], 'response_model_name': plan['model'],
                            'charged_request': True, 'usage': {'input_tokens': 100, 'output_tokens': 10}, 'seconds': .3}]}
                    entry = {**identity, 'report_file': rid + '.json'}
                    path = self.root / entry['report_file']
                    record = {'entry': entry, 'report': report, 'report_path': str(path)}
                    self.save(record)
                    self.records.append(record)
                    self.bundle['runs'].append(entry)
        self.bundle['paid_budget'] = {'session_requests': 12, 'charged_delta': {
            'requests': 12, 'input_tokens': 1200, 'output_tokens': 120, 'model_seconds': 3.6},
            'unknown_usage_calls_delta': 0, 'admission_closed': False}
        truth_path = self.root / 'truth.json'
        truth_path.write_text(json.dumps({'causal_reference': 'Controlled intervention proves local condition.'}), encoding='utf-8')
        self.document = {'schema_version': 'rca-ai-cross-case-adjudication-v1', 'status': 'completed',
            **self.header, 'cases': []}
        for case in self.cases:
            self.document['cases'].append({**self.header, 'case_id': case['case_id'], 'truth': {
                'identifiability': 'root_identifiable', 'rationale': 'Independent local mechanism and intervention.',
                'citations': [{'path': 'truth.json', 'sha256': sha_file(truth_path), 'json_path': '$.causal_reference'}]},
                'native_runs': [self.judgment(r) for r in self.records if r['entry']['case_id'] == case['case_id']]})

    @staticmethod
    def observation(row):
        return {'observation_id': row['id'], 'locator': 'get_service_logs/' + row['id'],
            'evidence_id': 'EV_' + row['id'], 'payload': {k: v for k, v in row.items() if k != 'visibility'},
            'source_fields': {'excerpt': row['message'], 'truncated': False}}

    def save(self, record):
        Path(record['report_path']).write_text(json.dumps(record['report'], ensure_ascii=False), encoding='utf-8')
        record['entry']['report_sha256'] = sha_file(record['report_path'])

    def judgment(self, record):
        e = record['entry']
        flags = {k: k not in {'wrong_definitive_cause', 'unsupported_definitive_cause', 'abstained_from_root',
            'missing_discriminating_information_identified', 'discriminating_next_step_proposed'} for k in scoring.JUDGMENT_FIELDS}
        return {**{k: e[k] for k in ('report_id', 'case_id', 'stage', 'repeat', 'report_sha256')},
            'judgment': flags, 'rationale_by_field': {k: 'Read final assertions and actual evidence.' for k in flags},
            'decisive_citations': [{'report_path': e['report_file'], 'json_path': '$.normalized.audit_items'}],
            'final_claim_decisions': []}

    def score(self, document=None):
        return scoring.build_scorecard(self.bundle, self.dataset, self.records, document)

    def test_missing_adjudication_preserves_mechanical_metrics_without_zero_accuracy(self):
        result = self.score()
        self.assertIsNone(result['ai_adjudicated_metrics'])
        self.assertEqual(result['adjudication_coverage']['rate'], 0)
        self.assertEqual(result['pending_case_ids'], ['case_1', 'case_2'])
        self.assertEqual(result['stage_metrics']['e2e']['autonomous_key_evidence_success']['rate'], 1)
        self.assertIsNone(result['stage_metrics']['sufficient_evidence']['autonomous_key_evidence_success']['rate'])
        self.assertEqual(result['stage_metrics']['sufficient_evidence']['complete_stored_key_evidence_coverage']['rate'], 1)

    def test_complete_ai_adjudication_uses_existing_formula_and_case_denominator(self):
        result = self.score(self.document)
        metrics = result['ai_adjudicated_metrics']['frozen:e2e']
        self.assertEqual(metrics['strict_root_location_run_rate'], {'numerator': 6, 'denominator': 6, 'rate': 1})
        self.assertEqual(metrics['strict_root_location_case_all_repeats']['denominator'], 2)
        self.assertEqual(result['independent_case_count'], 2)
        self.assertFalse(result['human_verified'])
        self.assertIsNone(result['human_verified_formal_metrics'])
        self.assertEqual(result['citation_integrity']['citations_checked'], 14)

    def test_partial_case_review_does_not_score_pending_case_as_failure(self):
        self.document['cases'].pop()
        self.document['status'] = 'partial'
        result = self.score(self.document)
        self.assertEqual(result['adjudication_coverage']['rate'], .5)
        self.assertEqual(result['ai_adjudicated_metrics']['frozen:e2e']['strict_root_location_run_rate']['denominator'], 3)
        self.assertEqual(result['pending_case_ids'], ['case_2'])

    def test_incomplete_completed_adjudication_is_rejected(self):
        self.document['cases'].pop()
        with self.assertRaisesRegex(ReviewError, 'COVERAGE_INCOMPLETE'):
            self.score(self.document)

    def test_duplicate_reports_or_missing_repeats_rejected(self):
        self.records[-1] = self.records[0]
        with self.assertRaisesRegex(ReviewError, 'DUPLICATE_RECORD'):
            self.score()

    def test_correlated_families_cannot_inflate_independent_case_count(self):
        self.cases[1]['independence_group'] = self.cases[0]['independence_group']
        with self.assertRaisesRegex(ReviewError, 'INDEPENDENCE_GROUP_REUSED'):
            self.score()

    def test_source_id_alone_and_false_field_value_do_not_count_as_acquired(self):
        report = copy.deepcopy(self.records[0]['report'])
        report['normalized']['observations'] = [{'payload': {'id': 'row_1'}}]
        result = scoring.acquisition_metrics(self.cases[0], report, stage='e2e')
        self.assertFalse(result['all_required_criteria_satisfied'])
        self.assertEqual(result['source_mismatches'][0]['reason'], 'field_values_or_missing_fields')
        report['normalized']['observations'] = [self.observation(row) for row in self.cases[0]['public']['world']['logs'][:2]]
        report['normalized']['observations'][0]['payload']['message'] = 'made up'
        self.assertFalse(scoring.acquisition_metrics(self.cases[0], report, stage='e2e')['all_required_criteria_satisfied'])

    def test_any_acceptable_observation_set_satisfies_criterion(self):
        report = copy.deepcopy(self.records[0]['report'])
        report['normalized']['observations'].pop(1)
        self.assertTrue(scoring.acquisition_metrics(self.cases[0], report, stage='e2e')['autonomous_key_evidence_success'])

    def test_partial_row_coverage_reported_even_before_one_criterion_completed(self):
        report = copy.deepcopy(self.records[0]['report'])
        report['normalized']['observations'] = report['normalized']['observations'][:1]
        result = scoring.acquisition_metrics(self.cases[0], report, stage='e2e')
        self.assertTrue(result['partial_only'])
        self.assertEqual(result['required_criteria_covered'], 0)
        self.assertEqual(result['best_observation_set_coverage_mean'], .5)

    def test_truncation_and_exposure_are_diagnostics_not_semantic_use(self):
        report = copy.deepcopy(self.records[0]['report'])
        report['normalized']['observations'][0]['source_fields']['truncated'] = True
        report['normalized']['queries'] = [{'truncated': True}]
        result = scoring.acquisition_metrics(self.cases[0], report, stage='e2e')
        self.assertEqual(result['truncated_observation_ids'], ['row_1'])
        self.assertEqual(result['collection_truncated_queries'], 1)
        self.assertIsNone(result['semantic_evidence_utilization'])

    def test_initial_ticket_evidence_excluded_from_autonomous_denominator(self):
        self.cases[0]['truth']['acquisition_eligibility']['initial_ticket_contains_required_evidence'] = True
        result = self.score()
        self.assertEqual(result['stage_metrics']['e2e']['autonomous_key_evidence_success']['denominator'], 3)
        self.assertEqual(result['stage_metrics']['e2e']['acquisition_ineligible_cases'], ['case_1'])

    def test_ai_cannot_be_relabelled_human_or_gold(self):
        for key, value in [('reviewer_type', 'human'), ('is_gold', True), ('human_verified', True), ('nonblind', False)]:
            document = copy.deepcopy(self.document)
            document[key] = value
            with self.assertRaises(ReviewError):
                self.score(document)

    def test_wrong_hash_or_citation_and_nonbinary_flags_rejected(self):
        document = copy.deepcopy(self.document)
        document['cases'][0]['native_runs'][0]['report_sha256'] = 'bad'
        with self.assertRaisesRegex(ReviewError, 'IDENTITY_MISMATCH'):
            self.score(document)
        document = copy.deepcopy(self.document)
        document['cases'][0]['native_runs'][0]['judgment']['scope_correct'] = 1
        with self.assertRaisesRegex(ReviewError, 'BINARY_JUDGMENT'):
            self.score(document)
        document = copy.deepcopy(self.document)
        document['cases'][0]['native_runs'][0]['decisive_citations'][0]['json_path'] = '$.missing'
        with self.assertRaisesRegex(ReviewError, 'JSON_PATH_MISSING'):
            self.score(document)

    def test_in_memory_report_must_still_match_saved_bytes(self):
        self.records[0]['report']['native_report']['status'] = 'partial'
        with self.assertRaisesRegex(ReviewError, 'LOADED_REPORT_BYTES_MISMATCH'):
            self.score()

    def test_paid_ledger_mismatch_is_never_silently_accepted(self):
        self.bundle['paid_budget']['charged_delta']['input_tokens'] = 1201
        with self.assertRaisesRegex(ReviewError, 'LEDGER_TOKEN_MISMATCH'):
            self.score()

    def test_latency_uses_nearest_rank_p95_and_delivery_not_accuracy(self):
        result = self.score()
        stage = result['stage_metrics']['e2e']
        self.assertEqual(stage['worker_wall_seconds']['mean'], 2)
        self.assertEqual(stage['worker_wall_seconds']['median'], 2)
        self.assertEqual(stage['worker_wall_seconds']['p95'], 3)
        self.assertEqual(stage['delivery_completed']['rate'], 1)
        self.assertIsNone(result['ai_adjudicated_metrics'])

    def test_harness_failure_stays_in_completion_and_acquisition_denominators(self):
        record = self.records[0]
        record['report']['harness_failure'] = {'code': 'WORKER_ERROR'}
        record['report']['native_report'] = {'status': 'harness_failed'}
        record['report']['normalized'] = {}
        record['report']['run_summary'] = {}
        self.save(record)
        result = self.score()
        stage = result['stage_metrics']['e2e']
        self.assertEqual(stage['delivery_completed']['numerator'], 5)
        self.assertEqual(stage['delivery_completed']['denominator'], 6)
        self.assertEqual(stage['autonomous_key_evidence_success']['numerator'], 5)
        self.assertEqual(stage['autonomous_key_evidence_success']['denominator'], 6)


if __name__ == '__main__':
    unittest.main()
