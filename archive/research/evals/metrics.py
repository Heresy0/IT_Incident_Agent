"""Aggregate actual measured samples; pending semantic review never counts as a pass."""
from collections import Counter
from statistics import mean
import math


def average(values):
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    left = int(position)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (position - left)


def summarize(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r['case']['id'], []).append(r)
    reviewed = [r for r in rows if r['grading']['semantic_status'] == 'reviewed']
    pairs = [g for g in groups.values() if len(g) >= 2]
    reviewed_pairs = [g for g in pairs if all(r['grading']['semantic_status'] == 'reviewed' for r in g)]
    citations = sum(r['grading']['citation_count'] for r in rows)
    valid_citations = sum(r['grading']['valid_citation_count'] for r in rows)
    durations = [r['summary']['duration_ms'] for r in rows]
    return {'run_count': len(rows), 'case_count': len(groups),
            'automated_pass_rate': average([int(r['grading']['automated_pass']) for r in rows]),
            'reviewed_count': len(reviewed), 'review_completion_rate': len(reviewed) / len(rows) if rows else None,
            'reviewer_kinds': dict(Counter(r['grading']['reviewer']['kind'] for r in reviewed)),
            'semantic_pass_rate': average([int(r['grading']['semantic_pass']) for r in reviewed]),
            'requirement_coverage': average([r['grading']['requirement_coverage'] for r in reviewed]),
            'claim_support_rate': average([r['grading']['claim_support_rate'] for r in reviewed]),
            'citation_id_validity': valid_citations / citations if citations else None,
            'execution_failure_rate': average([int(r['status'] == 'failed') for r in rows]),
            'run_error_rate': average([int(bool(r['summary'].get('errors') or r.get('evaluation_error_type'))) for r in rows]),
            'error_counts': dict(Counter(e['code'] for r in rows for e in r['summary'].get('errors', []))),
            'status_counts': dict(Counter(r['status'] for r in rows)),
            'duration_ms': {'mean': average(durations), 'p50': percentile(durations, .5), 'p95': percentile(durations, .95),
                            'max': max(durations) if durations else None},
            'mean_model_calls': average([r['summary']['model_calls'] for r in rows]),
            'known_input_tokens': sum(r['summary'].get('token_usage', {}).get('input_tokens', 0) for r in rows),
            'known_output_tokens': sum(r['summary'].get('token_usage', {}).get('output_tokens', 0) for r in rows),
            'partial_token_usage_runs': sum(r['summary'].get('token_usage', {}).get('status') != 'known' for r in rows),
            'repeat_status_agreement': average([int(len({r['status'] for r in g}) == 1) for g in pairs]),
            'repeat_quality_agreement': average([int(len({r['grading']['semantic_pass'] for r in g}) == 1) for g in reviewed_pairs]),
            'all_repeats_pass_rate': average([int(all(r['grading']['semantic_pass'] for r in g)) for g in reviewed_pairs]),
            'termination_reasons': dict(Counter(r.get('workflow_termination_reason') or r['summary'].get('termination_reason')
                                                or ('none' if 'workflow_termination_reason' in r else 'none_or_unrecorded') for r in rows))}


DEFAULT_GATES = {'min_automated_pass_rate': 1.0, 'min_review_completion_rate': 1.0,
                 'min_semantic_pass_rate': .85, 'min_requirement_coverage': .90,
                 'max_semantic_pass_drop': .05, 'max_mean_duration_increase': .25}


def gate(metrics, baseline=None, policy=None):
    if policy and (set(policy) - set(DEFAULT_GATES) or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in policy.values())):
        raise ValueError('Unknown gate or threshold outside 0..1')
    policy = {**DEFAULT_GATES, **(policy or {})}
    failures, pending = [], []
    for metric, key in [('automated_pass_rate', 'min_automated_pass_rate'),
                        ('semantic_pass_rate', 'min_semantic_pass_rate'), ('requirement_coverage', 'min_requirement_coverage')]:
        value = metrics[metric]
        if value is None:
            pending.append(metric)
        elif value < policy[key]:
            failures.append(f'{metric}={value:.3f} < {policy[key]:.3f}')
    if (metrics['review_completion_rate'] or 0) < policy['min_review_completion_rate']:
        pending.append('review_completion_rate')
    if baseline:
        for key in ('semantic_pass_rate',):
            if metrics[key] is not None and baseline[key] is not None and baseline[key] - metrics[key] > policy['max_semantic_pass_drop']:
                failures.append('semantic pass rate regressed beyond tolerance')
        old = baseline['duration_ms']['mean']
        new = metrics['duration_ms']['mean']
        if old and new is not None and new > old * (1 + policy['max_mean_duration_increase']):
            failures.append('mean duration regressed beyond tolerance')
    return {'decision': 'failed' if failures else 'needs_review' if pending else 'passed',
            'failures': failures, 'pending': sorted(set(pending)), 'policy': policy}


def compare(current, baseline):
    for key in ('suite', 'dataset_version', 'fixture_version', 'scorer_version', 'selected_cases', 'repeats'):
        if current.get(key) != baseline.get(key):
            raise ValueError('Baseline is not comparable: ' + key)
    if not current['complete'] or not baseline['complete']:
        raise ValueError('Incomplete evaluations cannot be compared as complete baselines')
    if {(r['case']['id'], r['attempt']) for r in current['results']} != {(r['case']['id'], r['attempt']) for r in baseline['results']}:
        raise ValueError('Baseline has a different set of case/repeat samples')
