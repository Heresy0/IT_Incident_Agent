"""Focused seeded edit probes, with a separate paid cap per case. Default: no models."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from agents.models import build_roles
from api.auth import Principal
from config.incident import IncidentConfig
from evidence.render import render
from evals.common import write_json
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from scripts.verify_review_driven import PaidBudget, RecordedModel, probe_models

VARIANTS = ('wording', 'trigger_unknown', 'unsupported_mechanism')
LIMITS = Limits(model_calls=12, tool_calls=6, seconds=240, reserve_model_calls=2, reserve_seconds=20)
CASE_PAID_CAP = 8


def control_checks(report):
    drafts = report.get('drafts', [])
    if len(drafts) < 2:
        return {'revised_draft_committed': False, 'untargeted_sections_preserved': None,
                'action_changed': None, 'final_review_completed': False}
    before, after = drafts[0]['draft'], drafts[1]['draft']
    return {'revised_draft_committed': True,
        'untargeted_sections_preserved': before['findings'] == after['findings'] and before['hypotheses'] == after['hypotheses'],
        'action_changed': before['recommended_actions'][0]['action'] != after['recommended_actions'][0]['action'],
        'final_review_completed': len(report.get('reviews', [])) >= 2}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--previous-summary', type=Path, help='Carry this focused session allowance across implementation fixes')
    parser.add_argument('--variants', nargs='+', choices=VARIANTS, default=list(VARIANTS))
    args = parser.parse_args(argv)
    if len(set(args.variants)) != len(args.variants):
        parser.error('Duplicate variants')
    config = IncidentConfig.from_file()
    source = implementation_version()
    scripts = {name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
               for name in ('verify_revision_probe.py', 'verify_review_driven.py', 'compare_incident.py')}
    fixture_hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted((ROOT / 'fixtures/incident').glob('*.json'))}
    prior_counts = dict.fromkeys(args.variants, 0)
    previous_ref = None
    if args.previous_summary:
        previous = json.loads(args.previous_summary.read_text(encoding='utf-8'))
        previous_plan = previous['plan']
        if (previous_plan['validation_session'] != 'focused_revision_followup'
                or previous_plan['model'] != config.model or previous_plan['variants'] != args.variants
                or previous_plan['paid_cap_per_case'] != CASE_PAID_CAP
                or previous_plan['fixture_hashes'] != fixture_hashes):
            raise ValueError('Previous focused session model, variants, cap or fixtures differ')
        prior_counts.update(previous_plan.get('prior_case_paid_attempts', {}))
        if {row['variant'] for row in previous['runs']} != set(args.variants) or len(previous['runs']) != len(args.variants):
            raise ValueError('Previous focused batch is incomplete or duplicated')
        for row in previous['runs']:
            if type(row['paid_attempts']) is not int or row['paid_attempts'] < 0:
                raise ValueError('Invalid paid attempts')
            prior_counts[row['variant']] += row['paid_attempts']
        if (set(prior_counts) != set(args.variants)
                or any(type(count) is not int or not 0 <= count <= CASE_PAID_CAP for count in prior_counts.values())
                or sum(prior_counts.values()) != previous['paid_attempts']):
            raise ValueError('Previous focused paid counts are inconsistent')
        previous_ref = {'path': str(args.previous_summary.resolve()),
                        'sha256': hashlib.sha256(args.previous_summary.read_bytes()).hexdigest()}
    plan = {'source_version': source, 'script_hashes': scripts, 'fixture_hashes': fixture_hashes,
            'model': config.model, 'variants': args.variants, 'limits': asdict(LIMITS),
            'paid_cap_per_case': CASE_PAID_CAP, 'maximum_paid_attempts': CASE_PAID_CAP * len(args.variants),
            'seeded_feedback': True, 'attempts_per_variant': 1, 'independent_quality_score': False,
            'synthetic_observations': True, 'live_repairs': 0,
            'validation_session': 'focused_revision_followup', 'estimated_cost': None,
            'prior_case_paid_attempts': prior_counts, 'remaining_case_paid_attempts': {
                variant: CASE_PAID_CAP - count for variant, count in prior_counts.items()},
            'previous_summary': previous_ref,
            'boundary_control': 'unsupported_mechanism is deliberately false preliminary seed judgment; final review must be independent'}
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return 0
    if not config.api_key or config.api_key == 'test-only':
        raise ValueError('Configured live model credentials required')
    directory = ROOT / 'output/revision-probe' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    rows = []
    for variant in args.variants:
        assert implementation_version() == source, 'Application changed during validation'
        assert all(hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest() == expected
                   for name, expected in scripts.items())
        assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
                   for name, expected in fixture_hashes.items())
        print(json.dumps({'starting': variant, 'directory': str(directory)}, ensure_ascii=False), flush=True)
        budget = PaidBudget(maximum=CASE_PAID_CAP)
        budget.used = prior_counts[variant]
        paid_before = budget.used
        live = {role: RecordedModel(model, budget) for role, model in build_roles(
            config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body).items()}
        models = probe_models(live, variant)
        report = capture('case_001', 'multi', lambda flow: models, Principal('synthetic_demo', 'cli_reader'), LIMITS,
            provider_factory=FixtureProvider, investigation_mode='programmatic', knowledge_mode='programmatic',
            blind_review=False, orchestration='review_driven')
        report.update(source_version=source, model=config.model, execution_mode='live_seeded_probe',
                      seeded_feedback=True, probe_variant=variant, independent_quality_score=False)
        path = directory / f'{variant}.json'
        write_json(path, report)
        write_json(directory / f'{variant}-replies.json', {role: model.replies for role, model in live.items()})
        if 'output' in report:
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
        row = {'variant': variant, 'status': report['status'], 'stop': report['run_summary']['termination_reason'],
            'paid_attempts': budget.used - paid_before, 'role_paid_attempts': {role: model.attempts for role, model in live.items()},
            'model_calls': report['run_summary']['model_calls'], 'tool_calls': report['run_summary']['tool_calls'],
            'pure_revisions': report.get('pure_revision_rounds', 0), 'reworks': report.get('rework_rounds', 0),
            'handoff_reason': report.get('handoff', {}).get('reason'), 'controls': control_checks(report),
            'validation_failures': [e.get('reason') for e in report.get('events', []) if e['type'] == 'validation_failure'],
            'report_file': path.relative_to(ROOT).as_posix(),
            'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        rows.append(row)
        write_json(directory / 'summary.json', {'plan': plan,
            'paid_attempts': sum(prior_counts.values()) + sum(row['paid_attempts'] for row in rows), 'runs': rows})
        print(json.dumps(row, ensure_ascii=False), flush=True)
        assert implementation_version() == source
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
