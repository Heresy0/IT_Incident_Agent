"""Offline, source-verified 2 x 2 budget experiment analysis.

Only the model-call limit and review-rework new-read limit may vary. Native
delivery outcomes, source acquisition and semantic correctness stay separate.
No application module or model client is imported by this module.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.summarize_frozen_versions import (
    ComparabilityError, _json, _require, _sha, _text_sha,
    _verify_hashes, _write_json, build_blind_packets as _build_blind_packets, classify_failures,
    distribution, estimate_cost, source_metrics,
)

ARMS = ('m12_r2', 'm12_r4', 'm16_r2', 'm16_r4')
STAGES = ('e2e', 'fixed_rework')
FACTORS = {'m12_r2': (12, 2), 'm12_r4': (12, 4),
           'm16_r2': (16, 2), 'm16_r4': (16, 4)}


def _validate_arm_configs(plan):
    _require(tuple(plan.get('arms', [])) == ARMS, 'BUDGET_ARM_SET_MISMATCH')
    configs = plan.get('arm_configs', {})
    _require(set(configs) == set(ARMS), 'BUDGET_ARM_CONFIG_SET_MISMATCH')
    base = configs[ARMS[0]]['limits']
    for arm, (models, reads) in FACTORS.items():
        config = configs[arm]
        _require(set(config) == {'limits', 'max_new_reads'}, 'UNDECLARED_ARM_CONFIGURATION')
        _require(config.get('max_new_reads') == reads, 'LOCAL_READ_LIMIT_MISMATCH')
        limits = config.get('limits', {})
        _require(limits.get('model_calls') == models, 'MODEL_CALL_LIMIT_MISMATCH')
        _require({key: value for key, value in limits.items() if key != 'model_calls'}
                 == {key: value for key, value in base.items() if key != 'model_calls'},
                 'UNDECLARED_BUDGET_DIFFERENCE')
    _require(base.get('tool_calls') == 8 and base.get('seconds') == 180,
             'SHARED_TOOL_TIME_BUDGET_MISMATCH')


def load_bundle(path, *, evaluator_root=ROOT, allow_free_smoke=False):
    """Require all four arms and every repetition; never drop failed runs."""
    path = Path(path).resolve()
    bundle = _json(path)
    plan = bundle['plan']
    execution_mode = plan.get('execution_mode')
    _require(execution_mode == 'live' or allow_free_smoke and execution_mode == 'free_smoke', 'LIVE_ARTIFACTS_REQUIRED')
    _require(plan.get('stage') in STAGES, 'UNKNOWN_STAGE')
    _validate_arm_configs(plan)
    _require(plan.get('model') == 'qwen3.7-flash' and bool(plan.get('model_settings')),
             'FIXED_FLASH_SETTINGS_REQUIRED')
    _require(bool(plan.get('provider_configuration_sha256')), 'PROVIDER_CONFIGURATION_MISSING')
    _require(isinstance(plan.get('repeats'), int) and not isinstance(plan['repeats'], bool)
             and plan['repeats'] >= 1, 'INVALID_REPEATS')
    _verify_hashes(evaluator_root, plan.get('evaluation_source_sha256', {}))
    application = plan['application']
    _require(bool(application.get('source_version')), 'APPLICATION_VERSION_MISSING')
    root, app = Path(application['source_root']).resolve(), Path(application['app_root']).resolve()
    _require(app == root / 'app', 'APPLICATION_ROOT_MISMATCH')
    _verify_hashes(root, application.get('app_source_sha256', {}), complete_app=True)
    freezes = {row['case_id']: row for row in plan['world_freezes']}
    _require(len(freezes) == len(plan['world_freezes']) == len(plan['cases'])
             and set(freezes) == set(plan['cases']), 'WORLD_FREEZE_SET_MISMATCH')
    reports = {}
    for entry in bundle['runs']:
        key = (entry['case_id'], entry['arm'], entry['repeat'])
        _require(key not in reports, 'DUPLICATE_RUN')
        _require(key[1] in ARMS and key[0] in freezes, 'UNDECLARED_RUN')
        target = (path.parent / entry['report_file']).resolve()
        _require(target.parent == path.parent, 'REPORT_PATH_OUTSIDE_BUNDLE')
        _require(target.is_file() and _sha(target) == entry['report_sha256'], 'REPORT_HASH_MISMATCH')
        report = _json(target)
        _require((report.get('case_id'), report.get('arm'), report.get('repeat')) == key,
                 'REPORT_IDENTITY_MISMATCH')
        _require(report.get('stage') == plan['stage'], 'REPORT_STAGE_MISMATCH')
        _require(report.get('execution_mode') == execution_mode, 'REPORT_EXECUTION_MODE_MISMATCH')
        _require(report.get('application') == application, 'REPORT_APPLICATION_MISMATCH')
        _require(report.get('world_freeze') == freezes[key[0]], 'REPORT_WORLD_MISMATCH')
        _require(report.get('observed_world_sha256') == freezes[key[0]].get('world_sha256')
                 and report.get('observed_capability_sha256') == freezes[key[0]].get('metadata_sha256'),
                 'WORKER_OBSERVED_WORLD_MISMATCH')
        _require(report.get('arm_limits') == plan['arm_configs'][key[1]]['limits']
                 == report.get('run_summary', {}).get('limits'), 'REPORT_LIMITS_MISMATCH')
        adapter = report.get('budget_adapter_audit', {})
        _require(adapter.get('policy') == plan['adapter_policy'] and adapter.get('arm') == key[1]
            and adapter.get('max_new_reads') == plan['arm_configs'][key[1]]['max_new_reads'],
            'BUDGET_ADAPTER_MISMATCH')
        _require(isinstance(adapter.get('calls'), list) and all(row.get('phase') == 'review_rework'
            and row.get('native_max_new_reads') == 2
            and row.get('effective_max_new_reads') == adapter['max_new_reads'] for row in adapter['calls']),
            'UNDECLARED_ADAPTER_OVERRIDE')
        if 'model_settings' in report:
            _require(report['model_settings'] == plan['model_settings'], 'REPORT_SETTINGS_MISMATCH')
        if 'provider_configuration_sha256' in report:
            _require(report['provider_configuration_sha256'] == plan['provider_configuration_sha256'],
                     'REPORT_PROVIDER_MISMATCH')
        loaded = report.get('loaded_application_modules')
        _require(isinstance(loaded, dict) and bool(loaded), 'WORKER_IMPORT_PROVENANCE_MISSING')
        for filename in loaded.values():
            module_path = Path(filename).resolve()
            _require(module_path.is_relative_to(app), 'WORKER_APPLICATION_IMPORT_ESCAPED')
            relative = module_path.relative_to(root).as_posix()
            _require(module_path.is_file() and application['app_source_sha256'].get(relative) == _sha(module_path),
                     'WORKER_MODULE_HASH_MISMATCH')
        normalized = report.get('normalized', {})
        for field in ('observations', 'queries', 'final_claims', 'audit_items', 'failures'):
            _require(isinstance(normalized.get(field), list), 'NORMALIZED_REPORT_MISSING:' + field)
        _require(isinstance(report.get('decision_trace'), list), 'DECISION_TRACE_MISSING')
        calls = report.get('model_call_log')
        _require(isinstance(calls, list), 'MODEL_CALL_LOG_MISSING')
        for call in calls:
            _require(call.get('model') == plan['model'], 'ACTUAL_MODEL_MISMATCH')
            if execution_mode == 'live' and call.get('response_model_name'):
                _require(call['response_model_name'] == plan['model'], 'RESPONSE_MODEL_MISMATCH')
            _require(isinstance(call.get('charged_request'), bool), 'PAID_ADMISSION_MISSING')
            _require(bool(call.get('request_sha256')) and (call.get('error') or bool(call.get('output_sha256'))),
                     'MODEL_REQUEST_OUTPUT_HASH_MISSING')
            usage = call.get('usage')
            _require(usage is None or (isinstance(usage, dict) and all(
                isinstance(usage.get(field), int) and not isinstance(usage[field], bool) and usage[field] >= 0
                for field in ('input_tokens', 'output_tokens'))), 'INVALID_USAGE')
        reports[key] = {'report': report, 'entry': entry, 'path': str(target)}
    expected = {(case, arm, repeat) for case in plan['cases'] for arm in ARMS
                for repeat in range(1, plan['repeats'] + 1)}
    _require(set(reports) == expected and len(reports) == plan.get('planned_runs'),
             'INCOMPLETE_FOUR_ARM_RUN_SET')
    schedule = plan.get('schedule', [])
    if schedule:
        planned = [(row['case_id'], row['arm'], row['repeat']) for row in schedule]
        _require(len(planned) == len(expected) and set(planned) == expected, 'SCHEDULE_SET_MISMATCH')
        _require([(row['case_id'], row['arm'], row['repeat']) for row in bundle['runs']] == planned,
                 'EXECUTION_SCHEDULE_MISMATCH')
    return {'path': str(path), 'sha256': _sha(path), 'plan': plan, 'reports': reports}


def validate_comparability(bundles, comparison_plan=None, *, require_all_stages=True):
    _require(bool(bundles), 'NO_BUNDLES')
    stages = [bundle['plan']['stage'] for bundle in bundles]
    _require(len(set(stages)) == len(stages), 'DUPLICATE_STAGE')
    first = bundles[0]['plan']
    shared = ('version', 'arms', 'arm_configs', 'application', 'model', 'model_settings',
              'provider_configuration_sha256', 'evaluation_source_sha256', 'adapter_policy', 'jobs', 'execution_mode',
              'accounting_policy', 'concurrency_policy')
    for bundle in bundles:
        plan = bundle['plan']
        for field in shared:
            _require(plan.get(field) == first.get(field), 'PLAN_MISMATCH:' + field)
        if comparison_plan:
            _require(plan['stage'] in comparison_plan['stages'], 'UNDECLARED_STAGE')
            stage = comparison_plan['stages'][plan['stage']]
            _require(plan['cases'] == stage['cases'] and plan['repeats'] == stage['repeats'],
                     'PRESPECIFIED_RUN_SET_MISMATCH')
            for field in shared:
                if field in comparison_plan:
                    _require(plan.get(field) == comparison_plan[field], 'PRESPECIFIED_FIELD_MISMATCH:' + field)
            if 'world_freezes' in comparison_plan:
                world_map = comparison_plan['world_freezes']
                if isinstance(world_map, list):
                    world_map = {row['case_id']: row for row in world_map}
                _require(all(world_map.get(row['case_id']) == row for row in plan['world_freezes']),
                         'PRESPECIFIED_WORLD_MISMATCH')
            if 'review_targets' in stage or 'review_targets' in comparison_plan:
                targets = stage.get('review_targets', comparison_plan.get('review_targets', {}))
                _require(plan.get('review_targets') == {case: targets.get(case, [])
                    if plan['stage'] == 'fixed_rework' else [] for case in plan['cases']},
                         'PRESPECIFIED_REVIEW_TARGET_MISMATCH')
    expected_stages = set(comparison_plan['stages'] if comparison_plan else STAGES)
    if require_all_stages:
        _require(set(stages) == expected_stages, 'INCOMPLETE_PRESPECIFIED_STAGE_SET')
    return {'status': 'passed', 'varying_parameters': ['model_call_limit', 'review_rework_max_new_reads'],
            'fixed_model': first['model'], 'complete_stages': set(stages) == expected_stages,
            'all_case_repeat_four_arm_blocks_complete': True,
            'source_integrity': 'application_evaluator_world_module_origin_and_report_hashes_verified',
            'execution_jobs': first.get('jobs'), 'inference': 'descriptive_small_sample_no_population_claim'}


def decision_metrics(report):
    events = report.get('native_report', {}).get('events', [])
    repeated = [row for row in events if row.get('type') == 'query_reused'
                and row.get('role') == 'investigation']
    trace = report['decision_trace']
    # One successful or malformed collection response is one decision attempt.
    # Revision is an investigation-role model call but is not a query decision.
    decisions = [row for row in trace if row.get('stage') in {'investigate', 'collect', 'collection', 'query'}
                 or row.get('is_collection_decision') is True]
    count = len(decisions)
    _require(len(repeated) <= count, 'REPEATED_QUERY_DENOMINATOR_INVALID')
    result = {'investigation_decision_attempts': count,
              'repeated_or_covered_query_proposals': len(repeated),
              'repeated_or_covered_fraction': len(repeated) / count if count else None,
              'repeated_proposal_reasons': dict(Counter(row.get('reason', 'unknown') for row in repeated)),
              'denominator_definition': 'all_collection_decision_attempts_including_finish_and_invalid_response',
              'decision_attempts_by_phase': dict(Counter(row.get('phase', 'unknown') for row in decisions)),
              'collection_stop_reasons': dict(Counter(row.get('reason', 'unknown') for row in events
                  if row.get('type') == 'collection_completed')),
              'collection_phases': dict(Counter(row.get('phase', 'unknown') for row in events
                  if row.get('type') == 'collection_completed'))}
    return result


def original_controls(report, plan, *, phase='first_pass'):
    """Score only unchanged original prefix items, never rewritten outputs."""
    if plan['stage'] != 'fixed_rework':
        return []
    targets = plan.get('review_targets', {}).get(report['case_id'], [])
    _require(bool(targets), 'FIXED_REVIEW_TARGETS_MISSING')
    field = 'review_controls_primary' if phase == 'primary' else 'review_controls_first_pass'
    observed = report['normalized'].get(field, [])
    indexed = {row['target_id']: row for row in observed}
    _require(len(indexed) == len(observed), 'DUPLICATE_REVIEW_CONTROL')
    _require(set(indexed) <= {row['target_id'] for row in targets}, 'UNKNOWN_REVIEW_CONTROL')
    result = []
    for target in targets:
        row = indexed.get(target['target_id'], {})
        if row:
            _require(row.get('original_text_sha256') == target['original_text_sha256'], 'FIXED_TARGET_TEXT_CHANGED')
            _require(row.get('kind') == target['kind'], 'FIXED_TARGET_KIND_CHANGED')
        verdict = row.get('native_verdict')
        assessed = row.get('protocol_status') == 'valid' and verdict is not None
        accept = row.get('accept')
        _require(accept is None or isinstance(accept, bool), 'INVALID_BINARY_ACCEPT')
        assessed = assessed and accept is not None
        reference = target.get('reference_relation', target.get('expected_decision'))
        expected_accept = target.get('expected_accept')
        _require(isinstance(expected_accept, bool), 'INVALID_EXPECTED_ACCEPT')
        result.append({'target_id': target['target_id'], 'kind': target['kind'], 'phase': phase,
            'original_text_sha256': target['original_text_sha256'], 'expected_accept': expected_accept,
            'expected_relation_or_decision': reference, 'native_verdict': verdict, 'assessed': assessed,
            'accept': accept if assessed else None,
            'false_accept': assessed and not expected_accept and accept,
            'false_reject_or_withheld': assessed and expected_accept and not accept,
            'relation_mismatch': assessed and reference is not None and verdict != reference,
            'missing_support_misclassified_as_contradiction': assessed and verdict == 'contradicted'
                and reference in {'insufficient', 'partial', 'partially_supported'},
            'reference_kind': 'source_constructed_provisional', 'human_verified': False, 'is_gold': False})
    return result


def run_metrics(report, plan, world, rate_card=None, *, pricing_scope=None):
    summary = report['run_summary']
    calls = [row for row in report['model_call_log'] if row['charged_request']]
    known = [row for row in calls if row.get('usage') is not None]
    source = source_metrics(report, world)
    all_ids = {row['observation_id'] for row in report['normalized']['observations']
               if row.get('kind') == 'observation'}
    initial = set(report.get('initial_observation_ids', []))
    if plan['stage'] == 'fixed_rework':
        _require('initial_observation_ids' in report, 'FIXED_PREFIX_OBSERVATIONS_MISSING')
    new_ids = all_ids - initial
    stage_acquisition = world.get('stage_acquisition', {}).get(plan['stage'], {})
    expected_new = set(stage_acquisition.get('expected_new_key_observation_ids',
        world.get('expected_new_key_observation_ids', world.get('key_observation_ids', [])))) - initial
    eligible_new = stage_acquisition.get('eligible_for_positive_new_acquisition_rate',
        world.get('eligible_for_positive_new_acquisition_rate', bool(expected_new))) and bool(expected_new)
    cost = estimate_cost(calls, rate_card, pricing_scope=pricing_scope)
    limits = plan['arm_configs'][report['arm']]['limits']
    violations = [field for field in ('model_calls', 'tool_calls', 'web_calls')
        if isinstance(summary.get(field), (int, float)) and summary[field] > limits[field]]
    if len(calls) > limits['model_calls'] and 'model_calls' not in violations:
        violations.append('model_calls')
    if isinstance(summary.get('duration_ms'), (int, float)) and summary['duration_ms'] > limits['seconds'] * 1000:
        violations.append('seconds')
    return {'native_status': report.get('status', report.get('native_report', {}).get('status')),
            'native_status_is_not_accuracy': True,
            'execution_error': bool(report.get('execution_error') or report.get('native_report', {}).get('execution_error')),
            'actual_charged_calls': len(calls), 'unpaid_invoke_attempts': len(report['model_call_log']) - len(calls),
            'model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
            'native_duration_ms': summary.get('duration_ms'), 'worker_wall_seconds': report.get('worker_wall_seconds'),
            'model_seconds': sum(row['seconds'] for row in calls) if all(isinstance(row.get('seconds'), (int, float)) for row in calls) else None,
            'known_usage_calls': len(known), 'unknown_usage_calls': len(calls) - len(known),
            'actual_input_tokens_known': sum(row['usage']['input_tokens'] for row in known),
            'actual_output_tokens_known': sum(row['usage']['output_tokens'] for row in known),
            'input_tokens': sum(row['usage']['input_tokens'] for row in known) if len(known) == len(calls) else None,
            'output_tokens': sum(row['usage']['output_tokens'] for row in known) if len(known) == len(calls) else None,
            'estimated_cost_CNY': cost['estimated_total'], 'cost': cost,
            'hard_limit_violations': violations,
            'termination_reason': summary.get('termination_reason'),
            'source': source, 'initial_observation_ids': sorted(initial),
            'new_independent_observation_ids': sorted(new_ids), 'new_independent_observations': len(new_ids),
            'new_key_source': {'eligible': bool(eligible_new), 'required_ids': sorted(expected_new),
                'acquired_ids': sorted(expected_new & new_ids), 'required_count': len(expected_new),
                'acquired_count': len(expected_new & new_ids),
                'all_required_acquired': expected_new <= new_ids if eligible_new else None},
            'decisions': decision_metrics(report), 'failures': classify_failures(report),
            'original_controls_primary': original_controls(report, plan, phase='primary'),
            'original_controls_first_pass': original_controls(report, plan)}


def control_summary(controls):
    assessed = [row for row in controls if row['assessed']]
    positives = [row for row in assessed if row['expected_accept']]
    negatives = [row for row in assessed if not row['expected_accept']]
    return {'reference_items': len(controls), 'assessed_items': len(assessed),
            'unassessed_items': len(controls) - len(assessed),
            'positive_reference_items_assessed': len(positives), 'negative_reference_items_assessed': len(negatives),
            'false_accepts': sum(bool(row['false_accept']) for row in assessed),
            'false_rejects_or_withheld': sum(bool(row['false_reject_or_withheld']) for row in assessed),
            'relation_mismatches': sum(bool(row['relation_mismatch']) for row in assessed),
            'missing_support_misclassified_as_contradiction': sum(bool(row['missing_support_misclassified_as_contradiction']) for row in assessed),
            'native_verdict_counts': dict(Counter(str(row['native_verdict']) for row in assessed)),
            'human_verified': False, 'is_gold': False, 'rewritten_items_scored_using_original_labels': False}


def _sum_counters(rows):
    counter = Counter()
    for row in rows:
        counter.update(row)
    return dict(counter)


def arm_summary(rows, rate_card=None, *, pricing_scope=None):
    metrics = [row['metrics'] for row in rows]
    calls = [call for row in rows for call in row['report']['model_call_log'] if call['charged_request']]
    positive = [row['source'] for row in metrics if row['source']['eligible_for_positive_acquisition_rate']]
    coverage = [row['source'] for row in metrics if row['source']['eligible_for_observation_coverage_rate']]
    new_positive = [row['new_key_source'] for row in metrics if row['new_key_source']['eligible']]
    decisions = sum(row['decisions']['investigation_decision_attempts'] for row in metrics)
    repeated = sum(row['decisions']['repeated_or_covered_query_proposals'] for row in metrics)
    return {'runs': len(rows), 'native_status_counts': dict(Counter(str(row['native_status']) for row in metrics)),
            'native_status_is_not_accuracy': True,
            'execution_error_runs': sum(row['execution_error'] for row in metrics),
            'hard_limit_violation_runs': sum(bool(row['hard_limit_violations']) for row in metrics),
            'terminal_structured_interruption_runs': sum(row['failures']['terminal_structured_interruption'] for row in metrics),
            'positive_acquisition_eligible_runs': len(positive),
            'key_records_required': sum(row['required_count'] for row in positive),
            'key_records_acquired': sum(row['acquired_count'] for row in positive),
            'all_required_key_records_acquired_runs': sum(row['all_required_acquired'] is True for row in positive),
            'coverage_records_required': sum(row['required_count'] for row in coverage),
            'coverage_records_acquired': sum(row['acquired_count'] for row in coverage),
            'new_key_acquisition_eligible_runs': len(new_positive),
            'new_key_records_required': sum(row['required_count'] for row in new_positive),
            'new_key_records_acquired': sum(row['acquired_count'] for row in new_positive),
            'all_required_new_key_records_acquired_runs': sum(row['all_required_acquired'] is True for row in new_positive),
            'new_independent_observations': sum(row['new_independent_observations'] for row in metrics),
            'key_records_cited_by_final_claim_refs': sum(len(row['source']['cited_by_final_claim_refs_ids']) for row in metrics),
            'key_records_cited_by_final_claim_counter_refs': sum(len(row['source']['cited_by_final_claim_counter_refs_ids']) for row in metrics),
            'citation_is_not_semantic_mechanism_verification': True,
            'investigation_decision_attempts': decisions, 'repeated_or_covered_query_proposals': repeated,
            'repeated_or_covered_fraction': repeated / decisions if decisions else None,
            'repeated_proposal_reasons': _sum_counters(row['decisions']['repeated_proposal_reasons'] for row in metrics),
            'collection_stop_reasons': _sum_counters(row['decisions']['collection_stop_reasons'] for row in metrics),
            'termination_reasons': dict(Counter(str(row['termination_reason']) for row in metrics)),
            'actual_charged_calls': sum(row['actual_charged_calls'] for row in metrics),
            'unknown_usage_calls': sum(row['unknown_usage_calls'] for row in metrics),
            'actual_input_tokens_known': sum(row['actual_input_tokens_known'] for row in metrics),
            'actual_output_tokens_known': sum(row['actual_output_tokens_known'] for row in metrics),
            'resources_per_run': {field: distribution([row[field] for row in metrics]) for field in
                ('actual_charged_calls', 'model_calls', 'tool_calls', 'native_duration_ms', 'worker_wall_seconds',
                 'model_seconds', 'input_tokens', 'output_tokens', 'estimated_cost_CNY')},
            'model_call_latency_seconds': distribution([call.get('seconds') for call in calls]),
            'cost': estimate_cost(calls, rate_card, pricing_scope=pricing_scope),
            'fixed_original_primary_review': control_summary([item for row in metrics for item in row['original_controls_primary']]),
            'fixed_original_first_pass_review': control_summary([item for row in metrics for item in row['original_controls_first_pass']]),
            'generated_claim_semantic_accuracy': None, 'human_verified': False}


def _effect_values(metrics):
    result = {name: metrics[name] for name in ('actual_charged_calls', 'tool_calls', 'native_duration_ms',
        'worker_wall_seconds', 'estimated_cost_CNY', 'new_independent_observations')}
    result.update(key_records_acquired=metrics['source']['acquired_count'],
        new_key_records_acquired=metrics['new_key_source']['acquired_count'],
        repeated_query_proposals=metrics['decisions']['repeated_or_covered_query_proposals'],
        repeated_query_fraction=metrics['decisions']['repeated_or_covered_fraction'],
        investigation_decision_attempts=metrics['decisions']['investigation_decision_attempts'],
        terminal_structured_interruption=int(metrics['failures']['terminal_structured_interruption']),
        original_relation_mismatches=sum(row['relation_mismatch'] for row in metrics['original_controls_first_pass']),
        original_review_unassessed=sum(not row['assessed'] for row in metrics['original_controls_first_pass']))
    return result


def factorial_effects(rows):
    """Within-case/repetition differences; no independent-accident inference."""
    groups = defaultdict(dict)
    for row in rows:
        groups[(row['case_id'], row['repeat'])][row['arm']] = _effect_values(row['metrics'])
    blocks = []
    for (case, repeat), arms in sorted(groups.items()):
        _require(set(arms) == set(ARMS), 'INCOMPLETE_FACTORIAL_BLOCK')
        effects = {}
        for field in arms[ARMS[0]]:
            a, b, c, d = (arms[arm][field] for arm in ARMS)
            known = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (a, b, c, d))
            effects[field] = {'model_limit_main_effect': ((c - a) + (d - b)) / 2 if known else None,
                'local_read_main_effect': ((b - a) + (d - c)) / 2 if known else None,
                'interaction_difference_in_differences': (d - c) - (b - a) if known else None,
                'local_read_effect_at_model12': b - a if known else None,
                'local_read_effect_at_model16': d - c if known else None,
                'model_effect_at_reads2': c - a if known else None,
                'model_effect_at_reads4': d - b if known else None}
        blocks.append({'case_id': case, 'repeat': repeat, 'effects': effects})
    fields = list(blocks[0]['effects']) if blocks else []
    return {'interpretation': 'descriptive within same case and repetition; correlated cases and stochastic calls; no causal population estimate',
        'formula': {'model_limit_main_effect': 'mean((m16_r2-m12_r2),(m16_r4-m12_r4))',
                    'local_read_main_effect': 'mean((m12_r4-m12_r2),(m16_r4-m16_r2))',
                    'interaction': '(m16_r4-m16_r2)-(m12_r4-m12_r2)'},
        'blocks': blocks,
        'aggregate': {field: {effect: distribution([block['effects'][field][effect] for block in blocks])
            for effect in blocks[0]['effects'][field]} for field in fields}}


def summarize_stage(bundle, rate_card=None, *, pricing_scope=None):
    plan = bundle['plan']
    worlds = {row['case_id']: row for row in plan['world_freezes']}
    rows = [{'case_id': key[0], 'arm': key[1], 'repeat': key[2], 'report': entry['report'],
             'report_path': entry['path'], 'report_sha256': entry['entry']['report_sha256'],
             'metrics': run_metrics(entry['report'], plan, worlds[key[0]], rate_card, pricing_scope=pricing_scope)}
            for key, entry in sorted(bundle['reports'].items())]
    return {'stage': plan['stage'], 'bundle': {'path': bundle['path'], 'sha256': bundle['sha256']},
        'arms': {arm: arm_summary([row for row in rows if row['arm'] == arm], rate_card, pricing_scope=pricing_scope) for arm in ARMS},
        'cases': {case: {arm: arm_summary([row for row in rows if row['case_id'] == case and row['arm'] == arm],
            rate_card, pricing_scope=pricing_scope) for arm in ARMS} for case in plan['cases']},
        'case_families': {str(family): {arm: arm_summary([row for row in rows if row['arm'] == arm
            and row['metrics']['source']['world_kind'] == family], rate_card, pricing_scope=pricing_scope)
            for arm in ARMS} for family in sorted({world.get('world_kind') for world in worlds.values()}, key=str)},
        'factorial_effects': factorial_effects(rows),
        'runs': [{key: value for key, value in row.items() if key != 'report'} for row in rows]}


def summarize(paths, *, evaluator_root=ROOT, comparison_plan=None, rate_card=None, allow_free_smoke=False):
    bundles = [load_bundle(path, evaluator_root=evaluator_root, allow_free_smoke=allow_free_smoke) for path in paths]
    comparable = validate_comparability(bundles, comparison_plan)
    scope = comparison_plan.get('pricing_scope') if comparison_plan else None
    return {'schema_version': 'budget-matrix-summary-v1', 'comparability': comparable,
        'execution_mode': bundles[0]['plan']['execution_mode'],
        'wiring_only': bundles[0]['plan']['execution_mode'] != 'live',
        'stages': {bundle['plan']['stage']: summarize_stage(bundle, rate_card, pricing_scope=scope) for bundle in bundles},
        'limitations': ['Only two budget factors vary; no budget default is selected automatically.',
            'Native outcome, key acquisition, citation and correct causal explanation are distinct.',
            'Fixed-prefix source records already present are excluded from new-key acquisition success.',
            'Original fixed-target references apply only before revision; revised language requires independent audit.',
            'Generated item counts differ by arm; an AI audit is not a paired fixed-target accuracy estimate.',
            'No-claim, failed and unassessed outputs are not semantic successes.',
            'Usage and latency unknowns are not filled with zero.',
            'Latency is observed at the preregistered concurrent worker load, not single-request production latency.',
            'Developer cases and source-derived references are provisional, not independent human gold.',
            'Small repeated samples do not establish production accuracy or population significance.'],
        'human_semantic_gate': 'pending', 'production_accuracy': 'not_established', 'default_budget_changed': False,
        'rollout_recommendation': 'hold'}


def build_blind_packets(bundles, *, rubric, stages=STAGES, seed=None, case_specs=None):
    """Audit every hypothesis/action, retaining no-item and failed reports.

    Program-generated source facts stay in the raw report and source packet,
    but cannot dilute the denominator of model-authored conclusions.
    """
    public, private = _build_blind_packets(bundles, rubric=rubric, stages=stages,
        seed=seed, case_specs=case_specs)
    allowed_kinds = {'hypothesis', 'action', 'pending_action'}
    by_id = {row['opaque_report_id']: row for row in private['reports']}
    excluded = Counter()
    for packet in public['packets']:
        keep = {item['opaque_item_id'] for item in packet['items'] if item['item_kind'] in allowed_kinds}
        excluded.update(item['item_kind'] for item in packet['items'] if item['opaque_item_id'] not in keep)
        packet['items'] = [item for item in packet['items'] if item['opaque_item_id'] in keep]
        linkage = by_id[packet['opaque_report_id']]
        linkage['items'] = [item for item in linkage['items'] if item['opaque_item_id'] in keep]
        # native_item_index deliberately retains the index in the unfiltered
        # native array, so exact-text verification can bind to original bytes.
    public['schema_version'] = 'budget-matrix-blind-audit-packet-v1'
    public['scope'] = {'included_item_kinds': sorted(allowed_kinds),
        'excluded_item_kinds': dict(excluded), 'all_reports_from_selected_input_stages_included': True,
        'program_source_facts_excluded_from_semantic_denominator': True,
        'action_expected_result_prose_audited': False}
    private['schema_version'] = 'budget-matrix-private-linkage-v1'
    private['scope'] = deepcopy(public['scope'])
    return public, private


def _audit_counts(reports):
    items = [item for report in reports for item in report['items']]
    assessed = [item for item in items if item['assessed']]
    accepted = [item for item in assessed if item['native_stance'] == 'accepted']
    return {'reports': len(reports),
        'reports_with_no_claims': sum(row['audit_status'] == 'no_claims' for row in reports),
        'reports_unavailable': sum(row['audit_status'] == 'report_unavailable' for row in reports),
        'reports_with_candidate_unsupported_acceptance': sum(any(item['unsupported_native_acceptance']
            for item in row['items']) for row in reports),
        'items': len(items), 'assessed_items': len(assessed), 'unassessed_items': len(items) - len(assessed),
        'native_accepted_assessed_items': len(accepted),
        'candidate_unsupported_acceptances': sum(item['unsupported_native_acceptance'] for item in assessed),
        'auditor_supported_assessed_items': sum(item['audit']['source_support'] == 'fully_entailed' for item in assessed),
        'supported_native_withheld_items': sum(item['supported_native_withheld'] for item in assessed),
        'local_mechanism_use_items': sum(item['local_mechanism_use'] is True for item in assessed),
        'key_citing_hypotheses_with_local_mechanism_use': sum(item['kind'] == 'hypothesis'
            and bool(item['key_observation_refs']) and item['local_mechanism_use'] is True for item in assessed),
        'source_support_counts': dict(Counter(item['audit']['source_support'] for item in assessed)),
        'by_item_kind': {kind: _audit_item_counts([item for item in items if item['kind'] == kind])
            for kind in sorted({item['kind'] for item in items})},
        'semantic_accuracy': None, 'human_verified': False, 'is_gold': False}


def _audit_item_counts(items):
    assessed = [item for item in items if item['assessed']]
    return {'items': len(items), 'assessed': len(assessed), 'unassessed': len(items) - len(assessed),
        'native_accepted_assessed': sum(item['native_stance'] == 'accepted' for item in assessed),
        'candidate_unsupported_acceptances': sum(item['unsupported_native_acceptance'] for item in assessed),
        'supported_native_withheld': sum(item['supported_native_withheld'] for item in assessed),
        'source_support_counts': dict(Counter(item['audit']['source_support'] for item in assessed))}


def join_blind_audit(packet_path, linkage_path, judgment_path):
    """Bind every audited item back to its exact report, text and source IDs."""
    packet, linkage, audit = _json(packet_path), _json(linkage_path), _json(judgment_path)
    _require(linkage.get('packets_sha256') == _sha(packet_path), 'BLIND_PACKET_HASH_MISMATCH')
    _require(audit.get('packets_sha256') == _sha(packet_path), 'AUDIT_INPUT_HASH_MISMATCH')
    _require(audit.get('rubric_sha256') == packet.get('rubric_sha256') == linkage.get('rubric_sha256'),
             'AUDIT_RUBRIC_MISMATCH')
    _require(audit.get('blinded') is True and audit.get('human_verified') is False and audit.get('is_gold') is False,
             'AUDIT_PROVENANCE_MISSING')
    expected = {row['opaque_report_id']: row for row in linkage['reports']}
    public = {row['opaque_report_id']: row for row in packet['packets']}
    observed = {row['opaque_report_id']: row for row in audit['reports']}
    _require(len(expected) == len(linkage['reports']) and len(public) == len(packet['packets'])
        and len(observed) == len(audit['reports']) and set(observed) == set(expected) == set(public),
        'INCOMPLETE_BLIND_REPORT_SET')
    statuses = {'reviewed', 'abstained', 'report_unavailable', 'no_claims'}
    supports = {'fully_entailed', 'partially_entailed', 'contradicted', 'insufficient', 'abstain'}
    joined = []
    for report_id, private in expected.items():
        raw_path = Path(private['report_path'])
        _require(raw_path.is_file() and _sha(raw_path) == private['report_sha256'], 'AUDITED_REPORT_HASH_MISMATCH')
        native = _json(raw_path)
        _require((native.get('stage'), native.get('case_id'), native.get('arm'), native.get('repeat'))
            == tuple(private[key] for key in ('stage', 'case_id', 'arm', 'repeat')), 'AUDITED_REPORT_IDENTITY_MISMATCH')
        judgment = observed[report_id]
        status = judgment.get('audit_status')
        _require(status in statuses, 'INVALID_AUDIT_REPORT_STATUS')
        private_items = {row['opaque_item_id']: row for row in private['items']}
        public_items = {row['opaque_item_id']: row for row in public[report_id]['items']}
        audit_items = {row['opaque_item_id']: row for row in judgment.get('items', [])}
        _require(len(private_items) == len(private['items']) and len(public_items) == len(public[report_id]['items'])
            and len(audit_items) == len(judgment.get('items', [])), 'DUPLICATE_AUDIT_ITEM')
        _require(set(private_items) == set(public_items) == set(audit_items), 'INCOMPLETE_BLIND_ITEM_SET')
        if not private_items:
            _require(status in {'no_claims', 'report_unavailable'}, 'EMPTY_REPORT_NOT_EXPLICITLY_COVERED')
        else:
            _require(status != 'no_claims', 'NONEMPTY_REPORT_CANNOT_BE_NO_CLAIMS')
        sources = {row['opaque_source_id'] for row in public[report_id]['acquired_sources']}
        items = []
        for item_id, private_item in private_items.items():
            public_item, row = public_items[item_id], audit_items[item_id]
            native_item = native['normalized']['audit_items'][private_item['native_item_index']]
            _require(native_item.get('id') == private_item['native_item_id'] and native_item.get('kind') == private_item['kind']
                == public_item['item_kind'], 'AUDITED_NATIVE_ITEM_IDENTITY_MISMATCH')
            text = native_item.get('text', native_item.get('statement', ''))
            _require(_text_sha(text) == private_item['original_text_sha256'] == _text_sha(public_item['original_text']),
                     'AUDITED_ITEM_TEXT_MISMATCH')
            _require(_text_sha(native_item.get('condition', '')) == private_item['condition_sha256']
                == _text_sha(public_item.get('condition', '')), 'AUDITED_ITEM_CONDITION_MISMATCH')
            _require(row.get('audit_status') in statuses and row.get('source_support') in supports,
                     'INVALID_AUDIT_ITEM_JUDGMENT')
            _require(isinstance(row.get('source_ids'), list) and set(row['source_ids']) <= sources,
                     'AUDIT_SOURCE_ID_NOT_ACQUIRED')
            for flag in ('local_mechanism_use', 'unknown_boundary_preserved'):
                _require(row.get(flag) is None or isinstance(row[flag], bool), 'INVALID_AUDIT_SEMANTIC_FLAG')
            assessed = row['audit_status'] == 'reviewed' and row['source_support'] != 'abstain'
            stance = private_item.get('published_stance')
            approval = private_item.get('native_approval') or {}
            if stance is None and approval:
                verdict = approval.get('submitted_decision') or approval.get('relation') or approval.get('verdict')
                stance = ('not_assessed' if approval.get('protocol_status', 'valid') != 'valid'
                    else 'accepted' if verdict in {'supported', 'appropriate'} else 'withheld')
            items.append({**private_item, 'audit': row, 'assessed': assessed, 'native_stance': stance,
                'unsupported_native_acceptance': assessed and stance == 'accepted' and row['source_support'] != 'fully_entailed',
                'supported_native_withheld': assessed and row['source_support'] == 'fully_entailed'
                    and stance in {'rejected', 'pending', 'withheld'},
                'local_mechanism_use': row.get('local_mechanism_use') if assessed else None})
        joined.append({**{key: private[key] for key in ('stage', 'case_id', 'arm', 'repeat', 'report_sha256')},
            'audit_status': status, 'items': items, 'normalization_coverage': private['normalization_coverage']})
    by_stage = {}
    for stage in sorted({row['stage'] for row in joined}):
        stage_reports = [row for row in joined if row['stage'] == stage]
        by_stage[stage] = {'arms': {arm: _audit_counts([row for row in stage_reports if row['arm'] == arm]) for arm in ARMS},
            'cases': {case: {arm: _audit_counts([row for row in stage_reports if row['arm'] == arm and row['case_id'] == case])
                for arm in ARMS} for case in sorted({row['case_id'] for row in stage_reports})}}
    return {'schema_version': 'budget-matrix-blind-audit-joined-v1', 'blinded_input': True,
        'human_verified': False, 'is_gold': False, 'packets_sha256': _sha(packet_path),
        'judgments_sha256': _sha(judgment_path), 'private_linkage_sha256': _sha(linkage_path),
        'scope': packet.get('scope'), 'stages': by_stage,
        'by_arm': {arm: _audit_counts([row for row in joined if row['arm'] == arm]) for arm in ARMS},
        'reports': joined, 'limitations': ['AI judgments are source-based provisional candidates, not human gold.',
            'Generated items and denominators differ between arms; counts and ratios are not fixed-target accuracy.',
            'Source style may reveal an arm after metadata removal.',
            'Failed, empty and unassessed outputs are not semantic successes.',
            'Program-generated source facts and action expected-result prose are outside the audit-item denominator.'],
        'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', action='append', type=Path)
    parser.add_argument('--comparison-plan', type=Path)
    parser.add_argument('--rate-card', type=Path)
    parser.add_argument('--evaluator-root', type=Path, default=ROOT)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--blind-packets', type=Path)
    parser.add_argument('--private-linkage', type=Path)
    parser.add_argument('--audit-rubric', type=Path)
    parser.add_argument('--case-specs', type=Path)
    parser.add_argument('--audit-stages', nargs='+', choices=STAGES, default=list(STAGES))
    parser.add_argument('--join-judgments', type=Path)
    parser.add_argument('--packet-input', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.join_judgments:
            if not args.packet_input or not args.private_linkage or args.bundle or args.blind_packets:
                parser.error('--join-judgments requires --packet-input and --private-linkage')
            result = join_blind_audit(args.packet_input, args.private_linkage, args.join_judgments)
        elif args.blind_packets:
            if not args.bundle or not args.private_linkage or not args.audit_rubric:
                parser.error('--blind-packets requires bundles, linkage and rubric')
            bundles = [load_bundle(path, evaluator_root=args.evaluator_root) for path in args.bundle]
            validate_comparability(bundles, _json(args.comparison_plan) if args.comparison_plan else None)
            public, private = build_blind_packets(bundles, rubric=_json(args.audit_rubric),
                stages=tuple(args.audit_stages), case_specs=_json(args.case_specs) if args.case_specs else None)
            public['schema_version'] = 'budget-matrix-blind-audit-packet-v1'
            private['schema_version'] = 'budget-matrix-private-linkage-v1'
            _write_json(args.blind_packets, public)
            private['packets_sha256'] = _sha(args.blind_packets)
            _write_json(args.private_linkage, private)
            result = {'reports': len(public['packets']), 'items': sum(len(row['items']) for row in public['packets']),
                'packets_sha256': private['packets_sha256'], 'human_semantic_gate': 'pending'}
        else:
            if not args.bundle:
                parser.error('--bundle is required')
            result = summarize(args.bundle, evaluator_root=args.evaluator_root,
                comparison_plan=_json(args.comparison_plan) if args.comparison_plan else None,
                rate_card=_json(args.rate_card) if args.rate_card else None)
    except ComparabilityError as exc:
        print(json.dumps({'comparability': 'failed', 'reason': str(exc)}, ensure_ascii=False))
        return 2
    _write_json(args.output, result)
    print(json.dumps({'output': str(args.output.resolve()), 'comparability': 'passed'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
