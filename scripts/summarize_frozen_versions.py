"""Offline paired summaries for two fully frozen application snapshots.

Native review verdicts are retained. Only a prespecified shared binary
acceptance boundary is compared; legacy verdicts are never given new four-class
labels. Source acquisition, explicit citation, and audited local-mechanism use
are three different outcomes. No model or application module is imported.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import secrets
import statistics

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('original', 'current')


class ComparabilityError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise ComparabilityError(code)


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text_sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def distribution(values):
    known = sorted(value for value in values if isinstance(value, (int, float))
                   and not isinstance(value, bool) and math.isfinite(value))
    return {'known': len(known), 'unknown': len(values) - len(known),
            'sum': sum(known) if known else None,
            'mean': statistics.mean(known) if known else None,
            'median': statistics.median(known) if known else None,
            'p95': known[math.ceil(.95 * len(known)) - 1] if known else None,
            'p95_method': 'nearest_rank', 'small_sample': len(known) < 20}


def _verify_hashes(root, mapping, *, complete_app=False):
    root = Path(root).resolve()
    _require(bool(mapping), 'MISSING_SOURCE_FREEZE')
    if complete_app:
        actual = {path.relative_to(root).as_posix() for path in (root / 'app').rglob('*.py')}
        _require(set(mapping) == actual, 'APP_SOURCE_SET_CHANGED')
    for relative, expected in mapping.items():
        target = (root / relative).resolve()
        _require(target.is_relative_to(root), 'SOURCE_PATH_OUTSIDE_ROOT')
        _require(target.is_file() and _sha(target) == expected, 'SOURCE_HASH_MISMATCH:' + relative)


def load_bundle(path, *, evaluator_root=ROOT):
    path = Path(path).resolve()
    bundle = _json(path)
    plan = bundle['plan']
    _require(plan.get('execution_mode') == 'live', 'LIVE_ARTIFACTS_REQUIRED')
    _require(plan.get('stage') in {'e2e', 'review'}, 'UNKNOWN_STAGE')
    _require(tuple(plan.get('arms', [])) == ARMS, 'VERSION_ARM_SET_MISMATCH')
    _require(set(plan.get('applications', {})) == set(ARMS), 'APPLICATION_SET_MISMATCH')
    _require(bool(plan.get('model')) and bool(plan.get('model_settings')), 'MODEL_SETTINGS_MISSING')
    _require(bool(plan.get('provider_configuration_sha256')), 'PROVIDER_CONFIGURATION_MISSING')
    _require(isinstance(plan.get('repeats'), int) and plan['repeats'] >= 1, 'INVALID_REPEATS')
    _verify_hashes(evaluator_root, plan.get('evaluation_source_sha256', {}))
    for application in plan['applications'].values():
        _require(bool(application.get('source_version')), 'APPLICATION_VERSION_MISSING')
        _require(Path(application['app_root']).resolve() == (Path(application['source_root']) / 'app').resolve(),
                 'APPLICATION_ROOT_MISMATCH')
        _verify_hashes(application['source_root'], application.get('app_source_sha256', {}), complete_app=True)
    freezes = {row['case_id']: row for row in plan['world_freezes']}
    _require(len(freezes) == len(plan['cases']) and set(freezes) == set(plan['cases']), 'WORLD_FREEZE_SET_MISMATCH')
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
        _require(report.get('application') == plan['applications'].get(key[1]), 'REPORT_APPLICATION_MISMATCH')
        _require(report.get('execution_mode') == 'live', 'REPORT_EXECUTION_MODE_MISMATCH')
        _require(report.get('world_freeze') == freezes.get(key[0]), 'REPORT_WORLD_MISMATCH')
        _require(report.get('observed_world_sha256') == freezes[key[0]].get('world_sha256')
                 and report.get('observed_capability_sha256') == freezes[key[0]].get('metadata_sha256'),
                 'WORKER_OBSERVED_WORLD_MISMATCH')
        loaded = report.get('loaded_application_modules')
        _require(isinstance(loaded, dict) and bool(loaded), 'WORKER_IMPORT_PROVENANCE_MISSING')
        application = report['application']
        source_root, app_root = Path(application['source_root']).resolve(), Path(application['app_root']).resolve()
        for filename in loaded.values():
            module_path = Path(filename).resolve()
            _require(module_path.is_relative_to(app_root), 'WORKER_APPLICATION_IMPORT_ESCAPED')
            relative = module_path.relative_to(source_root).as_posix()
            _require(module_path.is_file() and application['app_source_sha256'].get(relative) == _sha(module_path),
                     'WORKER_MODULE_HASH_MISMATCH')
        _require(report.get('run_summary', {}).get('limits') == plan['limits'], 'REPORT_LIMITS_MISMATCH')
        normalized = report.get('normalized', {})
        _require(isinstance(normalized.get('observations'), list)
                 and isinstance(normalized.get('final_claims'), list)
                 and isinstance(normalized.get('failures'), list), 'NORMALIZED_REPORT_MISSING')
        calls = report.get('model_call_log')
        _require(isinstance(calls, list), 'MODEL_CALL_LOG_MISSING')
        for call in calls:
            _require(call.get('model') == plan['model'], 'ACTUAL_MODEL_MISMATCH')
            _require(isinstance(call.get('charged_request'), bool), 'PAID_ADMISSION_MISSING')
            _require(bool(call.get('request_sha256')) and (call.get('error') or bool(call.get('output_sha256'))),
                     'MODEL_REQUEST_OUTPUT_HASH_MISSING')
            usage = call.get('usage')
            _require(usage is None or (isinstance(usage, dict) and all(
                isinstance(usage.get(field), int) and not isinstance(usage[field], bool) and usage[field] >= 0
                for field in ('input_tokens', 'output_tokens'))), 'INVALID_USAGE')
        reports[key] = {'report': report, 'entry': entry, 'path': str(target)}
    expected = {(case_id, arm, repeat) for case_id in plan['cases'] for arm in ARMS
                for repeat in range(1, plan['repeats'] + 1)}
    _require(set(reports) == expected and len(reports) == plan.get('planned_runs'), 'INCOMPLETE_PAIRED_RUN_SET')
    return {'path': str(path), 'sha256': _sha(path), 'plan': plan, 'reports': reports}


def validate_comparability(bundles, comparison_plan=None):
    _require(bool(bundles), 'NO_BUNDLES')
    _require(len({bundle['plan']['stage'] for bundle in bundles}) == len(bundles), 'DUPLICATE_STAGE')
    first = bundles[0]['plan']
    shared = ('version', 'applications', 'model', 'model_settings', 'provider_configuration_sha256',
              'limits', 'evaluation_source_sha256')
    for bundle in bundles:
        plan = bundle['plan']
        for field in shared:
            _require(plan.get(field) == first.get(field), 'PLAN_MISMATCH:' + field)
        if comparison_plan:
            expected = comparison_plan['stages'][plan['stage']]
            _require(plan['cases'] == expected['cases'] and plan['repeats'] == expected['repeats'],
                     'PRESPECIFIED_RUN_SET_MISMATCH')
            for field in ('applications', 'model', 'model_settings', 'provider_configuration_sha256', 'limits'):
                if field in comparison_plan:
                    _require(plan[field] == comparison_plan[field], 'PRESPECIFIED_FIELD_MISMATCH:' + field)
            if 'world_freezes' in comparison_plan:
                freezes = comparison_plan['world_freezes']
                indexed = freezes if isinstance(freezes, dict) else {row['case_id']: row for row in freezes}
                _require(all(indexed.get(row['case_id']) == row for row in plan['world_freezes']), 'PRESPECIFIED_WORLD_MISMATCH')
            if plan['stage'] == 'review' and 'review_targets' in comparison_plan:
                _require(plan.get('review_targets') == comparison_plan['review_targets'], 'PRESPECIFIED_REVIEW_TARGET_MISMATCH')
    return {'status': 'passed', 'varying_parameter': 'complete_application_snapshot',
            'paired_world_model_settings_provider_and_limits': 'identical',
            'legacy_four_class_mapping': 'not_performed'}


def binary_controls(report, plan):
    if plan['stage'] != 'review':
        return []
    targets = plan.get('review_targets', {}).get(report['case_id'], [])
    _require(bool(targets), 'REVIEW_TARGETS_MISSING')
    observed = report['normalized'].get('review_controls', [])
    indexed = {row['target_id']: row for row in observed}
    _require(len(indexed) == len(observed), 'DUPLICATE_REVIEW_CONTROL')
    _require(set(indexed) <= {row['target_id'] for row in targets}, 'UNKNOWN_REVIEW_CONTROL')
    result = []
    for target in targets:
        row = indexed.get(target['target_id'], {})
        if row:
            _require(row.get('original_text_sha256') == target['original_text_sha256'], 'FIXED_TARGET_TEXT_CHANGED')
            _require(row.get('kind') == target['kind'], 'FIXED_TARGET_KIND_CHANGED')
        accept = row.get('accept')
        _require(accept is None or isinstance(accept, bool), 'INVALID_BINARY_ACCEPT')
        assessed = row.get('protocol_status') == 'valid' and accept is not None
        expected = target['expected_accept']
        _require(isinstance(expected, bool), 'INVALID_EXPECTED_ACCEPT')
        result.append({'target_id': target['target_id'], 'native_target_id': row.get('native_target_id'),
            'kind': target['kind'], 'expected_accept': expected,
            'accept': accept if assessed else None, 'assessed': assessed,
            'native_verdict': row.get('native_verdict'), 'protocol_status': row.get('protocol_status', 'not_run'),
            'false_accept': assessed and not expected and accept,
            'false_reject_or_withheld': assessed and expected and not accept,
            'correct_binary_boundary': assessed and accept == expected,
            'reference_kind': 'source_constructed_provisional', 'human_verified': False,
            'four_class_relation': None})
    return result


def source_metrics(report, world):
    observations = report['normalized']['observations']
    identifiers = [row['observation_id'] for row in observations]
    # The same observation may occur in more than one legal query window.
    acquired = set(identifiers)
    required = set(world.get('key_observation_ids', []))
    selected = required & acquired
    evidence_sources = defaultdict(set)
    for row in observations:
        evidence_sources[row['evidence_id']].add(row['observation_id'])
    claims = report['normalized']['final_claims']
    supporting, counter = set(), set()
    for claim in claims:
        for field, destination in (('refs', supporting), ('counter_refs', counter)):
            for ref in claim.get(field, []):
                destination.update(evidence_sources.get(ref.get('evidence_id'), set()))
    eligible = bool(world.get('eligible_for_positive_acquisition_rate', bool(required)))
    return {'eligible_for_positive_acquisition_rate': eligible,
            'eligible_for_observation_coverage_rate': bool(world.get('eligible_for_observation_coverage_rate', bool(required))),
            'key_metric_kind': world.get('key_metric_kind'),
            'world_kind': world.get('world_kind'), 'matched_world_group': world.get('matched_world_group'),
            'required_ids': sorted(required), 'required_count': len(required),
            'acquired_ids': sorted(selected), 'acquired_count': len(selected),
            'omitted_ids': sorted(required - acquired),
            'all_required_acquired': bool(required) and required <= acquired if eligible else None,
            'cited_by_final_claim_refs_ids': sorted(selected & supporting),
            'cited_by_final_claim_counter_refs_ids': sorted(selected & counter),
            'acquired_but_not_cited_ids': sorted(selected - supporting - counter),
            'local_mechanism_use': None,
            'citation_is_not_semantic_mechanism_verification': True}


def classify_failures(report):
    rows = report['normalized']['failures']
    categories = {'model_protocol', 'provider', 'budget', 'other', 'unclassified'}
    for row in rows:
        _require(row.get('category') in categories, 'UNCLASSIFIED_FAILURE_CATEGORY')
        _require(isinstance(row.get('recovered'), bool) and isinstance(row.get('terminal'), bool),
                 'FAILURE_RECOVERY_OR_TERMINAL_STATE_MISSING')
    return {'terminal_structured_interruption': any(row['category'] == 'model_protocol' and row['terminal']
                and not row['recovered'] for row in rows),
            'unrecovered_model_protocol_failures': sum(row['category'] == 'model_protocol' and not row['recovered'] for row in rows),
            'recovered_model_protocol_failures': sum(row['category'] == 'model_protocol' and row['recovered'] for row in rows),
            'provider_failures': sum(row['category'] == 'provider' for row in rows),
            'budget_failures': sum(row['category'] == 'budget' for row in rows),
            'unclassified_failures': sum(row['category'] == 'unclassified' for row in rows),
            'native_failure_codes': dict(Counter(row['category'] + ':' + str(row.get('code')) for row in rows)),
            'normalization_coverage': report['normalized'].get('coverage', {})}


def run_metrics(report, plan, world):
    calls = [row for row in report['model_call_log'] if row['charged_request']]
    known = [row for row in calls if row.get('usage') is not None]
    summary = report['run_summary']
    violations = [field for field in ('model_calls', 'tool_calls', 'web_calls')
                  if isinstance(summary.get(field), (int, float)) and summary[field] > plan['limits'][field]]
    if len(calls) > plan['limits']['model_calls'] and 'model_calls' not in violations:
        violations.append('model_calls')
    if isinstance(summary.get('duration_ms'), (int, float)) and summary['duration_ms'] > plan['limits']['seconds'] * 1000:
        violations.append('seconds')
    return {'completed': report.get('status', report.get('native_report', {}).get('status')) == 'completed',
            'completion_is_not_causal_accuracy': True,
            'execution_error': bool(report.get('execution_error') or report.get('native_report', {}).get('execution_error')),
            'model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
            'duration_ms': summary.get('duration_ms'), 'actual_charged_calls': len(calls),
            'unpaid_invoke_attempts': len(report['model_call_log']) - len(calls),
            'known_usage_calls': len(known), 'unknown_usage_calls': len(calls) - len(known),
            'actual_input_tokens_known': sum(row['usage']['input_tokens'] for row in known),
            'actual_output_tokens_known': sum(row['usage']['output_tokens'] for row in known),
            'input_tokens': sum(row['usage']['input_tokens'] for row in known) if len(known) == len(calls) else None,
            'output_tokens': sum(row['usage']['output_tokens'] for row in known) if len(known) == len(calls) else None,
            'model_seconds': sum(row['seconds'] for row in calls) if all(isinstance(row.get('seconds'), (int, float)) for row in calls) else None,
            'hard_limit_violations': violations, 'failures': classify_failures(report),
            'source': source_metrics(report, world), 'binary_controls': binary_controls(report, plan)}


def binary_summary(rows):
    controls = [control for row in rows for control in row['metrics']['binary_controls']]
    assessed = [row for row in controls if row['assessed']]
    positives = [row for row in assessed if row['expected_accept']]
    negatives = [row for row in assessed if not row['expected_accept']]
    groups = defaultdict(list)
    for row in rows:
        for control in row['metrics']['binary_controls']:
            groups[(row['arm'], row['case_id'], control['target_id'])].append(control)
    repeated = [group for group in groups.values() if len(group) > 1]
    fully_assessed = [group for group in repeated if all(row['assessed'] for row in group)]
    flips = sum(len({row['accept'] for row in group}) > 1 for group in fully_assessed)
    return {'reference_items': len(controls), 'assessed_items': len(assessed),
            'unassessed_items': len(controls) - len(assessed),
            'positive_reference_items_assessed': len(positives), 'negative_reference_items_assessed': len(negatives),
            'false_accepts': sum(row['false_accept'] for row in negatives),
            'false_accept_rate': sum(row['false_accept'] for row in negatives) / len(negatives) if negatives else None,
            'false_rejects_or_withheld': sum(row['false_reject_or_withheld'] for row in positives),
            'false_reject_or_withheld_rate': sum(row['false_reject_or_withheld'] for row in positives) / len(positives) if positives else None,
            'binary_boundary_agreement': sum(row['correct_binary_boundary'] for row in assessed),
            'native_verdict_counts': dict(Counter(str(row['native_verdict']) for row in assessed)),
            'repeated_groups': len(repeated), 'fully_assessed_repeated_groups': len(fully_assessed),
            'groups_with_binary_flips': flips,
            'binary_flip_rate': flips / len(fully_assessed) if fully_assessed else None,
            'four_class_agreement': None, 'human_verified': False, 'is_gold': False}


def estimate_cost(calls, rate_card=None, *, pricing_scope=None):
    result = {'currency': 'CNY', 'estimated_total': None, 'known_usage_estimated_subtotal': None,
              'unknown_usage_calls': sum(row.get('usage') is None for row in calls),
              'unpriced_calls': len(calls), 'is_actual_vendor_bill': False}
    if not rate_card:
        return {**result, 'status': 'not_estimated', 'reason': 'NO_RATE_CARD'}
    if not pricing_scope or rate_card.get('pricing_scope') != pricing_scope:
        return {**result, 'status': 'not_estimated', 'reason': 'PUBLIC_PRICE_SCOPE_NOT_CONFIRMED_OR_MISMATCHED'}
    if rate_card.get('currency') != 'CNY' or not rate_card.get('effective_date') or not str(rate_card.get('source_url', '')).startswith(('https://', 'http://')):
        return {**result, 'status': 'not_estimated', 'reason': 'INCOMPLETE_PUBLIC_PRICE_PROVENANCE'}
    subtotal, priced, over_tier = 0.0, 0, 0
    for call in calls:
        model_rate = rate_card.get('models', {}).get(call['model'])
        if not model_rate or call.get('usage') is None:
            continue
        input_tokens = call['usage']['input_tokens']
        rates = model_rate.get('tiers', [model_rate])
        matching = [rate for rate in rates if isinstance(rate.get('max_input_tokens'), int)
                    and rate.get('min_input_tokens_exclusive', -1) < input_tokens <= rate['max_input_tokens']]
        if len(matching) != 1:
            over_tier += 1
            continue
        rate = matching[0]
        if not all(isinstance(rate.get(field), (int, float)) and rate[field] >= 0
                   for field in ('input_per_million', 'output_per_million')):
            continue
        subtotal += (call['usage']['input_tokens'] * rate['input_per_million']
                     + call['usage']['output_tokens'] * rate['output_per_million']) / 1_000_000
        priced += 1
    return {**result, 'status': 'estimated' if priced == len(calls) else 'partial_estimate',
            'estimated_total': subtotal if priced == len(calls) else None,
            'known_usage_estimated_subtotal': subtotal if priced else None,
            'priced_calls': priced, 'unpriced_calls': len(calls) - priced,
            'input_tier_unmatched_calls': over_tier,
            'source_url': rate_card['source_url'], 'effective_date': rate_card['effective_date'],
            'pricing_scope': pricing_scope, 'estimate_kind': 'published_non_cached_list_price_not_actual_bill'}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def build_blind_packets(bundles, *, rubric, stages=('e2e',), seed=None, case_specs=None):
    """Project all final items without native stance; keep the linkage private.

    The output's wording and acquired evidence remain exact. Version, model,
    native IDs, file paths, approval and completion labels are excluded. Source
    style may still reveal a version; this is practical, not external blinding.
    """
    rng = random.Random(seed if seed is not None else secrets.randbits(128))
    opaque = lambda prefix: prefix + f'{rng.getrandbits(96):024x}'
    packets, linkage = [], []
    for bundle in bundles:
        if bundle['plan']['stage'] not in stages:
            continue
        for key, entry in sorted(bundle['reports'].items()):
            report = entry['report']
            normalized = report['normalized']
            _require(isinstance(normalized.get('audit_items'), list), 'AUDIT_ITEMS_MISSING')
            report_id = opaque('R')
            sources, evidence_aliases = [], {}
            for observation in normalized['observations']:
                evidence_id = observation['evidence_id']
                if evidence_id in evidence_aliases:
                    continue
                source_id = opaque('S')
                evidence_aliases[evidence_id] = source_id
                fields = observation.get('source_fields', {})
                sources.append({'opaque_source_id': source_id, 'kind': observation.get('kind'),
                    'payload': observation.get('payload', {}),
                    **{name: fields[name] for name in ('service', 'observed_at', 'data_version',
                        'allowed_field_paths', 'truncated') if name in fields},
                    **({'read_status': fields['status']} if 'status' in fields else {})})
            def blind_refs(refs):
                result = []
                for ref in refs:
                    _require(isinstance(ref, dict), 'AUDIT_REFERENCE_NOT_RESOLVED')
                    selected = {name: ref[name] for name in ('field_path', 'value', 'unit', 'observed_at',
                                 'data_version', 'quote') if name in ref}
                    selected['opaque_source_id'] = evidence_aliases.get(ref.get('evidence_id'))
                    if selected['opaque_source_id'] is None:
                        selected['reference_unresolved'] = True
                    result.append(selected)
                return result
            items, private_items = [], []
            for index, item in enumerate(normalized['audit_items']):
                item_id = opaque('T')
                text = item.get('text', item.get('statement', ''))
                _require(isinstance(text, str), 'AUDIT_ITEM_TEXT_MISSING')
                items.append({'opaque_item_id': item_id, 'item_kind': item.get('kind'),
                    'original_text': text, 'condition': item.get('condition', ''),
                    'support_refs': blind_refs(item.get('refs', [])),
                    'counter_refs': blind_refs(item.get('counter_refs', []))})
                native_id = item.get('id')
                native_claim = next((claim for claim in normalized['final_claims'] if claim.get('id') == native_id), {})
                private_items.append({'opaque_item_id': item_id, 'native_item_id': native_id,
                    'native_item_index': index, 'kind': item.get('kind'),
                    'original_text_sha256': _text_sha(text), 'condition_sha256': _text_sha(item.get('condition', '')),
                    'native_approval': item.get('native_approval'),
                    'published_stance': item.get('published_stance', native_claim.get('published_stance')),
                    'key_observation_refs': sorted({observation['observation_id'] for observation in normalized['observations']
                        for ref in item.get('refs', []) if ref.get('evidence_id') == observation['evidence_id']
                        and observation['observation_id'] in report['world_freeze'].get('key_observation_ids', [])})})
            quality = []
            for query in normalized.get('queries', []):
                error = query.get('error')
                quality.append({name: query.get(name) for name in ('tool', 'name', 'truncated') if name in query}
                    | {'read_status': query.get('status'),
                       'error_code': error.get('code') if isinstance(error, dict) else error})
            question = report.get('audit_ticket_question', '')
            if case_specs is not None:
                spec = case_specs.get(key[0])
                _require(spec is not None and _fingerprint(spec['world']) == report['world_freeze']['world_sha256'],
                         'AUDIT_CONTEXT_WORLD_MISMATCH')
                ticket = spec['world']['ticket']
                question = {name: ticket[name] for name in ('title', 'symptoms', 'description', 'scope') if name in ticket}
            if not question:
                native_ticket = report.get('native_report', {}).get('ticket', {})
                question = native_ticket.get('description', native_ticket.get('title', '')) if isinstance(native_ticket, dict) else ''
            rng.shuffle(items)
            rng.shuffle(sources)
            packets.append({'opaque_report_id': report_id, 'ticket_question': question,
                'acquired_sources': sources, 'query_quality': quality, 'items': items})
            linkage.append({'opaque_report_id': report_id, 'stage': bundle['plan']['stage'],
                'case_id': key[0], 'arm': key[1], 'repeat': key[2],
                'report_path': entry['path'], 'report_sha256': entry['entry']['report_sha256'],
                'items': private_items, 'report_available': not bool(report.get('execution_error')
                    or report.get('native_report', {}).get('execution_error')),
                'eligible_for_positive_acquisition_rate': report['world_freeze'].get('eligible_for_positive_acquisition_rate', False),
                'normalization_coverage': normalized.get('coverage', {})})
    _require(bool(packets), 'NO_BLIND_AUDIT_REPORTS')
    rng.shuffle(packets)
    rubric_sha = hashlib.sha256(json.dumps(rubric, sort_keys=True, ensure_ascii=False,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()
    public = {'schema_version': 'native-version-blind-audit-packet-v1', 'rubric_sha256': rubric_sha,
              'packets': packets}
    private = {'schema_version': 'native-version-blind-linkage-private-v1',
        'bundle_sha256': [bundle['sha256'] for bundle in bundles if bundle['plan']['stage'] in stages],
        'rubric_sha256': rubric_sha, 'reports': linkage,
        'do_not_provide_to_blind_auditor': True}
    return public, private


def join_blind_audit(packet_path, linkage_path, judgment_path):
    """Verify complete blinded coverage before joining native acceptance labels."""
    packet, linkage, audit = _json(packet_path), _json(linkage_path), _json(judgment_path)
    _require(linkage.get('packets_sha256') == _sha(packet_path), 'BLIND_PACKET_HASH_MISMATCH')
    _require(audit.get('packets_sha256') == _sha(packet_path), 'AUDIT_INPUT_HASH_MISMATCH')
    _require(audit.get('rubric_sha256') == packet.get('rubric_sha256') == linkage.get('rubric_sha256'),
             'AUDIT_RUBRIC_MISMATCH')
    _require(audit.get('blinded') is True and audit.get('human_verified') is False and audit.get('is_gold') is False,
             'AUDIT_PROVENANCE_MISSING')
    expected_reports = {row['opaque_report_id']: row for row in linkage['reports']}
    public_reports = {row['opaque_report_id']: row for row in packet['packets']}
    observed_reports = {row['opaque_report_id']: row for row in audit['reports']}
    _require(len(observed_reports) == len(audit['reports']) and set(observed_reports) == set(expected_reports) == set(public_reports),
             'INCOMPLETE_BLIND_REPORT_SET')
    statuses = {'reviewed', 'abstained', 'report_unavailable', 'no_claims'}
    supports = {'fully_entailed', 'partially_entailed', 'contradicted', 'insufficient', 'abstain'}
    joined = []
    for report_id, private in expected_reports.items():
        raw_report = Path(private['report_path'])
        _require(raw_report.is_file() and _sha(raw_report) == private['report_sha256'], 'AUDITED_REPORT_HASH_MISMATCH')
        native = _json(raw_report)
        judgment = observed_reports[report_id]
        status = judgment.get('audit_status')
        _require(status in statuses, 'INVALID_AUDIT_REPORT_STATUS')
        expected_items = {row['opaque_item_id']: row for row in private['items']}
        public_items = {row['opaque_item_id']: row for row in public_reports[report_id]['items']}
        observed_items = {row['opaque_item_id']: row for row in judgment.get('items', [])}
        _require(len(observed_items) == len(judgment.get('items', [])), 'DUPLICATE_AUDIT_ITEM')
        if not expected_items:
            _require(status in {'no_claims', 'report_unavailable'} and not observed_items, 'EMPTY_REPORT_NOT_EXPLICITLY_COVERED')
        else:
            _require(set(observed_items) == set(expected_items) == set(public_items), 'INCOMPLETE_BLIND_ITEM_SET')
        items = []
        for item_id, private_item in expected_items.items():
            public_item = public_items[item_id]
            native_item = native['normalized']['audit_items'][private_item['native_item_index']]
            text = native_item.get('text', native_item.get('statement', ''))
            _require(_text_sha(text) == private_item['original_text_sha256'] == _text_sha(public_item['original_text']),
                     'AUDITED_ITEM_TEXT_MISMATCH')
            _require(_text_sha(native_item.get('condition', '')) == private_item['condition_sha256']
                     == _text_sha(public_item.get('condition', '')), 'AUDITED_ITEM_CONDITION_MISMATCH')
            row = observed_items[item_id]
            _require(row.get('audit_status') in statuses and row.get('source_support') in supports,
                     'INVALID_AUDIT_ITEM_JUDGMENT')
            assessed = row['audit_status'] == 'reviewed' and row['source_support'] != 'abstain'
            supported = row['source_support'] == 'fully_entailed'
            stance = private_item.get('published_stance')
            approval = private_item.get('native_approval') or {}
            if stance is None and approval:
                verdict = approval.get('submitted_decision') or approval.get('relation') or approval.get('verdict')
                stance = ('not_assessed' if approval.get('protocol_status', 'valid') != 'valid'
                          else 'accepted' if verdict in {'supported', 'appropriate'} else 'withheld')
            accepted = stance == 'accepted'
            items.append({**private_item, 'audit': row, 'assessed': assessed,
                'native_stance': stance,
                'unsupported_native_acceptance': assessed and accepted and not supported,
                'supported_native_withheld': assessed and supported and stance in {'rejected', 'pending', 'withheld'},
                'local_mechanism_use': row.get('local_mechanism_use') if assessed else None})
        joined.append({**{key: private[key] for key in ('stage', 'case_id', 'arm', 'repeat', 'report_sha256')},
                       'audit_status': status, 'items': items,
                       'eligible_for_positive_acquisition_rate': private.get('eligible_for_positive_acquisition_rate', False),
                       'normalization_coverage': private['normalization_coverage']})
    by_arm = {}
    for arm in ARMS:
        reports = [row for row in joined if row['arm'] == arm]
        items = [item for report in reports for item in report['items']]
        assessed = [item for item in items if item['assessed']]
        accepted = [item for item in assessed if item['native_stance'] == 'accepted']
        supported = [item for item in assessed if item['audit']['source_support'] == 'fully_entailed']
        def counts(group):
            assessed_group = [item for item in group if item['assessed']]
            return {'items': len(group), 'assessed': len(assessed_group),
                'unassessed': len(group) - len(assessed_group),
                'native_accepted_assessed': sum(item['native_stance'] == 'accepted' for item in assessed_group),
                'candidate_unsupported_acceptances': sum(item['unsupported_native_acceptance'] for item in assessed_group),
                'auditor_supported_assessed': sum(item['audit']['source_support'] == 'fully_entailed' for item in assessed_group),
                'supported_native_withheld': sum(item['supported_native_withheld'] for item in assessed_group),
                'native_not_assessed_or_unknown': sum(item['native_stance'] in {None, 'not_assessed'} for item in assessed_group),
                'source_support_counts': dict(Counter(item['audit']['source_support'] for item in assessed_group))}
        by_arm[arm] = {'reports': len(reports), 'reports_with_no_claims': sum(row['audit_status'] == 'no_claims' for row in reports),
            'reports_unavailable': sum(row['audit_status'] == 'report_unavailable' for row in reports),
            'reports_with_candidate_unsupported_acceptance': sum(any(item['unsupported_native_acceptance'] for item in row['items']) for row in reports),
            'items': len(items), 'assessed_items': len(assessed), 'unassessed_items': len(items) - len(assessed),
            'native_accepted_assessed_items': len(accepted),
            'unsupported_native_acceptances': sum(item['unsupported_native_acceptance'] for item in assessed),
            'auditor_supported_assessed_items': len(supported),
            'supported_native_withheld_items': sum(item['supported_native_withheld'] for item in assessed),
            'local_mechanism_use_items': sum(item['local_mechanism_use'] is True for item in assessed),
            'key_citing_hypotheses_with_local_mechanism_use': sum(item['kind'] == 'hypothesis'
                and bool(item['key_observation_refs']) and item['local_mechanism_use'] is True for item in assessed),
            'by_item_kind': {kind: counts([item for item in items if item['kind'] == kind]) for kind in sorted({item['kind'] for item in items})},
            'source_support_counts': dict(Counter(item['audit']['source_support'] for item in assessed))}
    return {'schema_version': 'native-version-blind-audit-joined-v1', 'blinded_input': True,
            'human_verified': False, 'is_gold': False, 'packets_sha256': _sha(packet_path),
            'judgments_sha256': _sha(judgment_path), 'private_linkage_sha256': _sha(linkage_path),
            'by_arm': by_arm, 'reports': joined,
            'limitations': ['Source-based AI judgments are provisional, not independent human gold.',
                'Unsupported acceptance and supported withholding are candidate AI-audited concerns, not precision or accuracy.',
                'Source style may reveal a version even after metadata and native stance removal.',
                'Missing or unassessed output is not counted as semantic correctness.',
                'Natural items differ by version; these counts are not a paired fixed-target accuracy score.'],
            'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'}


def arm_summary(rows, rate_card=None, *, pricing_scope=None):
    metrics = [row['metrics'] for row in rows]
    calls = [call for row in rows for call in row['report']['model_call_log'] if call['charged_request']]
    sources = [row['source'] for row in metrics]
    eligible = [row for row in sources if row['eligible_for_positive_acquisition_rate']]
    coverage_eligible = [row for row in sources if row['eligible_for_observation_coverage_rate']]
    return {'runs': len(rows), 'completed': sum(row['completed'] for row in metrics),
            'completed_is_not_accuracy': True,
            'execution_error_runs': sum(row['execution_error'] for row in metrics),
            'terminal_structured_interruption_runs': sum(row['failures']['terminal_structured_interruption'] for row in metrics),
            'runs_with_recovered_model_protocol_failure': sum(row['failures']['recovered_model_protocol_failures'] > 0 for row in metrics),
            'runs_with_provider_failures': sum(row['failures']['provider_failures'] > 0 for row in metrics),
            'runs_with_budget_failures': sum(row['failures']['budget_failures'] > 0 for row in metrics),
            'runs_with_unclassified_failures': sum(row['failures']['unclassified_failures'] > 0 for row in metrics),
            'failure_normalization_complete_runs': sum(row['failures']['normalization_coverage'].get('normalization_complete') is True for row in metrics),
            'failure_normalization_incomplete_runs': sum(row['failures']['normalization_coverage'].get('normalization_complete') is not True for row in metrics),
            'native_failure_codes': dict(Counter({code: sum(row['failures']['native_failure_codes'].get(code, 0) for row in metrics)
                for code in {code for row in metrics for code in row['failures']['native_failure_codes']}})),
            'hard_limit_violation_runs': sum(bool(row['hard_limit_violations']) for row in metrics),
            'positive_acquisition_eligible_runs': len(eligible),
            'all_required_key_records_acquired_runs': sum(row['all_required_acquired'] is True for row in eligible),
            'key_records_required': sum(row['required_count'] for row in eligible),
            'key_records_acquired': sum(row['acquired_count'] for row in eligible),
            'observation_coverage_eligible_runs': len(coverage_eligible),
            'coverage_records_required': sum(row['required_count'] for row in coverage_eligible),
            'coverage_records_acquired': sum(row['acquired_count'] for row in coverage_eligible),
            'key_records_cited_by_final_claim_refs': sum(len(row['cited_by_final_claim_refs_ids']) for row in sources),
            'key_records_cited_by_final_claim_counter_refs': sum(len(row['cited_by_final_claim_counter_refs_ids']) for row in sources),
            'key_records_acquired_but_not_cited': sum(len(row['acquired_but_not_cited_ids']) for row in sources),
            'audited_local_mechanism_use': None,
            'actual_charged_calls': sum(row['actual_charged_calls'] for row in metrics),
            'known_usage_calls': sum(row['known_usage_calls'] for row in metrics),
            'unknown_usage_calls': sum(row['unknown_usage_calls'] for row in metrics),
            'actual_input_tokens_known': sum(row['actual_input_tokens_known'] for row in metrics),
            'actual_output_tokens_known': sum(row['actual_output_tokens_known'] for row in metrics),
            'resources_per_run': {field: distribution([row[field] for row in metrics]) for field in
                ('model_calls', 'tool_calls', 'input_tokens', 'output_tokens', 'duration_ms', 'model_seconds')},
            'model_call_latency_seconds': distribution([call.get('seconds') for call in calls]),
            'cost': estimate_cost(calls, rate_card, pricing_scope=pricing_scope),
            'estimated_cost_CNY_per_run': distribution([estimate_cost(
                [call for call in row['report']['model_call_log'] if call['charged_request']], rate_card,
                pricing_scope=pricing_scope)['estimated_total'] for row in rows]),
            'fixed_target_binary_review': binary_summary(rows),
            'natural_claim_semantic_accuracy': None}


def summarize_stage(bundle, rate_card=None, *, pricing_scope=None):
    plan = bundle['plan']
    worlds = {row['case_id']: row for row in plan['world_freezes']}
    rows = [{'case_id': key[0], 'arm': key[1], 'repeat': key[2],
             'report_path': item['path'], 'report_sha256': item['entry']['report_sha256'],
             'report': item['report'], 'metrics': run_metrics(item['report'], plan, worlds[key[0]])}
            for key, item in sorted(bundle['reports'].items())]
    grouped = {arm: [row for row in rows if row['arm'] == arm] for arm in ARMS}
    pairs = []
    for case in plan['cases']:
        for repeat in range(1, plan['repeats'] + 1):
            pair = {row['arm']: row['metrics'] for row in rows if row['case_id'] == case and row['repeat'] == repeat}
            a, b = pair['original'], pair['current']
            pairs.append({'case_id': case, 'repeat': repeat,
                'current_minus_original': {field: b[field] - a[field]
                    if isinstance(a.get(field), (int, float)) and isinstance(b.get(field), (int, float)) else None
                    for field in ('model_calls', 'tool_calls', 'duration_ms', 'input_tokens', 'output_tokens')},
                'completed_delta': int(b['completed']) - int(a['completed']),
                'key_records_acquired_delta': b['source']['acquired_count'] - a['source']['acquired_count'],
                'terminal_structured_interruption_delta': int(b['failures']['terminal_structured_interruption'])
                    - int(a['failures']['terminal_structured_interruption'])})
    return {'stage': plan['stage'], 'bundle': {'path': bundle['path'], 'sha256': bundle['sha256']},
            'arms': {arm: arm_summary(grouped[arm], rate_card, pricing_scope=pricing_scope) for arm in ARMS},
            'cases': {case: {arm: arm_summary([row for row in grouped[arm] if row['case_id'] == case], rate_card,
                       pricing_scope=pricing_scope) for arm in ARMS} for case in plan['cases']},
            'case_families': {family: {arm: arm_summary([row for row in grouped[arm]
                if row['metrics']['source']['world_kind'] == family], rate_card, pricing_scope=pricing_scope)
                for arm in ARMS} for family in {world.get('world_kind') for world in worlds.values()}},
            'matched_world_groups': {group: {arm: arm_summary([row for row in grouped[arm]
                if row['metrics']['source']['matched_world_group'] == group], rate_card, pricing_scope=pricing_scope)
                for arm in ARMS} for group in {world.get('matched_world_group') for world in worlds.values()}},
            'paired_deltas': pairs,
            'runs': [{key: value for key, value in row.items() if key != 'report'} for row in rows]}


def summarize(paths, *, evaluator_root=ROOT, comparison_plan=None, rate_card=None):
    bundles = [load_bundle(path, evaluator_root=evaluator_root) for path in paths]
    comparable = validate_comparability(bundles, comparison_plan)
    scope = comparison_plan.get('pricing_scope') if comparison_plan else None
    return {'schema_version': 'frozen-version-summary-v1', 'comparability': comparable,
            'complete_prespecified_stages': set(bundle['plan']['stage'] for bundle in bundles)
                == set(comparison_plan['stages'] if comparison_plan else ('e2e', 'review')),
            'stages': {bundle['plan']['stage']: summarize_stage(bundle, rate_card, pricing_scope=scope) for bundle in bundles},
            'limitations': ['Full application snapshots differ; all other declared experimental inputs are paired.',
                'Source-derived key records and binary references are provisional, not human gold.',
                'Citation, local-mechanism use, and causal correctness are different outcomes.',
                'Legacy verdicts have not been translated into current four-class relations.',
                'A native uncertain result is withheld acceptance, not proof of contradiction.',
                'Original and anonymized cases are related perturbations, not independent accidents.',
                'Missing claims, missing reports and unassessed targets are not semantic successes.',
                'Completed is a native delivery contract and not a comparable causal accuracy score.',
                'Small repeated samples and nearest-rank p95 do not establish population performance.'],
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', action='append', type=Path)
    parser.add_argument('--evaluator-root', type=Path, default=ROOT)
    parser.add_argument('--comparison-plan', type=Path)
    parser.add_argument('--rate-card', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--blind-packets', type=Path, help='Write identity-stripped packets instead of a summary.')
    parser.add_argument('--private-linkage', type=Path, help='Never provide this file to the blind auditor.')
    parser.add_argument('--audit-rubric', type=Path)
    parser.add_argument('--case-specs', type=Path, help='Frozen input worlds provide the exact ticket question and business scope.')
    parser.add_argument('--audit-stages', nargs='+', default=['e2e'])
    parser.add_argument('--join-judgments', type=Path, help='Join completed blinded judgments to their private linkage.')
    parser.add_argument('--packet-input', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.join_judgments:
            if not args.packet_input or not args.private_linkage or args.bundle or args.blind_packets:
                parser.error('--join-judgments requires --packet-input/--private-linkage, without --bundle/--blind-packets')
            result = join_blind_audit(args.packet_input, args.private_linkage, args.join_judgments)
        elif args.blind_packets:
            if not args.bundle or not args.private_linkage or not args.audit_rubric:
                parser.error('--blind-packets requires --bundle/--private-linkage/--audit-rubric')
            bundles = [load_bundle(path, evaluator_root=args.evaluator_root) for path in args.bundle]
            validate_comparability(bundles, _json(args.comparison_plan) if args.comparison_plan else None)
            public, private = build_blind_packets(bundles, rubric=_json(args.audit_rubric), stages=tuple(args.audit_stages),
                                                 case_specs=_json(args.case_specs) if args.case_specs else None)
            _write_json(args.blind_packets, public)
            private['packets_sha256'] = _sha(args.blind_packets)
            _write_json(args.private_linkage, private)
            result = {'schema_version': 'native-version-blind-packet-build-v1',
                'packet_path': str(args.blind_packets.resolve()), 'packets_sha256': private['packets_sha256'],
                'reports': len(public['packets']), 'items': sum(len(row['items']) for row in public['packets']),
                'private_linkage_written': True, 'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'}
        else:
            if not args.bundle:
                parser.error('--bundle is required for a summary')
            result = summarize(args.bundle, evaluator_root=args.evaluator_root,
                               comparison_plan=_json(args.comparison_plan) if args.comparison_plan else None,
                               rate_card=_json(args.rate_card) if args.rate_card else None)
    except ComparabilityError as exc:
        print(json.dumps({'comparability': 'failed', 'reason': str(exc)}, ensure_ascii=False))
        return 2
    _write_json(args.output, result)
    print(json.dumps({'output': str(args.output.resolve()), 'comparability': 'passed',
                      'stages': list(result.get('stages', {})), 'rollout_recommendation': 'hold'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
