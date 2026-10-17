"""Source-constructed review controls and same-budget autonomous rework ablations.

Default execution only freezes a free plan. Synthetic references are provisional;
they never create human approval or establish production diagnostic accuracy.
"""
import argparse
from dataclasses import asdict, replace
import hashlib
import inspect
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from config.incident import IncidentConfig
from evals.closed_loop_cases import (VERSION, PRINCIPAL, SourceWorldProvider, build_cases,
                                     fixed_draft, acquired_key_observations, fingerprint, reference_targets)
from evals.common import write_json
from runtime.context import Limits, RunContext
from runtime.version import implementation_version
from scripts.evaluate_autonomous import (BudgetedModel, GlobalPaidBudget, MODEL_SETTINGS,
                                         jsonable, safe_exception, schedule)


def limits_for(args):
    return replace(Limits.from_env(), model_calls=args.model_budget, tool_calls=args.tool_budget,
                   seconds=args.seconds, reserve_model_calls=2, reserve_seconds=min(15, args.seconds / 4))


def versions():
    from workflow.v2 import engine, review, contracts, challenge
    return {'source_version': implementation_version(), 'engine_version': engine.ENGINE_VERSION,
            'review_policy_version': review.REVIEW_POLICY_VERSION,
            'contract_version': contracts.CONTRACT_VERSION,
            'challenge_policy_version': challenge.CHALLENGE_POLICY_VERSION,
            'challenge_prompt_sha256': hashlib.sha256(challenge.CHALLENGE_PROMPT.encode()).hexdigest(),
            'review_prompt_sha256': hashlib.sha256(review.REVIEW_PROMPT.encode()).hexdigest(),
            'engine_source_sha256': hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest(),
            'review_source_sha256': hashlib.sha256(Path(review.__file__).read_bytes()).hexdigest(),
            'challenge_source_sha256': hashlib.sha256(Path(challenge.__file__).read_bytes()).hexdigest()}


def plan_for(args, config, cases):
    scheduled = len(args.cases) * len(args.arms) * args.repeats
    return {'version': 'closed-loop-evaluation-v1', 'stage': args.stage,
            'execution_mode': 'live' if args.live else 'scripted_control_only' if args.scripted else 'plan_only',
            'comparison': {'review': 'primary versus primary_plus_blind_scope_challenge',
                'fixed_rework': 'same_fixed_prefix_primary_and_challenge_then_rework_on_or_off',
                'e2e': 'single_pass versus combined_challenge_and_rework'}[args.stage],
            'not_original_v22_comparison': True, 'cases': args.cases, 'arms': args.arms,
            'repeats': args.repeats, 'planned_runs': scheduled, 'limits': asdict(limits_for(args)),
            'maximum_model_calls': min(scheduled * (3 if args.stage == 'review' else args.model_budget), args.max_model_calls)
                if args.max_model_calls else scheduled * (3 if args.stage == 'review' else args.model_budget),
            'model_settings': {**MODEL_SETTINGS, 'model': config.model},
            'configuration_sha256': fingerprint({'model': config.model, 'base_url': config.base_url,
                                                 'extra_body': config.extra_body}),
            'application': versions(), 'dataset_version': VERSION,
            'case_freezes': [cases[key].freeze() for key in args.cases],
            'evaluation_source_sha256': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                for path in ('scripts/evaluate_autonomous_closed_loop.py', 'evals/closed_loop_cases.py')},
            'reference_kind': 'synthetic_source_constructed_provisional',
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'authoring': 'Developer-authored new synthetic worlds plus explicitly tagged known regressions; not independent external blind testing.',
            'read_only': True, 'estimated_cost': None}


def context_for(provider, limits):
    events = []
    context = RunContext(scope=provider.ticket().scope, limits=limits,
                         emit=lambda event: events.append({**event, 'seq': len(events) + 1}))
    return context, events


def run_review(case, model, limits, *, arm='review'):
    from tools.executor import ToolExecutor
    from workflow.v2.contracts import parse_draft
    from workflow.v2.review import review_draft
    from workflow.v2.challenge import challenge_supported
    provider = SourceWorldProvider(case)
    context, events = context_for(provider, limits)
    executor = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
    try:
        for check in case.initial_checks:
            executor.execute(check['tool'], check['args'])
        raw, expected = fixed_draft(case, executor)
        draft, errors = parse_draft(raw, executor)
        if errors:
            raise ValueError('Source-constructed fixed draft was invalid')
        batch = review_draft(model, draft, executor, context, allow_correction=True)
        primary = jsonable(batch)
        challenge_audit = None
        if arm == 'challenged_review':
            batch, challenge_audit = challenge_supported(model, draft, batch, executor, context,
                                                        collection=list(executor.check_results.values()))
        report = {'fixed_draft': raw, 'item_reviews': jsonable(batch)['items'],
                  'review_batch': jsonable(batch), 'primary_batch': primary,
                  'challenge_audit': challenge_audit, 'provisional_reference': expected,
                  'challenge_is_ground_truth': False}
    except Exception as exc:
        report = {'execution_error': safe_exception(exc), 'item_reviews': [],
                  'provisional_reference': reference_targets(case)}
    report.update(evidence=[item.model_dump(mode='json') for item in executor.evidence.values()],
                  collection=list(executor.check_results.values()), run_summary=context.summary(), events=events)
    return report


def run_fixed_rework(case, arm, model, limits):
    """Identical fixed prefix; source labels only apply to original fixed targets."""
    from tools.executor import ToolExecutor
    from workflow.v2.contracts import parse_draft
    from workflow.v2.review import review_draft
    from workflow.v2.challenge import challenge_supported
    from workflow.v2.engine import execute_review_rework, publish, collection_view
    provider = SourceWorldProvider(case)
    context, events = context_for(provider, limits)
    executor = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
    repairs = {'repairs': 0, 'reviewer_repairs': 0}
    try:
        for check in case.initial_checks:
            executor.execute(check['tool'], check['args'])
        raw, expected = fixed_draft(case, executor)
        draft, errors = parse_draft(raw, executor)
        if errors:
            raise ValueError('Fixed-prefix draft invalid')
        batch = review_draft(model, draft, executor, context, repairs=repairs,
                             collection=collection_view(executor))
        primary = jsonable(batch)
        batch, initial_challenge = challenge_supported(model, draft, batch, executor, context,
                                                       collection=collection_view(executor))
        first = {'draft': jsonable(draft), 'review': jsonable(batch), 'primary_batch': primary,
                 'collection': jsonable(list(executor.check_results.values())),
                 'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
                 'run_summary': context.summary()}
        audit = {'performed': False, 'reason': 'EVALUATION_REWORK_DISABLED'}
        if arm == 'closed_loop':
            draft, batch, audit = execute_review_rework(model, draft, batch, executor, context,
                                                       repairs=repairs, enable_challenge=True)
        report = publish(draft, batch, executor, context, audit.get('failures', []), 'FINISHED',
                         events, provider.ticket(), audit.get('decisions', 0), repairs)
        report.update(first_pass=first, reworks=[audit], rework_rounds=int(bool(audit.get('performed'))),
            primary_batch=primary, challenges=[initial_challenge, *audit.get('challenges', [])],
            final_draft=jsonable(draft), final_review=jsonable(batch),
            fixed_prefix={'draft': raw, 'source_checks': jsonable(case.initial_checks),
                          'is_fully_autonomous_e2e': False},
            first_pass_provisional_reference=expected,
            revised_claim_accuracy='pending_human_review; original labels are not reused')
    except Exception as exc:
        report = {'execution_error': safe_exception(exc), 'status': 'failed',
                  'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
                  'run_summary': context.summary(), 'events': events}
    return report


def run_e2e(case, arm, model, limits):
    from workflow.v2.engine import investigate
    if 'enable_rework' not in inspect.signature(investigate).parameters:
        raise ValueError('Engine does not yet support explicit rework ablation')
    provider = SourceWorldProvider(case)
    context, events = context_for(provider, limits)
    try:
        report = investigate(model, provider, PRINCIPAL, context=context, event_log=events,
                             enable_rework=arm == 'closed_loop')
    except Exception as exc:
        report = {'execution_error': safe_exception(exc), 'status': 'failed', 'evidence': [],
                  'run_summary': context.summary(), 'events': events}
    return report


def observed_judgment(item, kind):
    if kind == 'check_appropriateness':
        return item.get('submitted_decision')
    return item.get('relation')


def score_review(report):
    items = {item['target_id']: item for item in report.get('item_reviews', [])}
    results = []
    for target, reference in report.get('provisional_reference', {}).items():
        item = items.get(target, {})
        valid = item.get('protocol_status') == 'valid'
        observed = observed_judgment(item, reference['kind']) if valid else None
        positive = 'appropriate' if reference['kind'] == 'check_appropriateness' else 'supported'
        expected = reference['value']
        results.append({'target_id': target, 'kind': reference['kind'], 'expected': expected,
            'observed': observed, 'protocol_status': item.get('protocol_status', 'not_run'),
            'false_accept': valid and expected != positive and observed == positive,
            'false_reject': valid and expected == positive and observed != positive,
            'exact_agreement': valid and observed == expected,
            'reference_kind': 'synthetic_source_constructed_provisional', 'human_verified': False})
    return results


def rework_metrics(case, report):
    keys = acquired_key_observations(case, report)
    first = report.get('first_pass', {})
    before_ids = {eid for row in first.get('collection', []) for eid in row.get('evidence_ids', [])}
    before_keys = {item.get('locator', '').rsplit('/', 1)[-1] for item in report.get('evidence', [])
                   if item.get('evidence_id') in before_ids}
    acquired = set(case.key_observation_ids) - set(keys['omitted_ids'])
    reworks = report.get('reworks', [])
    changed = [target for row in reworks for target in row.get('changed_ids', [])]
    withdrawn = [target for row in reworks for target in row.get('withdrawn_ids', [])]
    return {'key_observations': keys, 'rework_rounds': report.get('rework_rounds', 0),
            'has_first_pass_snapshot': bool(first),
            'key_observations_acquired_after_review': sorted(acquired - before_keys) if first else None,
            'changed_target_ids': changed, 'withdrawn_target_ids': withdrawn,
            'revision_audit': [row.get('revision_audit', []) for row in reworks],
            'effective_narrowing_human_judgment': None,
            'narrowing_note': 'Changed or withdrawn text is auditable behavior, not proof of semantic improvement.'}


def metrics_for(case, report, stage):
    summary = report.get('run_summary', {})
    return {'status': report.get('status'), 'review_status': report.get('review_status'),
            'model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
            'token_usage': summary.get('token_usage'), 'duration_ms': summary.get('duration_ms'),
            'termination_reason': summary.get('termination_reason'),
            'execution_error': report.get('execution_error'),
            'review_controls': score_review(report) if stage == 'review' else [],
            'closed_loop': rework_metrics(case, report) if stage in {'e2e', 'fixed_rework'} else None,
            'fixed_original_review_controls': score_review({
                'provisional_reference': report.get('first_pass_provisional_reference', {}),
                'item_reviews': report.get('first_pass', {}).get('review', {}).get('items', [])})
                if stage == 'fixed_rework' else []}


def arm_summaries(runs):
    result = {}
    for arm in sorted({run.get('arm', 'review') for run in runs}):
        rows = [run for run in runs if run.get('arm', 'review') == arm]
        controls = [item for row in rows for item in row['metrics']['review_controls']]
        assessed = [item for item in controls if item['protocol_status'] == 'valid']
        positives = [item for item in assessed if item['expected'] in {'supported', 'appropriate'}]
        negatives = [item for item in assessed if item['expected'] not in {'supported', 'appropriate'}]
        groups = {}
        for row in rows:
            for item in row['metrics']['review_controls']:
                groups.setdefault((row['case_id'], item['target_id']), []).append(item['observed'])
        repeated = [values for values in groups.values() if len(values) > 1]
        changes = sum(len(set(values)) > 1 for values in repeated)
        closed = [row['metrics']['closed_loop'] for row in rows if row['metrics'].get('closed_loop')]
        counts = {}
        for key in ('model_calls', 'tool_calls', 'duration_ms'):
            known = [row['metrics'][key] for row in rows if isinstance(row['metrics'].get(key), (int, float))]
            counts[key] = {'known_runs': len(known), 'sum': sum(known),
                           'mean': sum(known) / len(known) if known else None}
        false_accepts = sum(item['false_accept'] for item in negatives)
        false_rejects = sum(item['false_reject'] for item in positives)
        result[arm] = {'runs': len(rows), 'reference_items': len(controls), 'assessed_items': len(assessed),
            'negative_reference_items': len(negatives), 'positive_reference_items': len(positives),
            'provisional_false_accepts': false_accepts, 'provisional_false_rejects': false_rejects,
            'provisional_false_accept_rate': false_accepts / len(negatives) if negatives else None,
            'provisional_false_reject_rate': false_rejects / len(positives) if positives else None,
            'repeated_groups': len(repeated), 'groups_with_relation_changes': changes,
            'resources': counts,
            'key_observations_required': sum(row['key_observations']['required_count'] for row in closed),
            'key_observations_omitted': sum(len(row['key_observations']['omitted_ids']) for row in closed),
            'key_observations_acquired_after_review': sum(len(row['key_observations_acquired_after_review'])
                for row in closed if row['key_observations_acquired_after_review'] is not None),
            'rework_rounds': sum(row['rework_rounds'] for row in closed),
            'changed_targets': sum(len(row['changed_target_ids']) for row in closed),
            'withdrawn_targets': sum(len(row['withdrawn_target_ids']) for row in closed),
            'effective_narrowing_human_judgment': None}
    return result


def paired_deltas(runs):
    grouped = {}
    for run in runs:
        grouped.setdefault((run['case_id'], run['repeat']), {})[run.get('arm', 'review')] = run
    pairs = []
    for (case, repeat), arms in grouped.items():
        baseline, final = (('review', 'challenged_review') if 'review' in arms else ('no_rework', 'closed_loop'))
        if baseline not in arms or final not in arms:
            continue
        a, b = arms[baseline]['metrics'], arms[final]['metrics']
        delta = {key: b[key] - a[key] if isinstance(a.get(key), (int, float))
                 and isinstance(b.get(key), (int, float)) else None
                 for key in ('model_calls', 'tool_calls', 'duration_ms')}
        pairs.append({'case_id': case, 'repeat': repeat, 'baseline_arm': baseline, 'final_arm': final,
                      'resource_delta_final_minus_baseline': delta,
                      'semantic_accuracy_delta': None})
    return pairs


def summarize(runs, *, execution_mode, expected_runs):
    controls, groups = [], {}
    for run in runs:
        for item in run['metrics']['review_controls']:
            controls.append(item)
            groups.setdefault((run['case_id'], run.get('arm', 'review'), item['target_id']), []).append(item['observed'])
    assessed = [item for item in controls if item['protocol_status'] == 'valid']
    positives = [item for item in assessed if item['expected'] in {'supported', 'appropriate'}]
    negatives = [item for item in assessed if item['expected'] not in {'supported', 'appropriate'}]
    false_accepts = sum(item['false_accept'] for item in negatives)
    false_rejects = sum(item['false_reject'] for item in positives)
    repeated = [values for values in groups.values() if len(values) > 1]
    changes = sum(len(set(values)) > 1 for values in repeated)
    complete = len(runs) == expected_runs and all(not row['metrics']['execution_error'] for row in runs)
    by_arm = arm_summaries(runs)
    gate_arm = next((arm for arm in ('challenged_review', 'review') if arm in by_arm), None)
    final = by_arm.get(gate_arm, {})
    gates = {'complete_execution': complete,
             'all_reference_items_assessed': bool(final.get('reference_items'))
                 and final['assessed_items'] == final['reference_items'],
             'zero_provisional_false_accepts': bool(final.get('negative_reference_items'))
                 and final['provisional_false_accepts'] == 0,
             'provisional_false_reject_rate_at_most_20_percent': bool(final.get('positive_reference_items'))
                 and final['provisional_false_reject_rate'] <= .2,
             'repeated_relation_flip_rate_at_most_20_percent': bool(final.get('repeated_groups'))
                 and final['groups_with_relation_changes'] / final['repeated_groups'] <= .2}
    synthetic_status = ('not_evaluated' if execution_mode != 'live' or not controls else
                        'passed' if all(gates.values()) else 'not_met')
    return {'reference_kind': 'synthetic_source_constructed_provisional', 'runs': len(runs),
            'expected_runs': expected_runs, 'reference_items': len(controls), 'assessed_items': len(assessed),
            'provisional_false_accepts': false_accepts, 'negative_reference_items': len(negatives),
            'provisional_false_rejects': false_rejects, 'positive_reference_items': len(positives),
            'provisional_exact_agreement': sum(item['exact_agreement'] for item in assessed),
            'repeated_groups': len(repeated), 'groups_with_relation_changes': changes,
            'by_arm': by_arm, 'paired_deltas': paired_deltas(runs),
            'synthetic_gate': {'status': synthetic_status, 'evaluated_arm': gate_arm, 'conditions': gates},
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold',
            'rollout_reason': 'Human semantic acceptance remains pending; synthetic controls do not authorize production rollout.'}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--scripted', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--stage', choices=('review', 'fixed_rework', 'e2e'), default='review')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--arms', nargs='+', choices=('review', 'challenged_review', 'no_rework', 'closed_loop'))
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--model-budget', type=int, default=12)
    parser.add_argument('--tool-budget', type=int, default=8)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'closed-loop-evaluation')
    args = parser.parse_args(argv)
    known = build_cases()
    args.cases = args.cases or list(known)
    args.arms = args.arms or (['review', 'challenged_review'] if args.stage == 'review' else ['no_rework', 'closed_loop'])
    if not args.cases or len(set(args.cases)) != len(args.cases) or not set(args.cases) <= known.keys():
        parser.error('Unknown or duplicate synthetic case')
    if len(set(args.arms)) != len(args.arms) \
            or (args.stage == 'review' and not set(args.arms) <= {'review', 'challenged_review'}) \
            or (args.stage != 'review' and not set(args.arms) <= {'no_rework', 'closed_loop'}):
        parser.error('Stage and arms do not match')
    if not 1 <= args.repeats <= 5 or not 3 <= args.model_budget <= 32 \
            or not 1 <= args.tool_budget <= 48 or not 20 <= args.seconds <= 300:
        parser.error('Invalid repeats or per-run limits')
    if args.live and (args.budget_ledger is None or args.max_model_calls is None):
        parser.error('Live requires an existing shared --budget-ledger and total --max-model-calls')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 180:
        parser.error('Total --max-model-calls must be 1..180')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        cases, config = build_cases(), IncidentConfig.from_file()
        if args.model:
            config = replace(config, model=args.model)
        plan = plan_for(args, config, cases)
        if not args.live and not args.scripted:
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, **plan}, ensure_ascii=False, indent=2))
            return 0
        if args.live and (not config.api_key or config.api_key == 'test-only'):
            parser.error('Model credentials required; no request sent')
        budget = GlobalPaidBudget(args.budget_ledger, args.max_model_calls) if args.live else None
        if args.live:
            from agents.models import build_model
            model = BudgetedModel(build_model(config.api_key, config.model, base_url=config.base_url,
                extra_body=config.extra_body, max_output_tokens=MODEL_SETTINGS['max_output_tokens'],
                request_timeout=MODEL_SETTINGS['request_timeout']), budget)
        directory = args.output / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'plan.json', plan)
        runs, human = [], {'status': 'pending', 'reviewer': None, 'runs': []}
        for case_id, arm, repeat in schedule(args.cases, args.arms, args.repeats):
            if versions() != plan['application']:
                raise ValueError('Application changed after evaluation freeze')
            if budget and (budget.session_requests >= args.max_model_calls or budget.snapshot().get('admission_closed')
                    or budget.remaining()['requests'] < 1):
                break
            case = cases[case_id]
            if args.scripted:
                from workflow.v2.scripted import scripted_model
                model = scripted_model(SourceWorldProvider(case))
            if args.stage == 'review':
                report = run_review(case, model, limits_for(args), arm=arm)
            elif args.stage == 'fixed_rework':
                report = run_fixed_rework(case, arm, model, limits_for(args))
            else:
                report = run_e2e(case, arm, model, limits_for(args))
            report.update(case_id=case_id, arm=arm, repeat=repeat, execution_mode=plan['execution_mode'],
                          application=plan['application'], synthetic_reference=case.freeze())
            name = f'{case_id}-{arm}-r{repeat}.json'
            write_json(directory / name, report)
            entry = {'case_id': case_id, 'arm': arm, 'repeat': repeat, 'report_file': name,
                     'report_sha256': hashlib.sha256((directory / name).read_bytes()).hexdigest(),
                     'metrics': metrics_for(case, report, args.stage)}
            runs.append(entry)
            human['runs'].append({'report_file': name, 'report_sha256': entry['report_sha256'],
                'reviewer': None, 'approved': None, 'false_accept': None, 'false_reject': None,
                'effective_narrowing': None, 'causal_overreach': None, 'notes': None})
            summary = summarize(runs, execution_mode=plan['execution_mode'], expected_runs=plan['planned_runs'])
            write_json(directory / 'bundle.json', {'plan': plan, 'runs': runs, 'summary': summary,
                'paid_budget': budget.summary() if budget else {'requests': 0}})
            write_json(directory / 'human-review.pending.json', human)
            print(json.dumps({'case_id': case_id, 'arm': arm, 'repeat': repeat,
                              'directory': str(directory), 'metrics': entry['metrics']}, ensure_ascii=False), flush=True)
        summary = summarize(runs, execution_mode=plan['execution_mode'], expected_runs=plan['planned_runs'])
        write_json(directory / 'bundle.json', {'plan': plan, 'runs': runs, 'summary': summary,
            'paid_budget': budget.summary() if budget else {'requests': 0}})
        write_json(directory / 'human-review.pending.json', human)
        print(json.dumps({'directory': str(directory), 'summary': summary}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'evaluation_error': safe_exception(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
