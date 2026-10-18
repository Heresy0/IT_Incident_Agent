"""Case-level RCA evaluation. Native approvals and AI audits are never gold.

Human judgments are deliberately separate from acquisition and delivery metrics.
An incomplete review produces coverage information, never an apparent zero score.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path


class ReviewError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


EVIDENCE_FIELDS = ()  # First panel is item-level source support, without stance.
ROOT_FIELDS = ('causal_condition_correct', 'causal_chain_correct', 'mechanism_correct',
    'wrong_definitive_cause', 'root_claim_made', 'abstained_from_root',
    'evidence_chain_sufficient', 'scope_correct', 'unsupported_definitive_cause',
    'unknown_boundary_preserved', 'missing_discriminating_information_identified',
    'discriminating_next_step_proposed')
IDENTIFIABILITY = {'root_identifiable', 'mechanism_only', 'not_identifiable'}


def human_header(document):
    require(document.get('reviewer_type') == 'human', 'HUMAN_REVIEW_REQUIRED')
    require(isinstance(document.get('reviewer_id'), str) and document['reviewer_id'].strip(), 'REVIEWER_ID_REQUIRED')
    require(document.get('attestation') == 'I personally reviewed this material; this is my human judgment.',
            'EXPLICIT_HUMAN_ATTESTATION_REQUIRED')
    require(isinstance(document.get('reviewed_at'), str) and document['reviewed_at'].strip(), 'REVIEW_DATE_REQUIRED')
    require(document.get('ai_generated_gold') is False, 'AI_CANNOT_SUPPLY_GOLD')


def _ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def mechanical_references_valid(report):
    """Reference identity, allowed path and exact source value must agree."""
    sources = {row['evidence_id']: row for row in report.get('normalized', {}).get('observations', [])}
    for item in report.get('normalized', {}).get('audit_items', []):
        for ref in item.get('refs', []) + item.get('counter_refs', []):
            if not isinstance(ref, dict) or ref.get('evidence_id') not in sources:
                return False
            source = sources[ref['evidence_id']]
            path = ref.get('field_path')
            if not isinstance(path, str) or not re.fullmatch(r'[^.\[\]]+(?:\[\d+\]|\.[^.\[\]]+)*', path):
                return False
            allowed = source.get('source_fields', {}).get('allowed_field_paths')
            if allowed is not None and path not in allowed:
                return False
            value = source.get('payload', {})
            try:
                for name, index in re.findall(r'([^\.\[\]]+)|\[(\d+)\]', path):
                    value = value[int(index)] if index else value[name]
            except (KeyError, TypeError, IndexError, ValueError):
                return False
            if 'value' not in ref or canonical_sha(value) != canonical_sha(ref['value']):
                return False
    return True


def score_run(evidence, root, identifiability, *, has_output, delivery_failed=False,
              mechanical_references_valid=True, established_assertions_supported=True):
    """One final conclusion set is one outcome, irrespective of item count."""
    require(identifiability in IDENTIFIABILITY, 'UNKNOWN_IDENTIFIABILITY')
    for row, names in ((evidence, EVIDENCE_FIELDS), (root, ROOT_FIELDS)):
        require(all(type(row.get(name)) is bool for name in names), 'COMPLETE_BINARY_HUMAN_JUDGMENT_REQUIRED')
    require(not (root['root_claim_made'] and root['abstained_from_root']), 'CONTRADICTORY_ROOT_JUDGMENT')
    wrong = root['wrong_definitive_cause'] or root['unsupported_definitive_cause']
    bounded = root['unknown_boundary_preserved'] and not wrong
    strict = bool(has_output and not delivery_failed and mechanical_references_valid and identifiability == 'root_identifiable'
        and root['causal_condition_correct'] and root['causal_chain_correct'] and root['mechanism_correct']
        and root['root_claim_made'] and root['evidence_chain_sufficient'] and established_assertions_supported
        and root['scope_correct'] and root['unknown_boundary_preserved'] and not wrong)
    mechanism = bool(has_output and not delivery_failed and mechanical_references_valid and identifiability == 'mechanism_only'
        and root['mechanism_correct'] and root['evidence_chain_sufficient'] and established_assertions_supported
        and root['scope_correct'] and bounded and not root['root_claim_made'])
    unknown = bool(has_output and not delivery_failed and mechanical_references_valid and identifiability == 'not_identifiable'
        and root['abstained_from_root'] and not root['root_claim_made'] and bounded
        and root['scope_correct'] and root['missing_discriminating_information_identified']
        and root['discriminating_next_step_proposed'])
    return {'strict_root_success': strict, 'mechanism_success': mechanism,
        'unknown_boundary_success': unknown, 'wrong_confirmation': root['wrong_definitive_cause'],
        'unsupported_confirmation': root['unsupported_definitive_cause'],
        'wrong_or_unsupported_confirmation': wrong,
        'boundary_overreach': identifiability != 'root_identifiable' and (wrong or root['root_claim_made']),
        'answerable_abstention': identifiability == 'root_identifiable' and root['abstained_from_root'],
        'evidence_chain_sufficient': root['evidence_chain_sufficient'] and established_assertions_supported,
        'causal_condition_correct': root['causal_condition_correct'],
        'causal_chain_correct': root['causal_chain_correct'],
        'has_output': bool(has_output), 'delivery_failed': bool(delivery_failed),
        'mechanical_references_valid': bool(mechanical_references_valid)}


def aggregate_case_metrics(rows, *, expected_repeats):
    """Do not pool generated items. Stability counts group case × condition × mode."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row['condition'], row['mode'], row['case_id'])].append(row)
    result = {}
    for condition, mode in sorted({key[:2] for key in groups}):
        selected = [row for key, group in groups.items() if key[:2] == (condition, mode) for row in group]
        stratified = {kind: [row for row in selected if row['identifiability'] == kind] for kind in IDENTIFIABILITY}
        root, mechanism, unknown = (stratified[k] for k in ('root_identifiable', 'mechanism_only', 'not_identifiable'))
        blocks = [group for key, group in groups.items() if key[:2] == (condition, mode)]
        require(all(len(group) == expected_repeats and len({r['repeat'] for r in group}) == expected_repeats
                    for group in blocks), 'INCOMPLETE_CASE_REPEAT_BLOCK')
        root_blocks = [group for group in blocks if group[0]['identifiability'] == 'root_identifiable']
        stability = Counter(sum(row['metrics']['strict_root_success'] for row in group) for group in root_blocks)
        result[condition + ':' + mode] = {
            'cases': len(blocks), 'runs': len(selected), 'repeats_per_case': expected_repeats,
            'strict_root_location_run_rate': _ratio(sum(r['metrics']['strict_root_success'] for r in root), len(root)),
            'strict_root_location_case_majority': _ratio(sum(sum(r['metrics']['strict_root_success'] for r in group)
                >= expected_repeats // 2 + 1 for group in root_blocks), len(root_blocks)),
            'strict_root_location_case_all_repeats': _ratio(sum(all(r['metrics']['strict_root_success'] for r in group)
                for group in root_blocks), len(root_blocks)),
            'mechanism_location': _ratio(sum(r['metrics']['mechanism_success'] for r in mechanism), len(mechanism)),
            'unknown_boundary': _ratio(sum(r['metrics']['unknown_boundary_success'] for r in unknown), len(unknown)),
            'wrong_confirmation': _ratio(sum(r['metrics']['wrong_confirmation'] for r in selected), len(selected)),
            'unsupported_confirmation': _ratio(sum(r['metrics']['unsupported_confirmation'] for r in selected), len(selected)),
            'wrong_or_unsupported_confirmation': _ratio(sum(r['metrics']['wrong_or_unsupported_confirmation'] for r in selected), len(selected)),
            'cases_with_any_wrong_confirmation': sum(any(r['metrics']['wrong_confirmation'] for r in group) for group in blocks),
            'cases_with_any_unsupported_confirmation': sum(any(r['metrics']['unsupported_confirmation'] for r in group) for group in blocks),
            'cases_with_any_boundary_overreach': sum(any(r['metrics']['boundary_overreach'] for r in group) for group in blocks),
            'boundary_overreach': _ratio(sum(r['metrics']['boundary_overreach'] for r in mechanism + unknown), len(mechanism + unknown)),
            'answerable_abstention': _ratio(sum(r['metrics']['answerable_abstention'] for r in root), len(root)),
            'root_evidence_chain_sufficiency': _ratio(sum(r['metrics']['evidence_chain_sufficient'] for r in root), len(root)),
            'mechanism_evidence_chain_sufficiency': _ratio(sum(r['metrics']['evidence_chain_sufficient'] for r in mechanism), len(mechanism)),
            'causal_condition_correct_run_rate': _ratio(sum(r['metrics']['causal_condition_correct'] for r in root), len(root)),
            'causal_chain_correct_run_rate': _ratio(sum(r['metrics']['causal_chain_correct'] for r in root), len(root)),
            'root_case_successes_out_of_repeats': {str(n): stability[n] for n in range(expected_repeats + 1)},
            'independence_warning': 'repetitions_and_variants_are_correlated_not_independent_incidents',
            'completion_is_not_accuracy': True}
    return result


PROVIDER_CODES = {'QUOTA_EXCEEDED', 'RATE_LIMITED', 'RATE_LIMIT_EXCEEDED', 'TIMEOUT',
    'NETWORK_ERROR', 'AUTHENTICATION_ERROR', 'PROVIDER_ERROR', 'INSUFFICIENT_QUOTA',
    'API_ERROR', 'SERVER_ERROR', 'CONNECTION_ERROR', 'AUTH_ERROR', 'RATE_LIMIT',
    'MODEL_ERROR', 'PROVIDER_AUTH_MISSING'}


def resource_metrics(report, rate_card=None, *, limits=None):
    """Reuse pricing/distribution, correcting legacy provider classification by raw code."""
    from scripts.summarize_frozen_versions import distribution, estimate_cost
    calls = [row for row in report.get('model_call_log', []) if row.get('charged_request')]
    known = [row for row in calls if row.get('usage') is not None]
    for call in known:
        require(all(type(call['usage'].get(name)) is int and call['usage'][name] >= 0
            for name in ('input_tokens', 'output_tokens')), 'INVALID_USAGE')
    failures = deepcopy(report.get('normalized', {}).get('failures', []))
    # A provider error may only appear in raw call logs, while older normalizers
    # classify a terminal workflow error as "other". Never inherit a false zero.
    codes = Counter(str(row.get('code', 'UNKNOWN')) for row in failures)
    call_codes = Counter(str(row['error'].get('code', 'UNKNOWN') if isinstance(row['error'], dict)
        else row['error']) for row in calls if row.get('error'))
    for code, count in call_codes.items():
        codes[code] = max(codes[code], count)
    provider_codes = {code: count for code, count in codes.items() if code in PROVIDER_CODES
        or 'QUOTA' in code or code.startswith('HTTP_')}
    events = report.get('native_report', {}).get('events', [])
    decisions = [row for row in report.get('decision_trace', []) if row.get('is_collection_decision') is True
        or row.get('stage') in {'investigate', 'collect', 'collection', 'query'}]
    repeats = [row for row in events if row.get('type') == 'query_reused' and row.get('role') == 'investigation']
    require(len(repeats) <= len(decisions), 'REPEATED_DECISION_DENOMINATOR_INVALID')
    summary = report.get('run_summary', {})
    bound_limits = limits if limits is not None else summary.get('limits', {})
    violations, assessed, unassessed = [], [], []
    for name, field, multiplier in (('model_calls', 'model_calls', 1), ('tool_calls', 'tool_calls', 1),
                                  ('web_calls', 'web_calls', 1), ('seconds', 'duration_ms', 1000)):
        limit, actual = bound_limits.get(name), summary.get(field)
        if isinstance(limit, (int, float)) and isinstance(actual, (int, float)):
            assessed.append(name)
            if actual > limit * multiplier:
                violations.append(name)
        else:
            unassessed.append(name)
    if isinstance(bound_limits.get('model_calls'), (int, float)) and len(calls) > bound_limits['model_calls']:
        violations.append('charged_model_calls')
    terminal_interruption = any(row.get('category') == 'model_protocol' and row.get('terminal')
        and not row.get('recovered') for row in failures)
    return {'charged_calls': len(calls), 'known_usage_calls': len(known),
        'delivery_status': report.get('native_report', {}).get('status'),
        'native_model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
        'web_calls': summary.get('web_calls'), 'worker_wall_seconds': report.get('worker_wall_seconds'),
        'hard_budget': {'assessed_dimensions': assessed, 'unassessed_dimensions': unassessed,
            'violations': violations, 'status': 'violation' if violations else 'unassessed' if unassessed else 'within_limits',
            'frozen_limits': bound_limits,
            'reported_limits_match': summary.get('limits') == bound_limits if summary.get('limits') is not None else None},
        'unknown_usage_calls': len(calls) - len(known),
        'input_tokens_known': sum(c['usage']['input_tokens'] for c in known),
        'output_tokens_known': sum(c['usage']['output_tokens'] for c in known),
        'input_tokens_total': sum(c['usage']['input_tokens'] for c in known) if len(known) == len(calls) else None,
        'output_tokens_total': sum(c['usage']['output_tokens'] for c in known) if len(known) == len(calls) else None,
        'cost': estimate_cost(calls, rate_card, pricing_scope=rate_card.get('pricing_scope') if rate_card else None),
        'model_call_latency_seconds': distribution([c['seconds'] for c in calls if isinstance(c.get('seconds'), (int, float))]),
        'duration_ms': report.get('run_summary', {}).get('duration_ms'),
        'provider_failures': sum(provider_codes.values()), 'provider_failure_codes': provider_codes,
        'raw_failure_codes': dict(codes),
        'structured_unrecovered': sum(row.get('category') == 'model_protocol' and not row.get('recovered') for row in failures),
        'terminal_structured_interruption': terminal_interruption,
        'repeated_decisions': len(repeats), 'collection_decisions': len(decisions),
        'repeated_fraction': len(repeats) / len(decisions) if decisions else None,
        'repeat_denominator': 'all_collection_decisions_including_finish_and_invalid',
        'collection_stop_reasons': dict(Counter(row.get('reason', 'unknown') for row in events if row.get('type') == 'collection_completed')),
        'termination_reason': report.get('run_summary', {}).get('termination_reason')}
