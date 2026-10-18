"""Offline RCA scorecard: human gold gates, paired cases and resource accounting."""
import argparse
import hashlib
import re
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.rca_scoring import (aggregate_case_metrics, canonical_sha, read_json,
    require, resource_metrics, score_run, sha_file, write_json, mechanical_references_valid)
from scripts.review_rca import (bundle_binding, import_reviews, load_bundles,
    load_materials, verify_report_bindings, visible_items)


def _paired_results(rows):
    by_condition = defaultdict(dict)
    for row in rows:
        key = (row['mode'], row['case_id'], row['repeat'])
        require(key not in by_condition[row['condition']], 'DUPLICATE_PAIRED_RUN')
        by_condition[row['condition']][key] = row
    comparisons = []
    for left, right in combinations(sorted(by_condition), 2):
        a, b = by_condition[left], by_condition[right]
        require(set(a) == set(b), 'PAIRED_FORMAL_RUN_SET_MISMATCH')
        blocks = defaultdict(list)
        for key in a:
            require(a[key]['identifiability'] == b[key]['identifiability'], 'PAIRED_TRUTH_MISMATCH')
            if a[key]['identifiability'] != 'root_identifiable':
                continue
            blocks[key[:2]].append((a[key]['metrics']['strict_root_success'], b[key]['metrics']['strict_root_success']))
        wins, ties, losses = 0, 0, 0
        cases = []
        for (mode, case), values in sorted(blocks.items()):
            na, nb = sum(v[0] for v in values), sum(v[1] for v in values)
            outcome = 'win' if nb > na else 'loss' if nb < na else 'tie'
            wins += outcome == 'win'
            losses += outcome == 'loss'
            ties += outcome == 'tie'
            cases.append({'mode': mode, 'case_id': case, 'left_successes': na, 'right_successes': nb,
                'repeats': len(values), 'right_vs_left': outcome})
        comparisons.append({'left': left, 'right': right, 'right_wins': wins, 'ties': ties,
            'right_losses': losses, 'case_blocks': cases,
            'interpretation': 'case_success_count_comparison_no_statistical_significance_claim'})
    return comparisons


def _fixed_controls(records, cases, human=None, *, live_eligible=False):
    rows = []
    for record in records:
        report, entry = record['report'], record['entry']
        if entry['stage'] != 'fixed_review':
            continue
        target = next((c for c in cases[entry['case_id']]['truth'].get('fixed_controls', [])
            if c['control_id'] == entry['control_id']), None)
        require(target is not None, 'FIXED_CONTROL_NOT_FOUND')
        observed = report.get('normalized', {}).get('review_controls', [])
        require(len(observed) <= 1, 'FIXED_CONTROL_MULTIPLE_TARGETS')
        row = observed[0] if observed else {}
        if row:
            require(row.get('original_text_sha256') == hashlib.sha256(target['statement'].encode('utf-8')).hexdigest(), 'FIXED_ORIGINAL_TEXT_BINDING_CHANGED')
        relation = row.get('native_verdict') if row.get('protocol_status') == 'valid' else None
        reference = None
        if human and live_eligible:
            reviewed_truth = human['outcomes']['truth'].get(entry['case_id'], {})
            if reviewed_truth.get('status') in {'human_consensus', 'human_adjudicated'}:
                reference = reviewed_truth['judgment'].get('fixed_controls', {}).get(entry['control_id'])
        # Full strings and file/report binding already live in packets/reports.
        rows.append({'system_id': entry['system_id'], 'case_id': entry['case_id'],
            'control_id': entry['control_id'], 'repeat': entry['repeat'],
            'candidate_expected_relation': target.get('expected_relation'),
            'observed_relation': relation, 'human_reference_relation': reference,
            'human_reference_confirmed': reference is not None,
            'relation_correct': relation == reference if reference is not None else None})
    ready = bool(rows) and all(r['human_reference_confirmed'] for r in rows)
    return {'rows': rows, 'human_reference_confirmed': ready,
        'formal_relation_agreement': (sum(r['relation_correct'] for r in rows) / len(rows)) if ready else None,
        'candidate_expected_labels_are_not_gold': not ready,
        'unassessed_controls': sum(r['observed_relation'] is None for r in rows)}


def build_summary(bundles, dataset, records, sources, comparability, *, human=None,
                  materials=None, rate_card=None, artifact_root=None):
    cases = {case['case_id']: case for case in dataset['cases']}
    planned = sum(len(b['plan'].get('schedule', [])) for b in bundles)
    pending = [row for bundle in bundles for row in bundle.get('scheduled_pending', [])]
    require(planned == len(records) + len(pending), 'PLAN_EXECUTION_COVERAGE_MISMATCH')
    schedule = Counter((row['system_id'], row['case_id'], row['stage'], row['repeat'], row.get('control_id'))
        for bundle in bundles for row in bundle['plan']['schedule'])
    actual = Counter((r['entry']['system_id'], r['entry']['case_id'], r['entry']['stage'],
        r['entry']['repeat'], r['entry'].get('control_id')) for r in records)
    actual.update((r['system_id'], r['case_id'], r['stage'], r['repeat'], r.get('control_id')) for r in pending)
    require(schedule == actual, 'SCHEDULE_OR_PENDING_BINDING_MISMATCH')
    live_eligible = all(b.get('execution_mode') in {'live', 'live_observation_only'} for b in bundles)
    require(all(r['report'].get('execution_mode') == bundles[
        next(i for i, b in enumerate(bundles) if r['entry']['system_id'] == b['plan']['system_id'])].get('execution_mode')
        for r in records), 'RECORD_EXECUTION_MODE_MISMATCH')
    review_ready = False
    human_coverage = {'status': 'pending_human_review', 'formal_ready': False,
        'reports_with_complete_human_evidence': 0, 'reports_with_complete_human_root_match': 0,
        'truths_with_confirmed_provenance': 0, 'ai_judgments_used_as_gold': False}
    if human is not None:
        require(materials is not None, 'REVIEW_MATERIALS_REQUIRED_FOR_REVALIDATION')
        used = {c['case_id'] for c in materials['truth-packets.private.json']['cases']}
        selected_cases = {k: v for k, v in cases.items() if k in used}
        verify_report_bindings(materials, records, selected_cases, bundle_sha256=bundle_binding(sources))
        verified = import_reviews(materials, human.get('review_documents', []),
            allow_single_reviewer=human.get('review_design') == 'single_human_explicitly_disclosed',
            artifact_root=artifact_root)
        require(canonical_sha(verified) == canonical_sha(human), 'JOINED_REVIEW_TAMPERED_OR_STALE')
        review_ready = verified['formal_ready']
        human_coverage = {'status': 'complete' if review_ready else 'pending_or_disputed',
            'formal_ready': review_ready, 'review_design': verified['review_design'],
            'reports_with_complete_human_evidence': sum(r['status'] in {'human_consensus', 'human_adjudicated'} for r in verified['outcomes']['evidence'].values()),
            'reports_with_complete_human_root_match': sum(r['status'] in {'human_consensus', 'human_adjudicated'} for r in verified['outcomes']['root_match'].values()),
            'truths_with_confirmed_provenance': sum(r['status'] in {'human_consensus', 'human_adjudicated'} for r in verified['outcomes']['truth'].values()),
            'disputed_rows': sum(r['status'] == 'disputed' for panel in verified['outcomes'].values() for r in panel.values()),
            'ai_judgments_used_as_gold': False, 'source_join_sha256': canonical_sha(verified)}
    resources, formal_rows, groups = [], [], defaultdict(list)
    link = {r['identity']['report_id']: r for r in materials['linkage.private.json']['reports']} if materials else {}
    for record in records:
        report, entry = record['report'], record['entry']
        plan = next(b['plan'] for b in bundles if b['plan']['system_id'] == entry['system_id'])
        resource = resource_metrics(report, rate_card, limits=plan.get('limits'))
        identity = {k: entry.get(k) for k in ('report_id', 'case_id', 'repeat', 'system_id', 'stage', 'control_id')}
        resources.append({**identity, 'metrics': resource})
        groups[(entry['system_id'], entry['stage'])].append(resource)
        if review_ready and live_eligible and not pending and entry['stage'] != 'fixed_review':
            opaque = link[entry['report_id']]['report_id']
            evidence = human['outcomes']['evidence'][opaque]['judgment']
            root = human['outcomes']['root_match'][opaque]['judgment']
            identifiability = human['outcomes']['truth'][entry['case_id']]['judgment']['identifiability']
            has_output = bool(visible_items(report))
            failed = bool(report.get('execution_error') or report.get('native_report', {}).get('execution_error'))
            references_valid = mechanical_references_valid(report)
            formal_rows.append({'condition': entry['system_id'], 'mode': entry['stage'],
                'case_id': entry['case_id'], 'repeat': entry['repeat'], 'identifiability': identifiability,
                'metrics': score_run(evidence, root, identifiability, has_output=has_output, delivery_failed=failed,
                    mechanical_references_valid=references_valid,
                    established_assertions_supported=root['established_assertions_supported']),
                'provider_failure_present': resource['provider_failures'] > 0})
    from scripts.summarize_frozen_versions import distribution, estimate_cost
    resource_groups = {}
    for (system, stage), rows in groups.items():
        calls = [c for record in records if record['entry']['system_id'] == system and record['entry']['stage'] == stage
            for c in record['report'].get('model_call_log', []) if c.get('charged_request')]
        decisions, repeats = sum(r['collection_decisions'] for r in rows), sum(r['repeated_decisions'] for r in rows)
        resource_groups[system + ':' + stage] = {'runs': len(rows), 'charged_calls': sum(r['charged_calls'] for r in rows),
            'delivery_statuses': dict(Counter(r['delivery_status'] or 'unknown' for r in rows)),
            'known_usage_calls': sum(r['known_usage_calls'] for r in rows), 'unknown_usage_calls': sum(r['unknown_usage_calls'] for r in rows),
            'provider_failures': sum(r['provider_failures'] for r in rows),
            'runs_with_provider_failures': sum(r['provider_failures'] > 0 for r in rows),
            'provider_failure_codes': dict(sum((Counter(r['provider_failure_codes']) for r in rows), Counter())),
            'structured_unrecovered': sum(r['structured_unrecovered'] for r in rows),
            'terminal_structured_interruptions': sum(r['terminal_structured_interruption'] for r in rows),
            'native_model_calls_known': sum(r['native_model_calls'] for r in rows if isinstance(r.get('native_model_calls'), (int, float))),
            'native_model_calls_unassessed_runs': sum(r.get('native_model_calls') is None for r in rows),
            'tool_calls_known': sum(r['tool_calls'] for r in rows if isinstance(r.get('tool_calls'), (int, float))),
            'tool_calls_unassessed_runs': sum(r.get('tool_calls') is None for r in rows),
            'worker_wall_seconds': distribution([r['worker_wall_seconds'] for r in rows if isinstance(r.get('worker_wall_seconds'), (int, float))]),
            'budget_violation_runs': sum(r['hard_budget']['status'] == 'violation' for r in rows),
            'budget_unassessed_runs': sum(r['hard_budget']['status'] == 'unassessed' for r in rows),
            'collection_stop_reasons': dict(sum((Counter(r['collection_stop_reasons']) for r in rows), Counter())),
            'repeated_decisions': repeats, 'collection_decisions': decisions,
            'repeated_fraction': repeats / decisions if decisions else None,
            'duration_ms': distribution([r['duration_ms'] for r in rows if isinstance(r.get('duration_ms'), (int, float))]),
            'cost': estimate_cost(calls, rate_card, pricing_scope=rate_card.get('pricing_scope') if rate_card else None)}
    repeats = {b['plan'].get('repeats') for b in bundles}
    require(len(repeats) == 1 and isinstance(next(iter(repeats)), int), 'REPEAT_CONFIGURATION_MISSING')
    formal_ready = review_ready and not pending and bool(formal_rows)
    scored = aggregate_case_metrics(formal_rows, expected_repeats=next(iter(repeats))) if formal_ready else None
    candidate_counts = Counter(cases[row['case_id']]['truth']['identifiability'] for row in dataset['manifest']['cases'])
    selected_ids = {row['case_id'] for bundle in bundles for row in bundle['plan']['schedule']}
    independent = bool(dataset['manifest'].get('holdout_sealed'))
    return {'schema_version': 'rca-scorecard-v1', 'sources': sources,
        'dataset_fingerprint': dataset['fingerprint'], 'comparability': comparability,
        'execution_coverage': {'planned_runs': planned, 'recorded_runs': len(records),
            'pending_runs': len(pending), 'complete_schedule': not pending, 'pending': pending},
        'candidate_data': {'cases': len(cases), 'proposed_strata': dict(candidate_counts),
            'selected_cases': len(selected_ids),
            'selected_independence_groups': len({cases[key]['independence_group'] for key in selected_ids}),
            'selected_candidate_strata': dict(Counter(cases[key]['truth']['identifiability'] for key in selected_ids)),
            'selected_splits': dict(Counter(cases[key]['split'] for key in selected_ids)),
            'candidate_labels_are_not_gold': True, 'holdout_sealed': independent,
            'origin': next(iter({c['truth']['origin'] for c in cases.values()}))
                if len({c['truth']['origin'] for c in cases.values()}) == 1 else 'mixed',
            'origins': dict(Counter(c['truth']['origin'] for c in cases.values())),
            'machine_experiment_cases': sum(c['truth']['origin'] == 'executed_synthetic_experiment' for c in cases.values()),
            'machine_execution_is_human_gold': False},
        'human_review': human_coverage, 'formal_metrics': scored,
        'human_verified': review_ready, 'is_gold': False,
        'formal_status': 'scripted_wiring_only_no_quality_score' if not live_eligible else 'human_reviewed_candidate_descriptive_only' if formal_ready else 'pending_human_review_or_execution',
        'independent_holdout_acceptance': None, 'production_accuracy': None,
        'independent_holdout_evaluated': False, 'native_supported_is_gold': False,
        'completion_is_accuracy': False, 'formal_run_scores': formal_rows if formal_ready else None,
        'paired_case_results': _paired_results(formal_rows) if formal_ready else None,
        'paired_coverage': {'systems': comparability['systems'], 'schedule_verified': True,
            'formal_comparison_pending': not formal_ready, 'no_significance_claim': True},
        'fixed_review_controls': _fixed_controls(records, cases, human, live_eligible=live_eligible and review_ready),
        'resource_groups': resource_groups, 'resource_runs': resources,
        'limits': ['No human gold is manufactured by this utility.',
            'Candidate-set scores do not establish independent holdout or production reliability.',
            'Repeated runs and related variants are correlated.',
            'Unknown provider usage is retained and is never priced as zero.',
            'Unresolved human disagreements block formal metrics; human adjudication is required.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', action='append', required=True)
    parser.add_argument('--dataset')
    parser.add_argument('--human-review')
    parser.add_argument('--review-dir')
    parser.add_argument('--artifact-root')
    parser.add_argument('--rate-card')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    bundles, dataset, records, sources, comparison = load_bundles(args.bundle, args.dataset)
    summary = build_summary(bundles, dataset, records, sources, comparison,
        human=read_json(args.human_review) if args.human_review else None,
        materials=load_materials(args.review_dir) if args.review_dir else None,
        rate_card=read_json(args.rate_card) if args.rate_card else None, artifact_root=args.artifact_root)
    write_json(args.output, summary)
    coverage = summary['execution_coverage']
    print(f"Recorded {coverage['recorded_runs']}/{coverage['planned_runs']} planned runs; "
        f"formal_status={summary['formal_status']}; human-gold accuracy="
        + ('available on candidate cases only' if summary['formal_metrics'] is not None else 'pending'))


if __name__ == '__main__':
    main()
