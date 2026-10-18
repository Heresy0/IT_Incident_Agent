"""Offline cross-mechanism evaluation with disclosed AI adjudication.

Acquisition, delivery and resources are mechanical observations. Root judgments
reuse the existing formula and remain AI-origin, nonblind development scores.
Missing adjudications produce coverage, never artificial zero accuracy.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

from evals.rca_scoring import (ROOT_FIELDS, aggregate_case_metrics, canonical_sha,
    mechanical_references_valid, read_json, require, resource_metrics, score_run, sha_file)

ROOT = Path(__file__).resolve().parents[1]
JUDGMENT_FIELDS = ROOT_FIELDS + ('established_assertions_supported',)
STAGES = {'e2e', 'sufficient_evidence'}
CHANNEL_TO_TOOL = {'metrics': 'get_service_metrics', 'logs': 'get_service_logs',
    'changes': 'get_recent_changes', 'owners': 'get_service_owner',
    'runbooks': 'search_runbooks', 'incidents': 'search_similar_incidents'}
# These are the fixture provider's public payload fields, not private row metadata.
PUBLIC_PAYLOAD_FIELDS = {'id', 'timestamp', 'service', 'environment', 'data_version',
    'metric', 'value', 'unit', 'aggregation', 'granularity', 'category', 'level',
    'error_code', 'message', 'version', 'summary', 'config_summary', 'team',
    'escalation', 'aliases', 'text', 'versions', 'updated_at', 'confirmed_at',
    'confirmed', 'resolution', 'validity'}


def _ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
        'rate': numerator / denominator if denominator else None}


def _origin(value):
    require(value.get('reviewer_type') == 'ai' and value.get('nonblind') is True,
        'DISCLOSED_NONBLIND_AI_ORIGIN_REQUIRED')
    require(value.get('human_verified') is False and value.get('is_gold') is False,
        'AI_CANNOT_ATTEST_HUMAN_OR_GOLD')


def _source_catalog(case):
    result = {}
    for channel, tool in CHANNEL_TO_TOOL.items():
        for row in case['public']['world'][channel]:
            require(row['id'] not in result, 'DUPLICATE_PUBLIC_SOURCE_ID')
            result[row['id']] = {'tool': tool, 'payload': {
                k: v for k, v in row.items() if k in PUBLIC_PAYLOAD_FIELDS}}
    return result


def acquisition_metrics(case, report, *, stage):
    """Require actual source identity AND exact public payload, not an ID mention.

    Payload collection does not establish model exposure or semantic utilization.
    Excerpt and query truncation are therefore reported separately.
    """
    require(stage in STAGES, 'UNKNOWN_ACQUISITION_STAGE')
    catalog = _source_catalog(case)
    acquired, mismatches, truncated, excerpts = set(), [], [], []
    for row in report.get('normalized', {}).get('observations', []):
        payload = row.get('payload', {})
        locator = row.get('locator', '')
        locator_id = locator.rsplit('/', 1)[-1] if '/' in locator else None
        identities = {v for v in (row.get('observation_id'), payload.get('id'), locator_id) if v}
        if len(identities) != 1 or next(iter(identities), None) not in catalog:
            mismatches.append({'evidence_id': row.get('evidence_id'), 'reason': 'source_identity'})
            continue
        oid = next(iter(identities))
        expected = catalog[oid]
        if locator and locator != expected['tool'] + '/' + oid:
            mismatches.append({'observation_id': oid, 'reason': 'source_tool'})
            continue
        # Public provider projections omit visibility and experiment-only metadata.
        actual = {k: v for k, v in payload.items() if k in PUBLIC_PAYLOAD_FIELDS}
        if canonical_sha(actual) != canonical_sha(expected['payload']):
            mismatches.append({'observation_id': oid, 'reason': 'field_values_or_missing_fields'})
            continue
        acquired.add(oid)
        fields = row.get('source_fields', {})
        is_truncated = bool(payload.get('content_truncated') or fields.get('truncated'))
        if is_truncated:
            truncated.append(oid)
        excerpt = fields.get('excerpt')
        excerpts.append({'observation_id': oid, 'evidence_id': row.get('evidence_id'),
            'excerpt_chars': len(excerpt) if isinstance(excerpt, str) else None,
            'payload_truncated': is_truncated,
            'model_prompt_exposure': 'not_inferred_from_stored_payload'})
    criteria = []
    for criterion in case['truth']['evidence_criteria']:
        alternatives = criterion['acceptable_observation_sets']
        require(bool(alternatives) and all(isinstance(s, list) and bool(s)
            and set(s) <= set(catalog) for s in alternatives), 'INVALID_ACQUISITION_CRITERION')
        criteria.append({'criterion_id': criterion['criterion_id'],
            'required': criterion.get('required', True),
            'satisfied': any(set(ids) <= acquired for ids in alternatives),
            'acceptable_observation_sets': alternatives,
            'best_alternative_coverage': max(len(set(ids) & acquired) / len(set(ids)) for ids in alternatives)})
    required = [c for c in criteria if c['required']]
    eligibility = case['truth'].get('acquisition_eligibility', {})
    eligible = bool(required and eligibility.get('required_evidence_available') is True
        and eligibility.get('initial_ticket_contains_required_evidence') is False
        and isinstance(eligibility.get('rationale'), str) and eligibility['rationale'].strip())
    covered = sum(c['satisfied'] for c in required)
    complete = bool(required) and covered == len(required)
    return {'eligible': eligible, 'eligibility': eligibility,
        'autonomous_acquisition_scored': stage == 'e2e' and eligible,
        'all_required_criteria_satisfied': complete,
        'autonomous_key_evidence_success': complete if stage == 'e2e' and eligible else None,
        'required_criteria_covered': covered, 'required_criteria_count': len(required),
        'required_criteria_coverage': covered / len(required) if required else None,
        'partial_only': bool(not complete and any(c['best_alternative_coverage'] > 0 for c in required)),
        'best_observation_set_coverage_mean': sum(c['best_alternative_coverage'] for c in required)
            / len(required) if required else None, 'criteria': criteria,
        'acquired_verified_observation_ids': sorted(acquired), 'source_mismatches': mismatches,
        'stored_evidence_excerpts': excerpts, 'truncated_observation_ids': sorted(set(truncated)),
        'collection_truncated_queries': sum(bool(q.get('truncated'))
            for q in report.get('normalized', {}).get('queries', [])),
        'semantic_evidence_utilization': None,
        'interpretation': 'sufficient_evidence_is_preset_diagnostic_not_autonomous_acquisition'
            if stage == 'sufficient_evidence' else 'actual_stored_public_payload_coverage_not_semantic_use'}


def _validate_inputs(bundle, dataset, records):
    plan = bundle['plan']
    repeats = plan['repeats']
    require(type(repeats) is int and repeats == 3, 'THREE_REPETITIONS_REQUIRED')
    require(len(plan['stages']) == len(STAGES) and set(plan['stages']) == STAGES,
        'E2E_AND_PRESET_DIAGNOSTIC_REQUIRED')
    require(bundle.get('execution_mode') == 'live_observation_only', 'LIVE_OBSERVATIONS_REQUIRED')
    require(not bundle.get('scheduled_pending'), 'EXECUTION_SCHEDULE_PENDING')
    require(plan.get('model') == 'qwen3.7-flash', 'FIXED_FLASH_REQUIRED')
    cases = {c['case_id']: c for c in dataset['cases'] if c['case_id'] in plan['cases']}
    require(bool(cases) and len(cases) == len(plan['cases']) and len(cases) == len(set(plan['cases'])), 'CASE_SET_MISMATCH')
    require(len({c['independence_group'] for c in cases.values()}) == len(cases),
        'INDEPENDENCE_GROUP_REUSED')
    require(len({c['scenario_family'] for c in cases.values()}) == len(cases), 'MECHANISM_FAMILY_REUSED')
    require(all(c['truth']['identifiability'] == 'root_identifiable' for c in cases.values()),
        'CROSS_ROOT_CASE_MUST_BE_ROOT_IDENTIFIABLE')
    expected = {(cid, stage, repeat) for cid in cases for stage in STAGES for repeat in range(1, repeats + 1)}
    actual, report_ids = set(), set()
    require(len(records) == plan['planned_runs'] == len(expected) == len(bundle['runs']), 'INCOMPLETE_RUN_SET')
    entries = {e['report_id']: e for e in bundle['runs']}
    require(len(entries) == len(bundle['runs']), 'DUPLICATE_BUNDLE_REPORT')
    for record in records:
        entry, report = record['entry'], record['report']
        rid = entry['report_id']
        require(rid not in report_ids and entries.get(rid) == entry, 'UNKNOWN_OR_DUPLICATE_RECORD')
        report_ids.add(rid)
        key = (entry['case_id'], entry['stage'], entry['repeat'])
        require(key in expected and key not in actual, 'UNEXPECTED_OR_DUPLICATE_RUN')
        actual.add(key)
        require(all(report.get(k) == entry.get(k) for k in
            ('report_id', 'case_id', 'stage', 'repeat', 'system_id', 'control_id')), 'REPORT_IDENTITY_MISMATCH')
        require(sha_file(record['report_path']) == entry['report_sha256'], 'REPORT_HASH_MISMATCH')
        require(canonical_sha(read_json(record['report_path'])) == canonical_sha(report), 'LOADED_REPORT_BYTES_MISMATCH')
        require(report.get('dataset_fingerprint') == dataset['fingerprint'], 'DATASET_FINGERPRINT_MISMATCH')
        require(report.get('model_settings') == plan.get('model_settings'), 'MODEL_SETTINGS_MISMATCH')
        require(report.get('harness_failure') or report.get('run_summary', {}).get('limits') == plan.get('limits'),
            'REPORT_LIMITS_MISMATCH')
        require(report.get('provider_configuration_sha256') == plan.get('provider_configuration_sha256'),
            'PROVIDER_CONFIGURATION_MISMATCH')
        require(report.get('application') == plan.get('application'), 'APPLICATION_MISMATCH')
        for call in report.get('model_call_log', []):
            require(call.get('model') == plan['model'] and (not call.get('response_model_name')
                or call['response_model_name'] == plan['model']),
                'ACTUAL_MODEL_MISMATCH')
    require(actual == expected and len({r['entry']['system_id'] for r in records}) == 1,
        'INCOMPLETE_OR_MIXED_SYSTEM_RUNS')
    return cases, repeats


def _ledger_metrics(bundle, resources):
    ledger = bundle.get('paid_budget', {})
    require(bool(ledger), 'PAID_LEDGER_REQUIRED')
    calls = sum(r['charged_calls'] for r in resources)
    unknown = sum(r['unknown_usage_calls'] for r in resources)
    delta = ledger.get('charged_delta', {})
    require(ledger.get('session_requests') == delta.get('requests') == calls, 'LEDGER_CALL_MISMATCH')
    require(ledger.get('unknown_usage_calls_delta') == unknown, 'LEDGER_UNKNOWN_USAGE_MISMATCH')
    if not unknown:
        require(delta.get('input_tokens') == sum(r['input_tokens_known'] for r in resources)
            and delta.get('output_tokens') == sum(r['output_tokens_known'] for r in resources), 'LEDGER_TOKEN_MISMATCH')
    initial, final = ledger.get('initial', {}), ledger.get('final', {})
    for field in ('requests', 'input_tokens', 'output_tokens', 'model_seconds'):
        if field in initial and field in final and field in delta:
            require(abs(final[field] - initial[field] - delta[field]) < 0.00001, 'LEDGER_DELTA_MISMATCH:' + field)
    return {'status': 'reconciled', 'charged_calls': calls, 'unknown_usage_calls': unknown,
        'ledger_accounted_input_tokens': delta.get('input_tokens'),
        'ledger_accounted_output_tokens': delta.get('output_tokens'),
        'unknown_usage_reservations_are_not_actual_token_measurements': bool(unknown),
        'admission_closed': ledger.get('admission_closed'), 'remaining': ledger.get('remaining')}


def _adjudicate(document, cases, records, repeats):
    if document is None:
        return [], {'status': 'not_supplied', 'citations_checked': 0}
    _origin(document)
    require(document.get('schema_version') == 'rca-ai-cross-case-adjudication-v1', 'WRONG_ADJUDICATION_SCHEMA')
    require(document.get('status') in {'completed', 'partial'}, 'ADJUDICATION_STATUS_REQUIRED')
    native = {r['entry']['report_id']: r for r in records}
    reviewed, case_ids, rows = set(), set(), []
    for case in document.get('cases', []):
        _origin(case)
        cid = case['case_id']
        require(cid in cases and cid not in case_ids, 'UNKNOWN_OR_DUPLICATE_ADJUDICATED_CASE')
        case_ids.add(cid)
        truth = case['truth']
        require(truth.get('identifiability') == cases[cid]['truth']['identifiability']
            and bool(truth.get('rationale')) and bool(truth.get('citations')), 'TRUTH_JUDGMENT_INCOMPLETE')
        require(len(case['native_runs']) == len(STAGES) * repeats, 'INCOMPLETE_CASE_ADJUDICATION')
        for judgment in case['native_runs']:
            rid = judgment['report_id']
            require(rid in native and rid not in reviewed, 'UNKNOWN_OR_DUPLICATE_NATIVE_JUDGMENT')
            record = native[rid]
            entry, report = record['entry'], record['report']
            require(entry['case_id'] == cid and all(judgment.get(k) == entry.get(k)
                for k in ('case_id', 'stage', 'repeat', 'report_sha256')), 'ADJUDICATION_IDENTITY_MISMATCH')
            flags = judgment['judgment']
            require(set(flags) == set(JUDGMENT_FIELDS) and all(type(flags[k]) is bool for k in JUDGMENT_FIELDS),
                'COMPLETE_BINARY_JUDGMENT_REQUIRED')
            require(all(isinstance(judgment.get('rationale_by_field', {}).get(k), str)
                and judgment['rationale_by_field'][k].strip() for k in JUDGMENT_FIELDS)
                and bool(judgment.get('decisive_citations'))
                and isinstance(judgment.get('final_claim_decisions'), list), 'JUDGMENT_RATIONALE_REQUIRED')
            from scripts.review_rca import visible_items
            metrics = score_run({}, flags, truth['identifiability'], has_output=bool(visible_items(report)),
                delivery_failed=bool(report.get('execution_error') or report.get('harness_failure')
                    or report.get('native_report', {}).get('execution_error')),
                mechanical_references_valid=mechanical_references_valid(report),
                established_assertions_supported=flags['established_assertions_supported'])
            rows.append({'report_id': rid, 'case_id': cid, 'condition': entry['system_id'],
                'mode': entry['stage'], 'repeat': entry['repeat'], 'identifiability': truth['identifiability'],
                'judgment': flags, 'metrics': metrics})
            reviewed.add(rid)
    if document['status'] == 'completed':
        require(reviewed == set(native), 'COMPLETED_ADJUDICATION_COVERAGE_INCOMPLETE')
    # Reuse existing path/hash validator without requiring irrelevant diagnostics.
    adapted = deepcopy(document)
    for case in adapted.get('cases', []):
        case['diagnostic_runs'] = []
    from scripts.score_rca_ai_adjudication import verify_citations
    integrity = verify_citations(adapted, records, artifact_root=ROOT)
    return rows, integrity


def build_scorecard(bundle, dataset, records, adjudication, rate_card=None):
    """Score a complete fixed-Flash run set; judgments may be wholly/partly absent."""
    cases, repeats = _validate_inputs(bundle, dataset, records)
    rows, integrity = _adjudicate(adjudication, cases, records, repeats)
    acquisition, resources = [], []
    for record in records:
        entry, report = record['entry'], record['report']
        identity = {k: entry[k] for k in ('report_id', 'case_id', 'stage', 'repeat')}
        acquisition.append({**identity, **acquisition_metrics(cases[entry['case_id']], report, stage=entry['stage'])})
        resources.append({**identity, **resource_metrics(report, rate_card, limits=bundle['plan']['limits'])})
    from scripts.summarize_frozen_versions import distribution, estimate_cost
    stage_metrics = {}
    for stage in sorted(STAGES):
        selected = [r for r in resources if r['stage'] == stage]
        evidence = [r for r in acquisition if r['stage'] == stage]
        eligible = [r for r in evidence if r['autonomous_acquisition_scored']]
        calls = [c for r in records if r['entry']['stage'] == stage for c in r['report'].get('model_call_log', [])
            if c.get('charged_request')]
        evidence_groups = defaultdict(list)
        for row in eligible:
            evidence_groups[row['case_id']].append(row)
        stage_metrics[stage] = {
            'runs': len(selected), 'independent_cases': len(cases),
            'delivery_completed': _ratio(sum(r['delivery_status'] == 'completed' for r in selected), len(selected)),
            'delivery_statuses': dict(Counter(r['delivery_status'] for r in selected)),
            'autonomous_key_evidence_success': _ratio(sum(r['autonomous_key_evidence_success'] for r in eligible), len(eligible)),
            'autonomous_key_evidence_case_majority': _ratio(sum(sum(r['autonomous_key_evidence_success'] for r in group)
                >= repeats // 2 + 1 for group in evidence_groups.values()), len(evidence_groups)),
            'autonomous_key_evidence_case_all_repeats': _ratio(sum(all(r['autonomous_key_evidence_success']
                for r in group) for group in evidence_groups.values()), len(evidence_groups)),
            'acquisition_ineligible_cases': sorted({r['case_id'] for r in evidence if not r['eligible']}),
            'required_criterion_coverage': _ratio(sum(r['required_criteria_covered'] for r in evidence),
                sum(r['required_criteria_count'] for r in evidence)),
            'complete_stored_key_evidence_coverage': _ratio(sum(r['all_required_criteria_satisfied'] for r in evidence), len(evidence)),
            'partial_only_runs': sum(r['partial_only'] for r in evidence),
            'best_observation_set_coverage_mean': distribution([r['best_observation_set_coverage_mean'] for r in evidence]),
            'charged_model_calls': sum(r['charged_calls'] for r in selected),
            'average_charged_model_calls': sum(r['charged_calls'] for r in selected) / len(selected),
            'known_usage_calls': sum(r['known_usage_calls'] for r in selected),
            'unknown_usage_calls': sum(r['unknown_usage_calls'] for r in selected),
            'input_tokens_known': sum(r['input_tokens_known'] for r in selected),
            'output_tokens_known': sum(r['output_tokens_known'] for r in selected),
            'cost': estimate_cost(calls, rate_card, pricing_scope=rate_card.get('pricing_scope') if rate_card else None),
            'worker_wall_seconds': distribution([r['worker_wall_seconds'] for r in selected]),
            'native_duration_seconds': distribution([r['duration_ms'] / 1000
                if isinstance(r['duration_ms'], (int, float)) else None for r in selected]),
            'per_run_cost': distribution([r['cost'].get('estimated_total') for r in selected]),
            'hard_budget_violation_runs': sum(bool(r['hard_budget']['violations']) for r in selected),
            'provider_failures': sum(r['provider_failures'] for r in selected)}
    coverage = _ratio(len(rows), len(records))
    return {'schema_version': 'rca-ai-cross-case-scorecard-v1',
        'status': 'scored' if len(rows) == len(records) else 'pending_or_partial_ai_adjudication',
        'reviewer_type': 'ai', 'adjudication_source': 'ai', 'nonblind': True,
        'human_verified': False, 'is_gold': False, 'human_verified_formal_metrics': None,
        'production_accuracy': None, 'independent_holdout_accuracy': None,
        'dataset_fingerprint': dataset['fingerprint'], 'model': bundle['plan']['model'],
        'model_settings': bundle['plan']['model_settings'], 'limits': bundle['plan']['limits'],
        'independent_case_count': len(cases), 'mechanism_family_count': len(cases),
        'repeats_per_case_stage': repeats, 'reports': len(records),
        'adjudication_coverage': coverage, 'adjudicated_case_count': len({r['case_id'] for r in rows}),
        'pending_case_ids': sorted(set(cases) - {r['case_id'] for r in rows}),
        'ai_adjudicated_metrics': aggregate_case_metrics(rows, expected_repeats=repeats) if rows else None,
        'native_run_scores': rows, 'citation_integrity': integrity, 'stage_metrics': stage_metrics,
        'paid_ledger': _ledger_metrics(bundle, resources),
        'acquisition_run_metrics': acquisition, 'resource_run_metrics': resources,
        'new_evaluated_provider_calls': 0,
        'limitations': ['AI-origin nonblind development-case adjudication; no human gold or production claim.',
            'Distinct mechanism groups are independent constructed cases; repeated model runs are correlated.',
            'Root success, autonomous acquisition and delivery completion have separate denominators.',
            'Stored payload acquisition does not prove model exposure or semantic evidence use.',
            'Preset sufficient-evidence runs are diagnostic and excluded from autonomous acquisition.',
            'Wrong confirmations use the final whole published output, excluding tentative or intermediate drafts.']}
