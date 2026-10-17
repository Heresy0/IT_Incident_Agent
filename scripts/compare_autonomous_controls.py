"""Compare frozen control artifacts without making model or network requests.

Completion-contract reprojection changes only the interpretation of an existing
report. It must never be counted as new evidence or improved causal accuracy.
"""
import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from evals.closed_loop_cases import (PRINCIPAL, SourceWorldProvider, acquired_key_observations,
                                     build_cases, reference_targets)
from runtime.context import Limits, RunContext
from tools.executor import ToolExecutor
from workflow.v2.contracts import Draft, source_facts
from workflow.v2.outcome import classify_outcome
from workflow.v2.review import ReviewBatch, evidence_pack_version

STAGES = ('e2e', 'fixed_rework', 'review')
SOURCE_FILES = {'review_source_sha256': 'app/workflow/v2/review.py',
                'challenge_source_sha256': 'app/workflow/v2/challenge.py'}
APPLICATION_VARIANTS = {'source_version', 'engine_version', 'engine_source_sha256'}


class ComparabilityError(ValueError):
    """Artifacts cannot support the requested controlled comparison."""


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(condition, code):
    if not condition:
        raise ComparabilityError(code)


def _normalized(path):
    return Path(path).read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')


def _same_source(a, b, relative, baseline_source, candidate_source, notes):
    if a == b:
        return
    left, right = Path(baseline_source) / relative, Path(candidate_source) / relative
    _require(left.is_file() and right.is_file(), 'SOURCE_HASH_DIFFERENCE_WITHOUT_SOURCE:' + relative)
    _require(_sha(left) == a and _sha(right) == b, 'SOURCE_FILE_HASH_MISMATCH:' + relative)
    _require(_normalized(left) == _normalized(right), 'CONTROL_SOURCE_CHANGED:' + relative)
    notes.append({'path': relative, 'difference': 'line_endings_only', 'baseline_sha256': a,
                  'candidate_sha256': b})


def load_bundle(path):
    path = Path(path)
    bundle = _json(path)
    plan = bundle['plan']
    reports = {}
    for row in bundle['runs']:
        key = (row['case_id'], row['arm'], row['repeat'])
        _require(key not in reports, 'DUPLICATE_RUN')
        target = (path.parent / row['report_file']).resolve()
        _require(target.parent == path.parent.resolve(), 'REPORT_PATH_OUTSIDE_BUNDLE')
        _require(target.is_file() and _sha(target) == row['report_sha256'], 'REPORT_HASH_MISMATCH')
        report = _json(target)
        _require((report.get('case_id'), report.get('arm'), report.get('repeat')) == key,
                 'REPORT_IDENTITY_MISMATCH')
        _require(report.get('application') == plan['application'], 'REPORT_APPLICATION_MISMATCH')
        _require(report.get('execution_mode') == plan['execution_mode'], 'REPORT_EXECUTION_MODE_MISMATCH')
        _require(report.get('run_summary', {}).get('limits') == plan['limits'], 'REPORT_LIMITS_MISMATCH')
        freeze = next((item for item in plan['case_freezes'] if item['case_id'] == key[0]), None)
        _require(freeze is not None and report.get('synthetic_reference') == freeze, 'REPORT_WORLD_MISMATCH')
        reports[key] = {'entry': row, 'report': report, 'path': str(target)}
    expected = {(case, arm, repeat) for case in plan['cases'] for arm in plan['arms']
                for repeat in range(1, plan['repeats'] + 1)}
    _require(set(reports) == expected and len(reports) == plan['planned_runs'], 'INCOMPLETE_RUN_SET')
    return {'path': str(path.resolve()), 'sha256': _sha(path), 'plan': plan, 'reports': reports}


def validate_comparability(baseline, candidate, *, baseline_source, candidate_source=ROOT,
                          comparison_plan=None):
    a, b = baseline['plan'], candidate['plan']
    notes = []
    _require(a.get('execution_mode') == b.get('execution_mode') == 'live', 'LIVE_ARTIFACTS_REQUIRED')
    for key in ('version', 'stage', 'cases', 'arms', 'repeats', 'planned_runs', 'limits',
                'model_settings', 'configuration_sha256', 'dataset_version', 'case_freezes'):
        _require(a.get(key) == b.get(key), 'PLAN_MISMATCH:' + key)
    _require(bool(a.get('configuration_sha256')), 'MISSING_MODEL_CONFIGURATION_HASH')
    _require(set(baseline['reports']) == set(candidate['reports']), 'PAIR_SET_MISMATCH')
    for key in set(a['application']) | set(b['application']):
        if key in APPLICATION_VARIANTS:
            continue
        if key in SOURCE_FILES:
            _same_source(a['application'].get(key), b['application'].get(key), SOURCE_FILES[key],
                         baseline_source, candidate_source, notes)
        else:
            _require(a['application'].get(key) == b['application'].get(key), 'POLICY_MISMATCH:' + key)
    _require(set(a['evaluation_source_sha256']) == set(b['evaluation_source_sha256']), 'EVALUATOR_SET_CHANGED')
    for path in a['evaluation_source_sha256']:
        _same_source(a['evaluation_source_sha256'][path], b['evaluation_source_sha256'][path], path,
                     baseline_source, candidate_source, notes)
    if comparison_plan:
        stage = comparison_plan['stages'][a['stage']]
        _require(a['model_settings']['model'] == comparison_plan['model'], 'PRESPECIFIED_MODEL_MISMATCH')
        for key, value in comparison_plan['model_settings'].items():
            _require(a['model_settings'].get(key) == value, 'PRESPECIFIED_MODEL_SETTINGS_MISMATCH:' + key)
        for key, value in comparison_plan['per_run_limits'].items():
            _require(a['limits'].get(key) == value, 'PRESPECIFIED_LIMIT_MISMATCH:' + key)
        _require(a['application']['source_version'] == comparison_plan['baseline_application_version'],
                 'PRESPECIFIED_BASELINE_MISMATCH')
        expected_cases = list(build_cases()) if isinstance(stage['cases'], str) else stage['cases']
        expected_arm = stage.get('arm', 'closed_loop')
        _require(a['cases'] == expected_cases and a['repeats'] == stage['repeats']
                 and a['arms'] == [expected_arm], 'PRESPECIFIED_RUN_SET_MISMATCH')
    return notes


def _collection(rows):
    return [{'tool': row.get('tool', row.get('name')), 'args': row.get('args', row.get('query_profile')),
             'status': row['status'], 'truncated': bool(row['truncated']), 'error': row['error'],
             'evidence_ids': row['evidence_ids']} for row in rows]


def _compact_collection(executor):
    return [{'name': row['tool'], 'query_profile': row['args'], 'status': row['status'],
             'truncated': row['truncated'], 'error': row['error'], 'evidence_ids': row['evidence_ids']}
            for row in executor.check_results.values()]


def reproject_report(report, case, *, expected_freeze=None):
    """Replay only recorded synthetic reads, then apply the new pure projection.

    No model is built. Any ledger, evidence, source-fact or pack inconsistency
    makes this unavailable instead of manufacturing a completion gain.
    """
    try:
        freeze = expected_freeze or report.get('synthetic_reference')
        _require(freeze == case.freeze(), 'REPLAY_CASE_FREEZE_MISMATCH')
        limits = Limits(**report['run_summary']['limits'])
        provider = SourceWorldProvider(case)
        context = RunContext(scope=provider.ticket().scope, limits=limits)
        # Avoid telemetry and event writers while retaining the real executor's
        # authorization, argument normalization, references and query behavior.
        context.emit = lambda event: None
        executor = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
        ledger = _collection(report['collection'])
        for row in ledger:
            executor.execute(row['tool'], row['args'])
        _require(_collection(executor.check_results.values()) == ledger, 'REPLAY_COLLECTION_MISMATCH')
        old_evidence = {item['evidence_id']: (item['hash'], item['payload'], item['locator'])
                        for item in report['evidence']}
        new_evidence = {item.evidence_id: (item.hash, item.payload, item.locator)
                        for item in executor.evidence.values()}
        _require(old_evidence == new_evidence, 'REPLAY_EVIDENCE_MISMATCH')
        draft = Draft.model_validate_json(json.dumps(report['drafts'][-1]['draft']))
        _require([item.model_dump(mode='json') for item in draft.facts]
                 == [item.model_dump(mode='json') for item in source_facts(executor)], 'REPLAY_FACTS_MISMATCH')
        batch_data = deepcopy(report['reviews'][-1]['review'])
        batch_data['items'] = report['item_reviews']
        reviews = ReviewBatch.model_validate_json(json.dumps(batch_data))
        compact = _compact_collection(executor)
        _require(reviews.evidence_pack_version in {evidence_pack_version(executor),
                 evidence_pack_version(executor, collection=compact)}, 'REPLAY_REVIEW_PACK_MISMATCH')
        phases = [event for event in report.get('events', []) if event.get('type') == 'collection_completed'
                  and event.get('phase', 'collect') == 'collect']
        stop = (phases[0]['reason'] if phases else report.get('run_outcome', {}).get('execution', {}).get('collection_stop'))
        _require(bool(stop), 'REPLAY_COLLECTION_STOP_MISSING')
        result = classify_outcome(draft, reviews, executor, report.get('protocol_failures', []), stop,
                                  provider.ticket(), collection=compact)
        changed = result['status'] != report.get('status')
        advice_gate = changed and report.get('status') == 'partial' and result['status'] == 'completed' and not draft.next_steps
        return {'status': 'available', 'original_status': report.get('status'),
                'projected_status': result['status'], 'projected_reason': result['reason'],
                'status_changed': changed, 'empty_advice_gate_completion_gain': advice_gate,
                'new_observations': 0, 'paid_requests': 0, 'causal_accuracy_gain': None,
                'run_outcome': result['run_outcome'],
                'note': 'Same recorded trajectory; contract-only reprojection is not new investigation or semantic acceptance.'}
    except Exception as exc:
        # Never echo source/model content or exception messages into the summary.
        return {'status': 'not_available', 'reason': str(exc) if isinstance(exc, ComparabilityError) else type(exc).__name__,
                'original_status': report.get('status'), 'projected_status': None,
                'new_observations': 0, 'paid_requests': 0, 'causal_accuracy_gain': None}


def run_metrics(report, case, stage):
    summary, events = report['run_summary'], report.get('events', [])
    limits = summary['limits']
    violations = []
    for key in ('model_calls', 'tool_calls', 'web_calls'):
        if summary.get(key, 0) > limits[key]:
            violations.append(key)
    if summary.get('duration_ms', 0) > limits['seconds'] * 1000:
        violations.append('seconds')
    controls = []
    if stage == 'review':
        from scripts.evaluate_autonomous_closed_loop import score_review
        references = reference_targets(case)
        _require(report.get('provisional_reference') == references, 'REVIEW_REFERENCE_MISMATCH')
        controls = score_review({**report, 'provisional_reference': references})
    keys = acquired_key_observations(case, report) if stage != 'review' else None
    first_ids = {eid for row in report.get('first_pass', {}).get('collection', []) for eid in row.get('evidence_ids', [])}
    after_ids = {item.get('locator', '').rsplit('/', 1)[-1] for item in report.get('evidence', [])
                 if item['evidence_id'] not in first_ids}
    return {'completed': report.get('status') == 'completed',
            'key_observations': keys,
            'key_observations_acquired_after_review': sorted(set(case.key_observation_ids) & after_ids)
                if stage != 'review' and report.get('first_pass') else [],
            'model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
            'duration_ms': summary.get('duration_ms'), 'review_controls': controls,
            'execution_error': bool(report.get('execution_error')),
            'unrecovered_failures': sum(not row.get('recovered') for row in report.get('protocol_failures', [])),
            'hard_limit_violations': violations,
            'budget_rejections': sum(event.get('type') == 'budget_rejected' for event in events)}


def _summary(rows):
    controls = [control for row in rows for control in row['review_controls']]
    assessed = [row for row in controls if row['protocol_status'] == 'valid']
    positives = [row for row in assessed if row['expected'] in {'appropriate', 'supported'}]
    negatives = [row for row in assessed if row['expected'] not in {'appropriate', 'supported'}]
    keys = [row['key_observations'] for row in rows if row['key_observations'] is not None]
    resources = {}
    for key in ('model_calls', 'tool_calls', 'duration_ms'):
        values = [row[key] for row in rows if isinstance(row[key], (int, float))]
        resources[key] = {'known_runs': len(values), 'sum': sum(values), 'mean': sum(values) / len(values) if values else None}
    return {'runs': len(rows), 'completed': sum(row['completed'] for row in rows),
            'key_observations_required': sum(row['required_count'] for row in keys),
            'key_observations_acquired': sum(row['acquired_count'] for row in keys),
            'key_observations_omitted': sum(len(row['omitted_ids']) for row in keys),
            'key_observations_acquired_after_review': sum(len(row['key_observations_acquired_after_review']) for row in rows),
            'resources': resources, 'provisional_false_accepts': sum(row['false_accept'] for row in negatives),
            'negative_reference_items': len(negatives),
            'provisional_false_rejects': sum(row['false_reject'] for row in positives),
            'positive_reference_items': len(positives), 'reference_items': len(controls),
            'unassessed_reference_items': len(controls) - len(assessed),
            'provisional_exact_agreement': sum(row['exact_agreement'] for row in assessed),
            'execution_error_runs': sum(row['execution_error'] for row in rows),
            'unrecovered_failure_runs': sum(row['unrecovered_failures'] > 0 for row in rows),
            'hard_limit_violation_runs': sum(bool(row['hard_limit_violations']) for row in rows),
            'budget_rejections': sum(row['budget_rejections'] for row in rows)}


def compare_stage(baseline_path, candidate_path, *, comparison_plan=None, baseline_source=None,
                  candidate_source=ROOT, reproject=True):
    baseline, candidate = load_bundle(baseline_path), load_bundle(candidate_path)
    baseline_source = baseline_source or ROOT / 'output' / 'autonomy-controls' / 'baseline-v2.3'
    notes = validate_comparability(baseline, candidate, baseline_source=baseline_source,
                                   candidate_source=candidate_source, comparison_plan=comparison_plan)
    stage, cases = baseline['plan']['stage'], build_cases()
    rows, left, right, projections = [], [], [], []
    for key in sorted(baseline['reports']):
        a, b = baseline['reports'][key], candidate['reports'][key]
        _require(cases[key[0]].freeze() == a['report']['synthetic_reference'], 'LOCAL_CASE_WORLD_CHANGED')
        ma, mb = run_metrics(a['report'], cases[key[0]], stage), run_metrics(b['report'], cases[key[0]], stage)
        left.append(ma)
        right.append(mb)
        projection = reproject_report(a['report'], cases[key[0]]) if stage == 'e2e' and reproject else None
        if projection is not None:
            projections.append(projection)
        rows.append({'case_id': key[0], 'arm': key[1], 'repeat': key[2],
                     'baseline_report': a['path'], 'candidate_report': b['path'],
                     'baseline': ma, 'candidate': mb,
                     'resource_delta_candidate_minus_baseline': {field: mb[field] - ma[field]
                         if isinstance(ma[field], (int, float)) and isinstance(mb[field], (int, float)) else None
                         for field in ('model_calls', 'tool_calls', 'duration_ms')},
                     'baseline_contract_reprojection': projection})
    available = [row for row in projections if row['status'] == 'available']
    return {'stage': stage, 'comparability': 'passed', 'normalization_notes': notes,
            'baseline_bundle': {'path': baseline['path'], 'sha256': baseline['sha256'], 'application': baseline['plan']['application']},
            'candidate_bundle': {'path': candidate['path'], 'sha256': candidate['sha256'], 'application': candidate['plan']['application']},
            'model_settings': baseline['plan']['model_settings'],
            'configuration_sha256': baseline['plan']['configuration_sha256'], 'limits': baseline['plan']['limits'],
            'baseline': _summary(left), 'candidate': _summary(right), 'pairs': rows,
            'contract_reprojection': {'requested': bool(stage == 'e2e' and reproject),
                'available_runs': len(available), 'not_available_runs': len(projections) - len(available),
                'original_completed_among_available': sum(row['original_status'] == 'completed' for row in available),
                'projected_completed_among_available': sum(row['projected_status'] == 'completed' for row in available),
                'empty_advice_gate_completion_gains': sum(row['empty_advice_gate_completion_gain'] for row in available),
                'new_observations': 0, 'paid_requests': 0, 'causal_accuracy_gain': None},
            'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold',
            'acquisition_is_not_causal_success': True}


def _one_bundle(directory):
    candidates = sorted(Path(directory).glob('*/bundle.json'))
    if (Path(directory) / 'bundle.json').is_file():
        candidates.append(Path(directory) / 'bundle.json')
    _require(len(candidates) == 1, 'EXPECTED_ONE_BUNDLE:' + Path(directory).name)
    return candidates[0]


def compare_root(root):
    root = Path(root)
    plan = _json(root / 'comparison-plan.json')
    results = {}
    aliases = {'e2e': 'e2e', 'fixed_rework': 'fixed', 'review': 'review'}
    for stage, name in aliases.items():
        try:
            results[stage] = compare_stage(_one_bundle(root / ('baseline-' + name)),
                _one_bundle(root / ('candidate-' + name)), comparison_plan=plan,
                baseline_source=root / 'baseline-v2.3')
        except (ComparabilityError, KeyError, OSError, ValueError) as exc:
            results[stage] = {'stage': stage, 'comparability': 'rejected',
                'reason': str(exc) if isinstance(exc, ComparabilityError) else type(exc).__name__}
    comparable = all(row['comparability'] == 'passed' for row in results.values())
    cross_stage_errors = []
    if comparable:
        for arm in ('baseline', 'candidate'):
            applications = [results[stage][arm + '_bundle']['application'] for stage in STAGES]
            if any(application != applications[0] for application in applications[1:]):
                cross_stage_errors.append(arm.upper() + '_APPLICATION_CHANGED_ACROSS_STAGES')
        configs = [results[stage]['model_settings'] for stage in STAGES]
        if any(config != configs[0] for config in configs[1:]):
            cross_stage_errors.append('MODEL_SETTINGS_CHANGED_ACROSS_STAGES')
        hashes = [results[stage]['configuration_sha256'] for stage in STAGES]
        if any(value != hashes[0] for value in hashes[1:]):
            cross_stage_errors.append('MODEL_CONFIGURATION_CHANGED_ACROSS_STAGES')
        limits = [results[stage]['limits'] for stage in STAGES]
        if any(limit != limits[0] for limit in limits[1:]):
            cross_stage_errors.append('LIMITS_CHANGED_ACROSS_STAGES')
        comparable = not cross_stage_errors
    conditions = {}
    if comparable:
        for stage, row in results.items():
            a, b = row['baseline'], row['candidate']
            conditions[stage + '_no_hard_cap_violation'] = b['hard_limit_violation_runs'] == 0
            conditions[stage + '_no_execution_error'] = b['execution_error_runs'] == 0
        a, b = results['fixed_rework']['baseline'], results['fixed_rework']['candidate']
        conditions['fixed_acquisition_improved'] = b['key_observations_acquired'] > a['key_observations_acquired']
        a, b = results['e2e']['baseline'], results['e2e']['candidate']
        conditions['e2e_no_additional_omissions'] = b['key_observations_omitted'] <= a['key_observations_omitted']
        conditions['e2e_no_fewer_completed'] = b['completed'] >= a['completed']
        projection = results['e2e']['contract_reprojection']
        conditions['e2e_no_fewer_completed_same_contract'] = (
            projection['available_runs'] == a['runs']
            and b['completed'] >= projection['projected_completed_among_available'])
        a, b = results['review']['baseline'], results['review']['candidate']
        conditions['review_false_accept_no_increase'] = b['provisional_false_accepts'] <= a['provisional_false_accepts']
        conditions['review_all_controls_assessed'] = b['unassessed_reference_items'] == 0
    return {'schema_version': 'autonomous-controls-comparison-v1', 'paid_requests_sent': 0,
            'comparison_plan_sha256': _sha(root / 'comparison-plan.json'),
            'comparability': 'passed' if comparable else 'rejected', 'stages': results,
            'cross_stage_errors': cross_stage_errors,
            'synthetic_acceptance': {'status': 'passed' if conditions and all(conditions.values()) else 'not_met',
                                     'conditions': conditions},
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold',
            'limitations': ['Source-constructed controls and small repeated samples are not independent human gold.',
                'Collected records, model-reviewed claims and contract completion are separate outcomes.',
                'Contract-only reprojection neither changes the trajectory nor demonstrates causal improvement.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT / 'output' / 'autonomy-controls')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args(argv)
    if args.plan or args.output is None:
        print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, 'root': str(args.root),
            'stages': list(STAGES), 'execution': 'Pass --output PATH to validate and compare existing artifacts.',
            'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'}, ensure_ascii=False, indent=2))
        return 0
    result = compare_root(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'comparability': result['comparability'],
        'synthetic_acceptance': result['synthetic_acceptance'], 'paid_requests_sent': 0,
        'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'}, ensure_ascii=False))
    return 0 if result['comparability'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
