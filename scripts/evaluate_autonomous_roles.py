"""Frozen Flash/Plus role ablation. Default planning never sends paid requests.

Only one declared role changes in each experimental arm. All three arms run the
same closed-loop application and source-constructed worlds under identical hard
limits. References remain provisional and do not authorize production rollout.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from config.incident import IncidentConfig
from evals.closed_loop_cases import (VERSION, PRINCIPAL, SourceWorldProvider, build_cases,
                                     fixed_draft, fingerprint, reference_targets)
from evals.common import write_json
from runtime.context import Limits
from scripts.evaluate_autonomous import (BudgetedModel, GlobalPaidBudget, MODEL_SETTINGS,
                                         jsonable, safe_exception)
from scripts.evaluate_autonomous_closed_loop import (context_for, metrics_for, score_review,
                                                     versions)

ROLES = ('investigation', 'diagnosis', 'reviewer', 'challenger')
ARMS = ('flash_all', 'investigator_plus', 'reviewer_plus')
FROZEN_LIMITS = Limits(model_calls=12, tool_calls=8, seconds=180,
                       reserve_model_calls=2, reserve_seconds=15, input_chars=48000)
EVALUATION_FILES = ('scripts/evaluate_autonomous_roles.py',
                    'scripts/evaluate_autonomous_closed_loop.py',
                    'scripts/evaluate_autonomous.py', 'evals/closed_loop_cases.py')


def role_map(arm, flash='qwen3.7-flash', plus='qwen3.7-plus'):
    if arm not in ARMS or not flash or not plus or flash == plus:
        raise ValueError('Distinct Flash/Plus models and a known arm are required')
    result = dict.fromkeys(ROLES, flash)
    if arm == 'investigator_plus':
        result['investigation'] = plus
    elif arm == 'reviewer_plus':
        result['reviewer'] = plus
    return result


def schedule_roles(cases, arms, repeats):
    """Rotate arm order across case/repetition blocks, preserving matched worlds."""
    for repeat in range(1, repeats + 1):
        for index, case_id in enumerate(cases):
            offset = (repeat - 1 + index) % len(arms)
            for arm in [*arms[offset:], *arms[:offset]]:
                yield case_id, arm, repeat


def source_freeze():
    return {'application': versions(),
            'app_source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted((ROOT / 'app').rglob('*.py'))},
            'evaluation_source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                          for name in EVALUATION_FILES}}


def plan_for(args, config, cases):
    source = source_freeze()
    models = {arm: role_map(arm, args.flash_model, args.plus_model) for arm in args.arms}
    scheduled = len(args.cases) * len(args.arms) * args.repeats
    return {'version': 'autonomous-role-ablation-v1', 'stage': args.stage,
            'execution_mode': 'live' if args.live else 'plan_only',
            'comparison': 'same_current_application_closed_loop_single_role_model_replacement',
            'cases': args.cases, 'arms': args.arms, 'repeats': args.repeats,
            'planned_runs': scheduled, 'limits': asdict(FROZEN_LIMITS),
            'maximum_model_calls': min(scheduled * FROZEN_LIMITS.model_calls, args.max_model_calls)
                if args.max_model_calls else scheduled * FROZEN_LIMITS.model_calls,
            'model_maps': models, 'model_settings': dict(MODEL_SETTINGS),
            'role_model_settings': {role: dict(MODEL_SETTINGS) for role in ROLES},
            'provider_configuration_sha256': fingerprint({'base_url': config.base_url,
                                                          'extra_body': config.extra_body}),
            'configuration_sha256': fingerprint({'model_maps': models,
                'settings': MODEL_SETTINGS, 'limits': asdict(FROZEN_LIMITS),
                'base_url': config.base_url, 'extra_body': config.extra_body}),
            **source, 'dataset_version': VERSION,
            'case_freezes': [cases[key].freeze() for key in args.cases],
            'reference_kind': 'synthetic_source_constructed_provisional',
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold', 'read_only': True,
            'estimated_cost': None,
            'price_note': 'No price assumptions; measured per-model token usage supports a dated external rate card.',
            'schedule': [{'case_id': cid, 'arm': arm, 'repeat': repeat}
                         for cid, arm, repeat in schedule_roles(args.cases, args.arms, args.repeats)]}


def _usage(response):
    usage = getattr(response, 'usage_metadata', None)
    if not usage:
        raw = getattr(response, 'response_metadata', {}).get('token_usage', {})
        usage = {'input_tokens': raw.get('prompt_tokens'), 'output_tokens': raw.get('completion_tokens')}
    if isinstance(usage, dict) and all(isinstance(usage.get(k), int) and usage[k] >= 0
                                     for k in ('input_tokens', 'output_tokens')):
        return {key: usage[key] for key in ('input_tokens', 'output_tokens')}
    return None


class RecordedRoleModel:
    """Log hashes and observed usage, never provider configuration or credentials."""
    def __init__(self, model, role, model_name, calls, *, tools=(), budget=None):
        self.model, self.role, self.model_name, self.calls = model, role, model_name, calls
        self.tools, self.budget = tools, budget
        self.json_output = getattr(model, 'json_output', True)
        self.blind_review = getattr(model, 'blind_review', False)

    def bind_tools(self, tools):
        return RecordedRoleModel(self.model.bind_tools(tools), self.role, self.model_name,
                                 self.calls, tools=tools, budget=self.budget)

    def invoke(self, messages):
        serialized = {'messages': [m.model_dump(mode='json') for m in messages], 'tools': self.tools}
        request_hash = fingerprint(serialized)
        stage = self.role
        try:
            body = json.loads(messages[-1].content)
            stage = body.get('stage') or ('challenge' if 'challenge_policy_version' in body else self.role)
        except (ValueError, TypeError, AttributeError, IndexError):
            pass
        before = self.budget.session_requests if self.budget else None
        row = {'role': self.role, 'model': self.model_name, 'stage': stage,
               'request_sha256': request_hash, 'output_sha256': None, 'usage': None,
               'seconds': None, 'error': None, 'charged_request': None,
               'response_model_name': None}
        started = time.monotonic()
        try:
            response = self.model.invoke(messages)
            row['output_sha256'] = fingerprint({'content': response.content,
                'tool_calls': getattr(response, 'tool_calls', []),
                'invalid_tool_calls': getattr(response, 'invalid_tool_calls', [])})
            row['usage'] = _usage(response)
            resolved = getattr(response, 'response_metadata', {}).get('model_name')
            if isinstance(resolved, str) and 0 < len(resolved) <= 160:
                row['response_model_name'] = resolved
            return response
        except Exception as exc:
            row['error'] = safe_exception(exc)
            raise
        finally:
            row['seconds'] = time.monotonic() - started
            if self.budget:
                row['charged_request'] = self.budget.session_requests > before
            self.calls.append(row)


def role_usage(calls):
    result = {}
    for row in calls:
        key = (row['role'], row['model'])
        group = result.setdefault(key, {'role': key[0], 'model': key[1], 'calls': 0,
            'input_tokens': 0, 'output_tokens': 0, 'known_usage_calls': 0,
            'unknown_usage_calls': 0, 'seconds': 0, 'errors': 0})
        group['calls'] += 1
        group['seconds'] += row['seconds']
        group['errors'] += bool(row['error'])
        if row['usage'] is None:
            group['unknown_usage_calls'] += 1
        else:
            group['known_usage_calls'] += 1
            for quantity in ('input_tokens', 'output_tokens'):
                group[quantity] += row['usage'][quantity]
    return list(result.values())


def models_for(mapping, config, budget, calls, *, factory=None):
    if set(mapping) != set(ROLES):
        raise ValueError('All roles must be explicitly bound; fallback is forbidden')
    if factory is None:
        from agents.models import build_model
        factory = build_model
    models = {}
    for role in ROLES:
        model = factory(config.api_key, mapping[role], base_url=config.base_url,
                        extra_body=config.extra_body, max_output_tokens=MODEL_SETTINGS['max_output_tokens'],
                        request_timeout=MODEL_SETTINGS['request_timeout'])
        if budget is not None:
            model = BudgetedModel(model, budget)
        models[role] = RecordedRoleModel(model, role, mapping[role], calls, budget=budget)
    return models


def run_fixed(case, models, limits, *, rework):
    """Primary reviewer and blind challenger receive separately bound models."""
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
        batch = review_draft(models['reviewer'], draft, executor, context, repairs=repairs,
                             collection=collection_view(executor))
        primary = jsonable(batch)
        batch, initial_challenge = challenge_supported(models['challenger'], draft, batch,
            executor, context, collection=collection_view(executor))
        first = {'draft': jsonable(draft), 'review': jsonable(batch), 'primary_batch': primary,
                 'collection': jsonable(list(executor.check_results.values())),
                 'evidence': [e.model_dump(mode='json') for e in executor.evidence.values()],
                 'run_summary': context.summary()}
        audit = {'performed': False, 'reason': 'REVIEW_ONLY_EVALUATION'}
        if rework:
            draft, batch, audit = execute_review_rework(models, draft, batch, executor, context,
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
        else:
            report = {'fixed_draft': raw, 'primary_batch': primary, 'review_batch': jsonable(batch),
                      'item_reviews': jsonable(batch)['items'], 'challenge_audit': initial_challenge,
                      'provisional_reference': expected, 'challenge_is_ground_truth': False}
    except Exception as exc:
        report = {'execution_error': safe_exception(exc), 'status': 'failed', 'item_reviews': [],
                  'provisional_reference': reference_targets(case) if not rework else {}}
    report.update(evidence=[e.model_dump(mode='json') for e in executor.evidence.values()],
                  collection=list(executor.check_results.values()), run_summary=context.summary(), events=events)
    return report


def run_e2e(case, models, limits):
    from workflow.v2.engine import investigate
    provider = SourceWorldProvider(case)
    context, events = context_for(provider, limits)
    try:
        return investigate(models, provider, PRINCIPAL, context=context, event_log=events,
                           enable_rework=True, enable_challenge=True)
    except Exception as exc:
        return {'execution_error': safe_exception(exc), 'status': 'failed', 'evidence': [],
                'run_summary': context.summary(), 'events': events}


def run_case(case, models, stage, limits=FROZEN_LIMITS):
    if set(models) != set(ROLES):
        raise ValueError('Explicit binding of all roles is required')
    return run_e2e(case, models, limits) if stage == 'e2e' else run_fixed(case, models, limits,
                                                                       rework=stage == 'fixed_rework')


def run_metrics(case, report, stage):
    metrics = metrics_for(case, report, stage)
    reference = report.get('provisional_reference', {}) if stage == 'review' else report.get('first_pass_provisional_reference', {})
    primary = report.get('primary_batch', {})
    final = report.get('review_batch', {}) if stage == 'review' else report.get('first_pass', {}).get('review', {})
    metrics['primary_review_controls'] = score_review({'provisional_reference': reference,
                                                      'item_reviews': primary.get('items', [])})
    metrics['final_review_controls'] = score_review({'provisional_reference': reference,
                                                    'item_reviews': final.get('items', [])})
    metrics['role_usage'] = role_usage(report.get('model_call_log', []))
    metrics['model_seconds'] = sum(row['seconds'] for row in report.get('model_call_log', []))
    return metrics


def summarize(runs, expected):
    return {'runs': len(runs), 'expected_runs': expected,
            'complete_execution': len(runs) == expected and all(not r['metrics']['execution_error'] for r in runs),
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold',
            'interpretation': 'Role attribution and original-target controls only; rewritten claims require separate review.'}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--stage', choices=('review', 'fixed_rework', 'e2e'), default='review')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--flash-model', default='qwen3.7-flash')
    parser.add_argument('--plus-model', default='qwen3.7-plus')
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--expected-source')
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'autonomous-roles')
    args = parser.parse_args(argv)
    known = build_cases()
    args.cases = args.cases or list(known)
    if not args.cases or len(set(args.cases)) != len(args.cases) or not set(args.cases) <= known.keys():
        parser.error('Unknown or duplicate synthetic case')
    if not 1 <= args.repeats <= 5 or len(set(args.arms)) != len(args.arms):
        parser.error('Invalid repeats or duplicate arms')
    if not args.flash_model or not args.plus_model or args.flash_model == args.plus_model:
        parser.error('Distinct Flash and Plus model identifiers are required')
    if args.live and (args.budget_ledger is None or args.max_model_calls is None):
        parser.error('Live requires existing single-owner --budget-ledger and --max-model-calls')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 1000:
        parser.error('Total --max-model-calls must be 1..1000')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        cases, config = build_cases(), IncidentConfig.from_file()
        plan = plan_for(args, config, cases)
        if args.expected_source and plan['application']['source_version'] != args.expected_source:
            raise ValueError('Application does not match requested frozen source')
        if not args.live:
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, **plan}, ensure_ascii=False, indent=2))
            return 0
        if not config.api_key or config.api_key == 'test-only':
            parser.error('Model credentials required; no request sent')
        budget = GlobalPaidBudget(args.budget_ledger, args.max_model_calls)
        directory = args.output / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'plan.json', plan)
        runs, human = [], {'status': 'pending', 'reviewer': None, 'runs': []}

        def save():
            write_json(directory / 'bundle.json', {'plan': plan, 'runs': runs,
                'summary': summarize(runs, plan['planned_runs']), 'paid_budget': budget.summary()})
            write_json(directory / 'human-review.pending.json', human)

        save()
        frozen = {key: plan[key] for key in ('application', 'app_source_sha256', 'evaluation_source_sha256')}
        for case_id, arm, repeat in schedule_roles(args.cases, args.arms, args.repeats):
            if source_freeze() != frozen:
                raise ValueError('Application or evaluation changed after freeze')
            if budget.session_requests >= args.max_model_calls or budget.snapshot().get('admission_closed') \
                    or budget.remaining()['requests'] < 1:
                break
            mapping, calls = plan['model_maps'][arm], []
            models = models_for(mapping, config, budget, calls)
            case = cases[case_id]
            report = run_case(case, models, args.stage)
            report.update(case_id=case_id, arm=arm, repeat=repeat, execution_mode='live', stage=args.stage,
                          application=plan['application'], synthetic_reference=case.freeze(),
                          model_map=mapping, model_call_log=calls, role_usage=role_usage(calls))
            name = f'{case_id}-{arm}-r{repeat}.json'
            write_json(directory / name, report)
            entry = {'case_id': case_id, 'arm': arm, 'repeat': repeat, 'report_file': name,
                     'report_sha256': hashlib.sha256((directory / name).read_bytes()).hexdigest(),
                     'metrics': run_metrics(case, report, args.stage)}
            runs.append(entry)
            human['runs'].append({'report_file': name, 'report_sha256': entry['report_sha256'],
                                 'reviewer': None, 'approved': None, 'notes': None})
            save()
            print(json.dumps({'case_id': case_id, 'arm': arm, 'repeat': repeat,
                              'directory': str(directory), 'metrics': entry['metrics']}, ensure_ascii=False), flush=True)
        save()
        print(json.dumps({'directory': str(directory), 'summary': summarize(runs, plan['planned_runs'])}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'evaluation_error': safe_exception(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
