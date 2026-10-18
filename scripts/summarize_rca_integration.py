"""Offline mechanical citation and reviewer-sensitivity observations, not gold."""
import argparse
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.rca_scoring import (canonical_sha, read_json, sha_file, write_json,
    mechanical_references_valid, resource_metrics)
from scripts.evaluate_rca_integration import acquired_content_sha, acquired_pack_sha, require
from scripts.summarize_frozen_versions import distribution, estimate_cost


IDENTITY = ('task_id', 'group_id', 'case_id', 'comparison_kind', 'condition', 'repeat', 'stage', 'system_id')


def _field_refs(refs, source_rows):
    return sorted({(source_rows.get(r.get('evidence_id'), '<unknown_source>'), r.get('field_path', '<missing_field>'))
                   for r in refs if isinstance(r, dict)})


def citation_observation(report):
    """Exact acquired/cited fields only. Neither relevance nor truth is inferred."""
    native, normalized = report.get('native_report', {}), report.get('normalized', {})
    evidence = native.get('evidence', [])
    rows = {r['evidence_id']: r.get('locator', r['evidence_id']) for r in evidence}
    acquired = sorted(rows.values())
    claims = normalized.get('final_claims', [])
    author = set()
    claim_rows = []
    for claim in claims:
        selected = _field_refs(claim.get('refs', []), rows)
        counter = _field_refs(claim.get('counter_refs', []), rows)
        author.update(selected)
        author.update(counter)
        claim_rows.append({'id': claim.get('id'), 'statement': claim.get('statement'),
            'level': claim.get('level'), 'published_stance': claim.get('published_stance'),
            'support_fields': selected, 'counter_fields': counter})
    def review(batch):
        return [{'target_id': r.get('target_id'), 'relation': r.get('relation'),
            'submitted_relation': r.get('submitted_relation'), 'protocol_status': r.get('protocol_status'),
            'issue': r.get('issue'), 'selected_fields': _field_refs(r.get('refs', []), rows)}
            for r in batch.get('items', [])]
    primary = review(native.get('primary_review') or native.get('first_pass', {}).get('review', {}))
    final = review(native.get('final_review') or {'items': native.get('item_reviews', [])})
    reviewer = {tuple(field) for r in final for field in r['selected_fields']}
    author_sources = {row for row, field in author}
    return {'acquired_content_sha256': acquired_content_sha(report), 'acquired_pack_sha256': acquired_pack_sha(report),
        'acquired_sources': acquired,
        'mechanical_author_reference_values_valid': mechanical_references_valid(report),
        'claims': claim_rows, 'author_selected_fields': sorted(author),
        'acquired_sources_without_claim_reference': sorted(set(acquired) - author_sources),
        'primary_review': primary, 'final_review': final,
        'reviewer_selected_author_fields': sorted(reviewer & author),
        'reviewer_selected_fields_not_in_author_refs': sorted(reviewer - author),
        'interpretation': 'registered_fields_and_model_verdicts_only; unused_source_is_not_semantic_error'}


def load_bundle(path):
    path = Path(path).resolve()
    bundle = read_json(path)
    require(bundle.get('schema_version') == 'rca-integration-bundle-v1', 'WRONG_DIAGNOSTIC_BUNDLE')
    plan = bundle['plan']
    require(plan.get('schema_version') == 'rca-integration-plan-v1', 'WRONG_DIAGNOSTIC_PLAN')
    reports, seen = [], set()
    for entry in bundle['runs']:
        key = tuple(entry[k] for k in IDENTITY)
        require(key not in seen, 'DUPLICATE_REPORT')
        seen.add(key)
        target = (path.parent / entry['report_file']).resolve()
        require(target.parent == path.parent and sha_file(target) == entry['report_sha256'], 'REPORT_HASH_MISMATCH')
        report = read_json(target)
        require(all(report.get(k) == entry[k] for k in IDENTITY) and report.get('report_id') == entry['report_id'],
                'REPORT_IDENTITY_MISMATCH')
        require(report.get('application') == plan['application'] and report.get('model_settings') == plan['model_settings']
                and report.get('provider_configuration_sha256') == plan['provider_configuration_sha256'], 'REPORT_CONFIGURATION_MISMATCH')
        require(report.get('execution_mode') == bundle['execution_mode'], 'EXECUTION_MODE_MISMATCH')
        require(report.get('public_freeze') == plan['public_freezes'][entry['task_id']], 'PUBLIC_FREEZE_MISMATCH')
        require(canonical_sha(report['task']) == report['public_freeze']['task_sha256'], 'PUBLIC_TASK_CHANGED')
        require(report.get('acquired_content_sha256') == acquired_content_sha(report), 'ACQUIRED_CONTENT_CHANGED')
        require(report.get('acquired_pack_sha256') == acquired_pack_sha(report), 'ACQUIRED_PACK_CHANGED')
        if report['native_report'].get('status') != 'harness_failed':
            require(report.get('observed_world_sha256') == report['public_freeze']['world_sha256'] and
                report.get('observed_capability_sha256') == report['public_freeze']['capability_sha256'], 'OBSERVED_WORLD_MISMATCH')
            require(report.get('run_summary', {}).get('limits') == plan['limits'], 'LIMITS_MISMATCH')
        for call in report.get('model_call_log', []):
            require(call.get('model') == ('scripted-wiring' if bundle['execution_mode'] == 'scripted_control_only' else plan['model']),
                    'MODEL_MISMATCH')
            require(not call.get('response_model_name') or bundle['execution_mode'] != 'live_observation_only'
                    or call['response_model_name'] == plan['model'], 'RESOLVED_MODEL_MISMATCH')
        reports.append(report)
    actual = Counter(tuple(e[k] for k in IDENTITY) for e in bundle['runs'] + bundle.get('scheduled_pending', []))
    expected = Counter(tuple(e[k] for k in IDENTITY) for e in plan['schedule'])
    require(actual == expected and sum(expected.values()) == plan['planned_runs'], 'SCHEDULE_COVERAGE_MISMATCH')
    return bundle, reports


def reconcile_accounting(bundle, reports):
    paid = bundle.get('paid_budget')
    if paid is None:
        return {'status': 'scripted_no_paid_ledger', 'charged_calls': 0}
    calls = [c for r in reports for c in r.get('model_call_log', []) if c.get('charged_request')]
    ids = [c.get('reservation_id') for c in calls]
    require(None not in ids and len(ids) == len(set(ids)), 'PAID_RESERVATION_ID_INVALID')
    events = paid['session_reservation_events']
    reserves = {e['reservation_id']: e for e in events if e['type'] == 'reserve'}
    settles = {e['reservation_id']: e for e in events if e['type'] == 'settle'}
    require(set(ids) == set(reserves) == set(settles), 'PAID_CALL_LEDGER_MISMATCH')
    require(not paid['pending_reservation_ids'] and paid['session_requests'] == len(calls), 'PAID_RESERVATIONS_UNRESOLVED')
    known, unknown = 0, 0
    for call in calls:
        event = settles[call['reservation_id']]
        require(event['known_usage'] == (call.get('usage') is not None), 'USAGE_STATUS_MISMATCH')
        if event['known_usage']:
            require(all(event['usage'][k] == call['usage'][k] for k in ('input_tokens', 'output_tokens')), 'KNOWN_USAGE_MISMATCH')
            known += 1
        else:
            unknown += 1
    for key in ('input_tokens', 'output_tokens'):
        expected = sum(e['usage'][key] if e['known_usage'] else reserves[rid][key] for rid, e in settles.items())
        require(paid['charged_delta'][key] == expected, 'TOKEN_SETTLEMENT_MISMATCH')
    require(paid['unknown_usage_calls_delta'] == unknown, 'UNKNOWN_USAGE_COUNT_MISMATCH')
    return {'status': 'reconciled', 'charged_calls': len(calls), 'known_usage_calls': known,
        'unknown_usage_calls': unknown, 'unknown_tokens_are_conservative_reservations': True,
        'pending_reservations': 0, 'persistence_errors': paid.get('persistence_errors', [])}


def build_summary(bundle, reports, *, rate_card=None, native_bundles=()):
    plan = bundle['plan']
    rows, grouped = [], defaultdict(list)
    for report in reports:
        row = {k: report[k] for k in IDENTITY}
        row.update(report_id=report['report_id'], observations=citation_observation(report),
                   resources=resource_metrics(report, rate_card, limits=plan['limits']))
        rows.append(row)
        grouped[(report['group_id'], report['repeat'])].append(row)
    paired = []
    for (group, repeat), values in sorted(grouped.items()):
        hashes = {r['observations']['acquired_pack_sha256'] for r in values}
        expected = [e for e in plan['schedule'] if e['group_id'] == group and e['repeat'] == repeat]
        ready = len(values) == len(expected) and len(hashes) == 1 and None not in hashes
        paired.append({'group_id': group, 'repeat': repeat, 'paired_inputs_confirmed': ready,
            'acquired_pack_sha256': next(iter(hashes)) if ready else None,
            'observations': [{'condition': r['condition'], 'report_id': r['report_id'],
                'final_review': r['observations']['final_review']} for r in values],
            'semantic_correctness': None})
    stability = []
    for group, condition in sorted({(r['group_id'], r['condition']) for r in rows}):
        values = [r for r in rows if r['group_id'] == group and r['condition'] == condition]
        verdicts = [r['observations']['final_review'][0].get('relation')
                    if len(r['observations']['final_review']) == 1 else None for r in values]
        stability.append({'group_id': group, 'condition': condition, 'recorded_repeats': len(values),
            'planned_repeats': plan['repeats'], 'native_relation_counts': dict(Counter(str(v) for v in verdicts)),
            'all_repeats_same_native_relation': len(values) == plan['repeats'] and None not in verdicts and len(set(verdicts)) == 1,
            'accuracy': None})
    calls = [c for r in reports for c in r.get('model_call_log', []) if c.get('charged_request')]
    return {'schema_version': 'rca-integration-summary-v1', 'human_verified': False, 'is_gold': False,
        'human_semantic_gate': 'pending', 'formal_metrics': None, 'semantic_error_rate': None,
        'planned_runs': plan['planned_runs'], 'recorded_runs': len(reports),
        'scheduled_pending': bundle.get('scheduled_pending', []),
        'accounting': reconcile_accounting(bundle, reports),
        'cost': estimate_cost(calls, rate_card, pricing_scope=rate_card.get('pricing_scope') if rate_card else None),
        'worker_latency_seconds': distribution([r.get('worker_wall_seconds') for r in reports]),
        'paired_groups': paired, 'repeat_observations': stability, 'runs': rows,
        'native_evidence_integration': summarize_native(native_bundles, plan, rate_card),
        'interpretation_limits': [
            'Model verdicts and reference coverage are observations, never human semantic gold.',
            'The native reviewer can independently cite all acquired sources; missing or wrong author fields do not force rejection.',
            'Wording pairs compare question interpretations; relation changes do not establish improved model behavior.',
            'Reproduced worlds differ from old authored worlds; their results must not be pooled as before/after accuracy.',
            'Three repeats per cell describe local repeatability, not independent cases or production accuracy.',
            'No cross-request or cross-time ablation is claimed without registered observations.']}


def summarize_native(paths, diagnostic_plan, rate_card=None):
    from scripts.review_rca import load_inputs
    rows, groups = [], defaultdict(list)
    for path in paths:
        path = Path(path).resolve()
        bundle = read_json(path)
        require(bundle.get('schema_version') == 'rca-run-bundle-v1', 'WRONG_NATIVE_BUNDLE')
        plan = bundle['plan']
        bundle, dataset, verified_records = load_inputs(path, plan['dataset_freeze']['manifest_path'])
        for key in ('application', 'model', 'model_settings', 'limits', 'provider_configuration_sha256'):
            require(plan.get(key) == diagnostic_plan.get(key), 'NATIVE_DIAGNOSTIC_CONFIGURATION_MISMATCH:' + key)
        require(plan.get('repeats', 0) >= 3, 'NATIVE_COMPARISON_NEEDS_THREE_REPEATS')
        native_identity = ('case_id', 'repeat', 'stage', 'system_id', 'control_id')
        require(Counter(tuple(e.get(k) for k in native_identity) for e in
            bundle['runs'] + bundle.get('scheduled_pending', [])) ==
            Counter(tuple(e.get(k) for k in native_identity) for e in plan['schedule']), 'NATIVE_SCHEDULE_COVERAGE_MISMATCH')
        for record in verified_records:
            entry, report = record['entry'], record['report']
            if entry['stage'] not in {'e2e', 'sufficient_evidence'}:
                continue
            row = {k: entry[k] for k in ('report_id', 'case_id', 'repeat', 'stage', 'system_id')}
            row.update(public_sha256=report['world_freeze']['public_sha256'],
                observations=citation_observation(report), resources=resource_metrics(report, rate_card, limits=plan['limits']))
            rows.append(row)
            groups[(entry['case_id'], entry['repeat'], entry['system_id'])].append(row)
    pairs = []
    for (case, repeat, system), values in sorted(groups.items()):
        require(len({v['stage'] for v in values}) == len(values), 'DUPLICATE_NATIVE_COMPARISON_CELL')
        for left, right in combinations(values, 2):
            require(left['public_sha256'] == right['public_sha256'], 'NATIVE_PAIRED_WORLD_DIFFERS')
            a, b = left['observations'], right['observations']
            pairs.append({'case_id': case, 'repeat': repeat, 'system_id': system,
                'stages': [left['stage'], right['stage']],
                'same_actual_evidence_content': a['acquired_content_sha256'] is not None and
                    a['acquired_content_sha256'] == b['acquired_content_sha256'],
                'same_query_quality_pack': a['acquired_pack_sha256'] is not None and
                    a['acquired_pack_sha256'] == b['acquired_pack_sha256'],
                'left_only_acquired_sources': sorted(set(a['acquired_sources']) - set(b['acquired_sources'])),
                'right_only_acquired_sources': sorted(set(b['acquired_sources']) - set(a['acquired_sources'])),
                'semantic_evidence_utilization': None})
    return {'runs': rows, 'paired_actual_evidence_checks': pairs,
            'semantic_evidence_utilization': None, 'human_review_status': 'pending'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--native-bundle', type=Path, action='append', default=[])
    parser.add_argument('--rate-card', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bundle, reports = load_bundle(args.bundle)
    result = build_summary(bundle, reports, rate_card=read_json(args.rate_card) if args.rate_card else None,
                           native_bundles=args.native_bundle)
    result['source_bundle'] = {'path': str(args.bundle.resolve()), 'sha256': sha_file(args.bundle)}
    write_json(args.output, result)
    print({'output': str(args.output), 'recorded_runs': result['recorded_runs'], 'formal_metrics': None})


if __name__ == '__main__':
    main()
