"""Focused seeded edit probes, with a separate paid cap per case. Default: no models."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
from langchain_core.messages import AIMessage

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
REVIEW_SESSION = 'reviewer_contract_followup'


class ReplayInitialRevision:
    """Replay one already-paid diagnosis turn; all subsequent turns remain live."""
    json_output = True
    blind_review = False

    def __init__(self, live, content):
        self.live, self.content = live, content
        self.replayed = False

    def invoke(self, messages):
        if not self.replayed:
            self.replayed = True
            return AIMessage(content=self.content)
        return self.live.invoke(messages)


def control_checks(report):
    drafts = report.get('drafts', [])
    if len(drafts) < 2:
        return {'revised_draft_committed': False, 'untargeted_sections_preserved': None,
                'action_changed': None, 'final_review_completed': False}
    before, after = drafts[0]['draft'], drafts[1]['draft']
    reviews = report.get('reviews', [])
    final_review_completed = bool(reviews) and reviews[-1]['revision'] == drafts[-1]['revision']
    return {'revised_draft_committed': True,
        'untargeted_sections_preserved': before['findings'] == after['findings'] and before['hypotheses'] == after['hypotheses'],
        'action_changed': before['recommended_actions'][0]['action'] != after['recommended_actions'][0]['action'],
        'final_review_completed': final_review_completed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--previous-summary', type=Path, help='Carry this focused session allowance across implementation fixes')
    parser.add_argument('--validation-session', choices=['focused_revision_followup', 'scope_conflict_followup', REVIEW_SESSION],
                        default='focused_revision_followup')
    parser.add_argument('--replay-initial-revision-from', type=Path,
        help='For unsupported_mechanism only: replay the retained paid initial edit, then independently rerun live review/correction')
    parser.add_argument('--variants', nargs='+', choices=VARIANTS, default=list(VARIANTS))
    args = parser.parse_args(argv)
    if len(set(args.variants)) != len(args.variants):
        parser.error('Duplicate variants')
    case_cap = 12 if args.validation_session == REVIEW_SESSION else CASE_PAID_CAP
    final_review_reserve = 2 if args.validation_session == REVIEW_SESSION else 0
    limits = Limits(model_calls=16, tool_calls=6, seconds=360, reserve_model_calls=2, reserve_seconds=20) if final_review_reserve else LIMITS
    execution_order = sorted(args.variants, key=lambda item: item != 'unsupported_mechanism') if final_review_reserve else args.variants
    if final_review_reserve and args.replay_initial_revision_from:
        parser.error('Reviewer contract follow-up runs fresh; paid reply replay is not permitted')
    config = IncidentConfig.from_file()
    source = implementation_version()
    scripts = {name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
               for name in ('verify_revision_probe.py', 'verify_review_driven.py', 'compare_incident.py')}
    fixture_hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted((ROOT / 'fixtures/incident').glob('*.json'))}
    prior_counts = dict.fromkeys(args.variants, 0)
    previous_ref = None
    previous = None
    if args.previous_summary:
        previous = json.loads(args.previous_summary.read_text(encoding='utf-8'))
        previous_plan = previous['plan']
        if (previous_plan['validation_session'] != args.validation_session
                or previous_plan['model'] != config.model or previous_plan['variants'] != args.variants
                or previous_plan['paid_cap_per_case'] != case_cap
                or previous_plan.get('final_review_reserve_per_case', 0) != final_review_reserve
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
                or any(type(count) is not int or not 0 <= count <= case_cap for count in prior_counts.values())
                or sum(prior_counts.values()) != previous['paid_attempts']):
            raise ValueError('Previous focused paid counts are inconsistent')
        previous_ref = {'path': str(args.previous_summary.resolve()),
                        'sha256': hashlib.sha256(args.previous_summary.read_bytes()).hexdigest()}
    replay_ref, replay_content = None, None
    if args.replay_initial_revision_from:
        if previous is None or 'unsupported_mechanism' not in args.variants:
            raise ValueError('Replay requires the matching previous summary and mechanism variant')
        recorded = next(row for row in previous['runs'] if row['variant'] == 'unsupported_mechanism')
        path = args.replay_initial_revision_from.resolve()
        if path != (ROOT / recorded['report_file']).resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != recorded['report_sha256']:
            raise ValueError('Replay report is not the retained previous mechanism report')
        replies = path.with_name(path.stem + '-replies.json')
        data = json.loads(replies.read_text(encoding='utf-8'))
        replay_content = data['diagnosis'][0]['content']
        replay_ref = {'report_file': recorded['report_file'], 'report_sha256': recorded['report_sha256'],
            'reply_file': replies.relative_to(ROOT).as_posix(), 'reply_sha256': hashlib.sha256(replies.read_bytes()).hexdigest(),
            'content_sha256': hashlib.sha256(replay_content.encode()).hexdigest(),
            'source_version': previous['plan']['source_version'], 'new_inference': False}
    plan = {'source_version': source, 'script_hashes': scripts, 'fixture_hashes': fixture_hashes,
            'model': config.model, 'variants': args.variants, 'execution_order': execution_order, 'limits': asdict(limits),
            'paid_cap_per_case': case_cap, 'maximum_paid_attempts': case_cap * len(args.variants),
            'final_review_reserve_per_case': final_review_reserve,
            'seeded_feedback': True, 'attempts_per_variant': 1, 'independent_quality_score': False,
            'synthetic_observations': True, 'live_repairs': 0,
            'validation_session': args.validation_session, 'estimated_cost': None,
            'prior_case_paid_attempts': prior_counts, 'remaining_case_paid_attempts': {
                variant: case_cap - count for variant, count in prior_counts.items()},
            'previous_summary': previous_ref,
            'replayed_initial_revision': replay_ref,
            'boundary_control': 'unsupported_mechanism is deliberately false preliminary seed judgment; final review must be independent'}
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return 0
    if not config.api_key or config.api_key == 'test-only':
        raise ValueError('Configured live model credentials required')
    directory = ROOT / 'output/revision-probe' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    rows = []
    for variant in execution_order:
        assert implementation_version() == source, 'Application changed during validation'
        assert all(hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest() == expected
                   for name, expected in scripts.items())
        assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
                   for name, expected in fixture_hashes.items())
        if replay_ref:
            assert hashlib.sha256((ROOT / replay_ref['reply_file']).read_bytes()).hexdigest() == replay_ref['reply_sha256']
        print(json.dumps({'starting': variant, 'directory': str(directory)}, ensure_ascii=False), flush=True)
        budget = PaidBudget(maximum=case_cap, final_review_reserve=final_review_reserve)
        budget.used = prior_counts[variant]
        paid_before = budget.used
        live = {role: RecordedModel(model, budget, role=role) for role, model in build_roles(
            config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body).items()}
        delegated = dict(live)
        if replay_ref and variant == 'unsupported_mechanism':
            delegated['diagnosis'] = ReplayInitialRevision(live['diagnosis'], replay_content)
        models = probe_models(delegated, variant)
        report = capture('case_001', 'multi', lambda flow: models, Principal('synthetic_demo', 'cli_reader'), limits,
            provider_factory=FixtureProvider, investigation_mode='programmatic', knowledge_mode='programmatic',
            blind_review=False, orchestration='review_driven')
        report.update(source_version=source, model=config.model, execution_mode='live_seeded_probe',
                      seeded_feedback=True, probe_variant=variant, independent_quality_score=False)
        report['replayed_revision_turns'] = int(bool(replay_ref) and variant == 'unsupported_mechanism')
        path = directory / f'{variant}.json'
        write_json(path, report)
        write_json(directory / f'{variant}-replies.json', {role: model.replies for role, model in live.items()})
        if 'output' in report:
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
        row = {'variant': variant, 'status': report['status'], 'stop': report['run_summary']['termination_reason'],
            'paid_attempts': budget.used - paid_before, 'role_paid_attempts': {role: model.attempts for role, model in live.items()},
            'model_calls': report['run_summary']['model_calls'], 'tool_calls': report['run_summary']['tool_calls'],
            'pure_revisions': report.get('pure_revision_rounds', 0), 'reworks': report.get('rework_rounds', 0),
            'scope_revisions': report.get('scope_revision_rounds', 0),
            'final_review_paid_attempts': live['reviewer'].final_review_attempts,
            'replayed_revision_turns': report['replayed_revision_turns'],
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
