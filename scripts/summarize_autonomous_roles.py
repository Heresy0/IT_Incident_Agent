"""Offline, integrity-checked summaries of a frozen model-role experiment.

References describe developer-authored synthetic controls, not human gold.
Acquiring or citing a source does not establish a correct causal explanation.
This module never constructs a model or performs a network request.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from evals.closed_loop_cases import build_cases, acquired_key_observations, reference_targets
from scripts.evaluate_autonomous_closed_loop import score_review

ARMS = ('flash_all', 'investigator_plus', 'reviewer_plus')
STAGES = ('e2e', 'fixed_rework', 'review')
ROLES = ('investigation', 'diagnosis', 'reviewer', 'challenger')
FLASH, PLUS = 'qwen3.7-flash', 'qwen3.7-plus'


class ComparabilityError(ValueError):
    """An artifact cannot support this controlled comparison."""


def _require(condition, code):
    if not condition:
        raise ComparabilityError(code)


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def distribution(values):
    """Nearest-rank p95; unknown observations are excluded, never zero-filled."""
    known = sorted(value for value in values if isinstance(value, (int, float))
                   and not isinstance(value, bool) and math.isfinite(value))
    return {'known': len(known), 'unknown': len(values) - len(known),
            'sum': sum(known) if known else None,
            'mean': statistics.mean(known) if known else None,
            'median': statistics.median(known) if known else None,
            'p95': known[math.ceil(.95 * len(known)) - 1] if known else None,
            'p95_method': 'nearest_rank',
            'small_sample': len(known) < 20}


def expected_model_maps():
    result = {arm: {role: FLASH for role in ROLES} for arm in ARMS}
    result['investigator_plus']['investigation'] = PLUS
    result['reviewer_plus']['reviewer'] = PLUS
    return result


def _validate_sources(plan, source_root):
    source_root = Path(source_root).resolve()
    _require(bool(plan.get('app_source_sha256')), 'MISSING_APP_SOURCE_FREEZE')
    expected_app = {path.relative_to(source_root).as_posix()
                    for path in (source_root / 'app').rglob('*.py')}
    _require(set(plan['app_source_sha256']) == expected_app, 'APP_SOURCE_SET_CHANGED')
    _require(bool(plan.get('evaluation_source_sha256')), 'MISSING_EVALUATOR_SOURCE_FREEZE')
    for group in ('app_source_sha256', 'evaluation_source_sha256'):
        for relative, expected in plan[group].items():
            target = (source_root / relative).resolve()
            _require(target.is_relative_to(source_root), 'SOURCE_PATH_OUTSIDE_ROOT')
            _require(target.is_file() and _sha(target) == expected,
                     'SOURCE_HASH_MISMATCH:' + relative)


def load_bundle(path, *, source_root=ROOT):
    """Read complete runs and verify every input, configuration and file hash."""
    path = Path(path).resolve()
    bundle = _json(path)
    plan = bundle['plan']
    _require(plan.get('stage') in STAGES, 'UNKNOWN_STAGE')
    _require(plan.get('execution_mode') == 'live', 'LIVE_ARTIFACTS_REQUIRED')
    _require(tuple(plan.get('arms', [])) == ARMS, 'ROLE_ARM_SET_MISMATCH')
    _require(plan.get('model_maps') == expected_model_maps(), 'ROLE_MODEL_MAP_MISMATCH')
    _require(bool(plan.get('provider_configuration_sha256')), 'MISSING_PROVIDER_CONFIGURATION_HASH')
    _require(bool(plan.get('configuration_sha256')), 'MISSING_CONFIGURATION_HASH')
    _require(bool(plan.get('model_settings')), 'MISSING_SHARED_MODEL_SETTINGS')
    _require(plan.get('role_model_settings') == {role: plan['model_settings'] for role in ROLES},
             'ROLE_MODEL_SETTINGS_MISMATCH')
    _require(isinstance(plan.get('repeats'), int) and plan['repeats'] >= 1, 'INVALID_REPEATS')
    _validate_sources(plan, source_root)
    cases = build_cases()
    freezes = {row['case_id']: row for row in plan['case_freezes']}
    _require(len(freezes) == len(plan['cases']) and set(freezes) == set(plan['cases']),
             'CASE_FREEZE_SET_MISMATCH')
    for case_id in plan['cases']:
        _require(case_id in cases and cases[case_id].freeze() == freezes[case_id], 'LOCAL_CASE_WORLD_CHANGED')
    reports = {}
    for entry in bundle['runs']:
        key = (entry['case_id'], entry['arm'], entry['repeat'])
        _require(key not in reports, 'DUPLICATE_RUN')
        target = (path.parent / entry['report_file']).resolve()
        _require(target.parent == path.parent, 'REPORT_PATH_OUTSIDE_BUNDLE')
        _require(target.is_file() and _sha(target) == entry['report_sha256'], 'REPORT_HASH_MISMATCH')
        report = _json(target)
        _require((report.get('case_id'), report.get('arm'), report.get('repeat')) == key,
                 'REPORT_IDENTITY_MISMATCH')
        _require(report.get('execution_mode') == 'live', 'REPORT_EXECUTION_MODE_MISMATCH')
        _require(report.get('application') == plan['application'], 'REPORT_APPLICATION_MISMATCH')
        _require(report.get('run_summary', {}).get('limits') == plan['limits'], 'REPORT_LIMITS_MISMATCH')
        _require(report.get('model_map') == plan['model_maps'].get(key[1]), 'REPORT_MODEL_MAP_MISMATCH')
        _require(report.get('synthetic_reference') == freezes.get(key[0]), 'REPORT_WORLD_MISMATCH')
        _require(isinstance(report.get('model_call_log'), list), 'MISSING_MODEL_CALL_LOG')
        for call in report['model_call_log']:
            role = call.get('role')
            _require(role in ROLES and call.get('model') == report['model_map'][role],
                     'ACTUAL_ROLE_MODEL_MISMATCH')
            _require(bool(call.get('request_sha256')), 'MISSING_REQUEST_HASH')
            _require(call.get('error') or bool(call.get('output_sha256')), 'MISSING_OUTPUT_HASH')
            _require(isinstance(call.get('charged_request'), bool), 'MISSING_PAID_REQUEST_ADMISSION')
            usage = call.get('usage')
            _require(usage is None or (isinstance(usage, dict) and all(
                isinstance(usage.get(field), int) and not isinstance(usage[field], bool) and usage[field] >= 0
                for field in ('input_tokens', 'output_tokens'))), 'INVALID_USAGE')
        reports[key] = {'report': report, 'entry': entry, 'path': str(target)}
    expected = {(case, arm, repeat) for case in plan['cases'] for arm in ARMS
                for repeat in range(1, plan['repeats'] + 1)}
    _require(set(reports) == expected and len(reports) == plan.get('planned_runs'), 'INCOMPLETE_RUN_SET')
    return {'path': str(path), 'sha256': _sha(path), 'plan': plan, 'reports': reports}


def validate_comparability(bundles, *, comparison_plan=None):
    """Only the two declared role/model substitutions may vary between arms."""
    _require(bool(bundles), 'NO_BUNDLES')
    stages = [bundle['plan']['stage'] for bundle in bundles]
    _require(len(set(stages)) == len(stages), 'DUPLICATE_STAGE')
    reference = bundles[0]['plan']
    shared = ('version', 'application', 'app_source_sha256', 'evaluation_source_sha256',
              'limits', 'model_settings', 'role_model_settings', 'model_maps', 'provider_configuration_sha256',
              'configuration_sha256', 'dataset_version')
    for bundle in bundles:
        plan = bundle['plan']
        for field in shared:
            _require(plan.get(field) == reference.get(field), 'PLAN_MISMATCH:' + field)
        if comparison_plan:
            stage = comparison_plan['stages'][plan['stage']]
            expected_cases = list(build_cases()) if isinstance(stage['cases'], str) else stage['cases']
            _require(plan['cases'] == expected_cases and plan['repeats'] == stage['repeats'],
                     'PRESPECIFIED_RUN_SET_MISMATCH')
            for field, expected in comparison_plan.get('per_run_limits', {}).items():
                _require(plan['limits'].get(field) == expected, 'PRESPECIFIED_LIMIT_MISMATCH:' + field)
            if 'model_settings' in comparison_plan:
                _require(plan['model_settings'] == comparison_plan['model_settings'],
                         'PRESPECIFIED_MODEL_SETTINGS_MISMATCH')
            if 'model_maps' in comparison_plan:
                _require(plan['model_maps'] == comparison_plan['model_maps'], 'PRESPECIFIED_MODEL_MAP_MISMATCH')
            if 'application' in comparison_plan:
                _require(plan['application'] == comparison_plan['application'], 'PRESPECIFIED_APP_MISMATCH')
            for field in ('app_source_sha256', 'limits', 'provider_configuration_sha256'):
                if field in comparison_plan:
                    _require(plan[field] == comparison_plan[field], 'PRESPECIFIED_FIELD_MISMATCH:' + field)
            if 'case_freezes' in comparison_plan:
                _require(all(freeze == comparison_plan['case_freezes'].get(freeze['case_id'])
                             for freeze in plan['case_freezes']), 'PRESPECIFIED_WORLD_MISMATCH')
    return {'status': 'passed', 'varying_parameter': 'declared_single_role_model',
            'challenger_model': FLASH, 'diagnosis_model': FLASH,
            'source_integrity': 'all_application_evaluator_world_and_report_hashes_verified'}


def _review_controls(report, case, stage, phase):
    if stage == 'e2e':
        return []  # No language-output gold is manufactured from source IDs.
    reference = reference_targets(case)
    reference_field = 'provisional_reference' if stage == 'review' else 'first_pass_provisional_reference'
    _require(report.get(reference_field) == reference
             or (bool(report.get('execution_error')) and not report.get(reference_field)),
             'REVIEW_REFERENCE_MISMATCH')
    if stage == 'review':
        batch = report.get('primary_batch', {}) if phase == 'primary' else report.get('review_batch', {})
    else:
        first = report.get('first_pass', {})
        batch = first.get('primary_batch', report.get('primary_batch', {})) if phase == 'primary' else first.get('review', {})
    return score_review({'item_reviews': batch.get('items', []), 'provisional_reference': reference})


def _final_draft(report):
    if isinstance(report.get('final_draft'), dict):
        return report['final_draft']
    drafts = report.get('drafts', [])
    return drafts[-1].get('draft', {}) if drafts else {}


def source_utilization(case, report):
    """Count acquired key records explicitly cited by final model-authored claims."""
    keys = acquired_key_observations(case, report)
    evidence_ids = {row['evidence_id']: row.get('locator', '').rsplit('/', 1)[-1]
                    for row in report.get('evidence', [])}
    required = set(case.key_observation_ids)
    acquired = required & set(evidence_ids.values())
    claims = _final_draft(report).get('claims', [])
    supporting = {evidence_ids.get(ref.get('evidence_id')) for claim in claims for ref in claim.get('refs', [])}
    counter = {evidence_ids.get(ref.get('evidence_id')) for claim in claims for ref in claim.get('counter_refs', [])}
    mechanism = {evidence_ids.get(ref.get('evidence_id')) for claim in claims if claim.get('level') == 'mechanism'
                 for ref in claim.get('refs', [])}
    return {**keys, 'acquired_ids': sorted(acquired),
            'cited_by_final_claim_refs_ids': sorted(acquired & supporting),
            'cited_by_final_claim_counter_refs_ids': sorted(acquired & counter),
            'cited_by_final_mechanism_refs_ids': sorted(acquired & mechanism),
            'acquired_but_not_cited_by_final_claims_ids': sorted(acquired - supporting - counter),
            'citation_does_not_establish_causal_use_or_accuracy': True}


def run_metrics(report, case, stage):
    summary = report.get('run_summary', {})
    attempts = report['model_call_log']
    calls = [call for call in attempts if call['charged_request']]
    limits = summary['limits']
    violations = [field for field in ('model_calls', 'tool_calls', 'web_calls')
                  if isinstance(summary.get(field), (int, float)) and summary[field] > limits[field]]
    if summary.get('duration_ms', 0) > limits['seconds'] * 1000:
        violations.append('seconds')
    reworks = report.get('reworks', [])
    return {'completed': report.get('status') == 'completed', 'status': report.get('status'),
            'execution_error': bool(report.get('execution_error')),
            'unrecovered_protocol_failures': sum(not row.get('recovered') for row in report.get('protocol_failures', [])),
            'protocol_failure_history_count': len(report.get('protocol_failure_history', report.get('protocol_failures', []))),
            'hard_limit_violations': violations,
            'model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
            'duration_ms': summary.get('duration_ms'),
            'source_utilization': source_utilization(case, report) if stage != 'review' else None,
            'rework': {'performed_rounds': sum(bool(row.get('performed')) for row in reworks),
                       'stop_reasons': [row.get('reason', 'UNKNOWN') for row in reworks],
                       'changed_targets': sum(len(row.get('changed_ids', [])) for row in reworks),
                       'withdrawn_targets': sum(len(row.get('withdrawn_ids', [])) for row in reworks),
                       'text_revision_without_new_observation_rounds': sum(bool(row.get('performed'))
                           and not row.get('new_observation_ids')
                           and bool(row.get('changed_ids') or row.get('withdrawn_ids')) for row in reworks)},
            'primary_review_controls': _review_controls(report, case, stage, 'primary'),
            'final_review_controls': _review_controls(report, case, stage, 'final'),
            'actual_model_call_count': len(calls),
            'model_invoke_attempts': len(attempts),
            'unpaid_admission_rejections': len(attempts) - len(calls),
            'actual_plus_call_count': sum(row['model'] == PLUS for row in calls),
            'actual_calls_by_role_model': dict(Counter(row['role'] + ':' + row['model'] for row in calls)),
            'usage_known_calls': sum(row.get('usage') is not None for row in calls),
            'usage_unknown_calls': sum(row.get('usage') is None for row in calls),
            'actual_input_tokens_known': sum(row['usage']['input_tokens'] for row in calls if row.get('usage') is not None),
            'actual_output_tokens_known': sum(row['usage']['output_tokens'] for row in calls if row.get('usage') is not None),
            'input_tokens': sum(row['usage']['input_tokens'] for row in calls) if all(row.get('usage') is not None for row in calls) else None,
            'output_tokens': sum(row['usage']['output_tokens'] for row in calls) if all(row.get('usage') is not None for row in calls) else None,
            'model_seconds': sum(row['seconds'] for row in calls) if all(isinstance(row.get('seconds'), (int, float)) for row in calls) else None}


def review_summary(rows, field):
    controls = [item for row in rows for item in row['metrics'][field]]
    assessed = [item for item in controls if item['protocol_status'] == 'valid']
    positives = [item for item in assessed if item['expected'] in {'supported', 'appropriate'}]
    negatives = [item for item in assessed if item['expected'] not in {'supported', 'appropriate'}]
    groups = defaultdict(list)
    for row in rows:
        for item in row['metrics'][field]:
            groups[(row['arm'], row['case_id'], item['target_id'])].append(item)
    repeated = [items for items in groups.values() if len(items) > 1]
    relation_groups = [items for items in repeated if all(item['protocol_status'] == 'valid' for item in items)]
    flips = sum(len({item['observed'] for item in items}) > 1 for items in relation_groups)
    status_flips = sum(len({(item['protocol_status'], item['observed']) for item in items}) > 1 for items in repeated)
    return {'reference_kind': 'synthetic_source_constructed_provisional', 'human_verified': False,
            'reference_items': len(controls), 'assessed_items': len(assessed),
            'unassessed_items': len(controls) - len(assessed),
            'positive_reference_items_assessed': len(positives), 'negative_reference_items_assessed': len(negatives),
            'false_accepts': sum(item['false_accept'] for item in negatives),
            'false_accept_rate': sum(item['false_accept'] for item in negatives) / len(negatives) if negatives else None,
            'false_rejects': sum(item['false_reject'] for item in positives),
            'false_reject_rate': sum(item['false_reject'] for item in positives) / len(positives) if positives else None,
            'exact_relation_agreement': sum(item['exact_agreement'] for item in assessed),
            'exact_relation_agreement_rate': sum(item['exact_agreement'] for item in assessed) / len(assessed) if assessed else None,
            'repeated_groups': len(repeated), 'fully_assessed_repeated_groups': len(relation_groups),
            'groups_with_relation_flips': flips,
            'relation_flip_rate': flips / len(relation_groups) if relation_groups else None,
            'groups_with_relation_or_protocol_changes': status_flips,
            'confusion_counts': dict(Counter(item['expected'] + '->' + str(item['observed']) for item in assessed)),
            'revised_claim_accuracy': None}


def estimate_cost(calls, rate_card=None, *, pricing_scope=None):
    """Return only a matching-scope token price estimate, never a billing claim."""
    result = {'currency': 'CNY', 'estimated_total': None, 'known_usage_estimated_subtotal': None,
              'unknown_usage_calls': sum(call.get('usage') is None for call in calls),
              'unpriced_calls': len(calls), 'is_actual_vendor_bill': False}
    if not rate_card:
        return {**result, 'status': 'not_estimated', 'reason': 'NO_RATE_CARD'}
    if not pricing_scope or rate_card.get('pricing_scope') != pricing_scope:
        return {**result, 'status': 'not_estimated', 'reason': 'PUBLIC_PRICE_SCOPE_NOT_CONFIRMED_OR_MISMATCHED'}
    if rate_card.get('currency') != 'CNY' or not rate_card.get('effective_date') or not str(rate_card.get('source_url', '')).startswith(('https://', 'http://')):
        return {**result, 'status': 'not_estimated', 'reason': 'INCOMPLETE_PUBLIC_PRICE_PROVENANCE'}
    subtotal, priced, unknown, over_tier = 0.0, 0, 0, 0
    for call in calls:
        rate = rate_card.get('models', {}).get(call['model'])
        if not rate or not all(isinstance(rate.get(field), (int, float)) and rate[field] >= 0
                               for field in ('input_per_million', 'output_per_million')):
            continue
        if call.get('usage') is None:
            unknown += 1
            continue
        if not isinstance(rate.get('max_input_tokens'), int) or call['usage']['input_tokens'] > rate['max_input_tokens']:
            over_tier += 1
            continue
        subtotal += (call['usage']['input_tokens'] * rate['input_per_million']
                     + call['usage']['output_tokens'] * rate['output_per_million']) / 1_000_000
        priced += 1
    complete = priced == len(calls)
    return {**result, 'status': 'estimated' if complete else 'partial_estimate',
            'estimated_total': subtotal if complete else None,
            'known_usage_estimated_subtotal': subtotal if priced else None,
            'priced_calls': priced, 'unpriced_calls': len(calls) - priced,
            'input_tier_unmatched_calls': over_tier,
            'reason': None if complete else 'UNKNOWN_USAGE_OR_UNPRICED_MODEL',
            'source_url': rate_card['source_url'], 'effective_date': rate_card['effective_date'],
            'pricing_scope': pricing_scope,
            'estimate_kind': 'published_non_cached_list_price_not_actual_bill',
            'cache_discounts_free_quota_and_other_discounts': 'not_applied'}


def arm_summary(rows, rate_card=None, *, pricing_scope=None):
    metrics = [row['metrics'] for row in rows]
    sources = [row['source_utilization'] for row in metrics if row['source_utilization'] is not None]
    calls = [call for row in rows for call in row['report']['model_call_log'] if call['charged_request']]
    plus_runs = sum(row['actual_plus_call_count'] > 0 for row in metrics)
    def usage_for(group):
        known = [call for call in group if call.get('usage') is not None]
        return {'calls': len(group), 'usage_known_calls': len(known),
                'usage_unknown_calls': len(group) - len(known),
                'actual_input_tokens_known': sum(call['usage']['input_tokens'] for call in known),
                'actual_output_tokens_known': sum(call['usage']['output_tokens'] for call in known),
                'input_tokens': sum(call['usage']['input_tokens'] for call in known) if len(known) == len(group) else None,
                'output_tokens': sum(call['usage']['output_tokens'] for call in known) if len(known) == len(group) else None,
                'model_call_latency_seconds': distribution([call.get('seconds') for call in group]),
                'cost': estimate_cost(group, rate_card, pricing_scope=pricing_scope)}
    role_groups = defaultdict(list)
    model_groups = defaultdict(list)
    for call in calls:
        role_groups[call['role'] + ':' + call['model']].append(call)
        model_groups[call['model']].append(call)
    return {'runs': len(rows), 'completed': sum(row['completed'] for row in metrics),
            'execution_error_runs': sum(row['execution_error'] for row in metrics),
            'unrecovered_protocol_failure_runs': sum(row['unrecovered_protocol_failures'] > 0 for row in metrics),
            'hard_limit_violation_runs': sum(bool(row['hard_limit_violations']) for row in metrics),
            'key_observations_required': sum(row['required_count'] for row in sources),
            'key_observations_acquired': sum(row['acquired_count'] for row in sources),
            'key_observations_cited_by_final_claim_refs': sum(len(row['cited_by_final_claim_refs_ids']) for row in sources),
            'key_observations_cited_by_final_claim_counter_refs': sum(len(row['cited_by_final_claim_counter_refs_ids']) for row in sources),
            'key_observations_cited_by_final_mechanism_refs': sum(len(row['cited_by_final_mechanism_refs_ids']) for row in sources),
            'key_observations_acquired_but_not_cited_by_final_claims': sum(len(row['acquired_but_not_cited_by_final_claims_ids']) for row in sources),
            'actual_plus_calls': sum(row['actual_plus_call_count'] for row in metrics),
            'runs_with_actual_plus_calls': plus_runs,
            'runs_without_actual_plus_calls': len(rows) - plus_runs,
            'plus_benefit_attribution_supported_by_exposure': plus_runs > 0,
            'exposure_is_not_proof_of_model_benefit': True,
            'usage_known_calls': sum(row['usage_known_calls'] for row in metrics),
            'usage_unknown_calls': sum(row['usage_unknown_calls'] for row in metrics),
            'actual_input_tokens_known': sum(row['actual_input_tokens_known'] for row in metrics),
            'actual_output_tokens_known': sum(row['actual_output_tokens_known'] for row in metrics),
            'actual_calls_by_role_model': dict(Counter(call['role'] + ':' + call['model'] for call in calls)),
            'actual_usage_by_role_model': {key: usage_for(group) for key, group in sorted(role_groups.items())},
            'actual_usage_by_model': {key: usage_for(group) for key, group in sorted(model_groups.items())},
            'rework': {'performed_runs': sum(row['rework']['performed_rounds'] > 0 for row in metrics),
                       'performed_rounds': sum(row['rework']['performed_rounds'] for row in metrics),
                       'stop_reason_counts': dict(Counter(reason for row in metrics for reason in row['rework']['stop_reasons'])),
                       'changed_targets': sum(row['rework']['changed_targets'] for row in metrics),
                       'withdrawn_targets': sum(row['rework']['withdrawn_targets'] for row in metrics),
                       'text_revision_without_new_observation_rounds': sum(row['rework']['text_revision_without_new_observation_rounds'] for row in metrics)},
            'resources_per_run': {field: distribution([row[field] for row in metrics]) for field in
                ('model_calls', 'tool_calls', 'input_tokens', 'output_tokens', 'duration_ms', 'model_seconds')},
            'model_call_latency_seconds': distribution([call.get('seconds') for call in calls]),
            'cost': estimate_cost(calls, rate_card, pricing_scope=pricing_scope),
            'estimated_cost_CNY_per_run': distribution([estimate_cost(
                [call for call in row['report']['model_call_log'] if call['charged_request']], rate_card,
                pricing_scope=pricing_scope)['estimated_total'] for row in rows]),
            'primary_review': review_summary(rows, 'primary_review_controls'),
            'final_review': review_summary(rows, 'final_review_controls'),
            'causal_accuracy': None, 'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'}


def summarize_stage(bundle, rate_card=None, *, pricing_scope=None):
    stage, cases = bundle['plan']['stage'], build_cases()
    rows = []
    for key, value in sorted(bundle['reports'].items()):
        report = value['report']
        rows.append({'case_id': key[0], 'arm': key[1], 'repeat': key[2],
                     'report_path': value['path'], 'report_sha256': value['entry']['report_sha256'],
                     'metrics': run_metrics(report, cases[key[0]], stage), 'report': report})
    arms = {arm: arm_summary([row for row in rows if row['arm'] == arm], rate_card, pricing_scope=pricing_scope)
            for arm in ARMS}
    paired = []
    for case in bundle['plan']['cases']:
        for repeat in range(1, bundle['plan']['repeats'] + 1):
            group = {row['arm']: row for row in rows if row['case_id'] == case and row['repeat'] == repeat}
            baseline = group['flash_all']['metrics']
            for arm in ARMS[1:]:
                alternative = group[arm]['metrics']
                paired.append({'case_id': case, 'repeat': repeat, 'arm': arm,
                    'has_actual_plus_calls': alternative['actual_plus_call_count'] > 0,
                    'deltas_from_flash_all': {field: alternative[field] - baseline[field]
                        if isinstance(alternative.get(field), (int, float)) and isinstance(baseline.get(field), (int, float)) else None
                        for field in ('model_calls', 'tool_calls', 'duration_ms', 'input_tokens', 'output_tokens')},
                    'completed_delta': int(alternative['completed']) - int(baseline['completed']),
                    'key_acquisition_delta': alternative['source_utilization']['acquired_count'] - baseline['source_utilization']['acquired_count']
                        if alternative['source_utilization'] is not None else None})
    return {'stage': stage, 'bundle_path': bundle['path'], 'bundle_sha256': bundle['sha256'],
            'arms': arms, 'paired_deltas': paired,
            'cases': {case: {arm: arm_summary([row for row in rows if row['case_id'] == case and row['arm'] == arm],
                rate_card, pricing_scope=pricing_scope) for arm in ARMS} for case in bundle['plan']['cases']},
            'runs': [{key: value for key, value in row.items() if key != 'report'} for row in rows],
            'fixed_target_scope': 'review stage: fixed original targets; fixed_rework: first-pass original targets only; e2e has no semantic gold'}


def summarize(paths, *, source_root=ROOT, comparison_plan=None, rate_card=None):
    bundles = [load_bundle(path, source_root=source_root) for path in paths]
    comparability = validate_comparability(bundles, comparison_plan=comparison_plan)
    scope = comparison_plan.get('pricing_scope') if comparison_plan else None
    return {'version': 'autonomous-role-summary-v1', 'comparability': comparability,
            'application': bundles[0]['plan']['application'],
            'model_maps': expected_model_maps(),
            'complete_prespecified_stages': set(bundle['plan']['stage'] for bundle in bundles)
                == set(comparison_plan['stages'] if comparison_plan else STAGES),
            'stages': {bundle['plan']['stage']: summarize_stage(bundle, rate_card, pricing_scope=scope) for bundle in bundles},
            'limitations': ['Synthetic controls are developer authored and are not human gold or external blind tests.',
                'Key-record acquisition and source citation are not proof of correct causal explanation.',
                'Revised claims and autonomous e2e output have no manufactured semantic accuracy labels.',
                'Small repeated samples and nearest-rank p95 do not establish population performance.',
                'Shared Flash challenger and diagnosis roles remain fixed in every arm.',
                'Observed arm differences without actual Plus calls cannot be attributed to Plus.'],
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', action='append', required=True, type=Path)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--comparison-plan', type=Path)
    parser.add_argument('--rate-card', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = summarize(args.bundle, source_root=args.source_root,
                           comparison_plan=_json(args.comparison_plan) if args.comparison_plan else None,
                           rate_card=_json(args.rate_card) if args.rate_card else None)
    except ComparabilityError as exc:
        print(json.dumps({'comparability': 'failed', 'reason': str(exc)}, ensure_ascii=False))
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()), 'stages': list(result['stages']),
                      'comparability': 'passed', 'rollout_recommendation': 'hold'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
