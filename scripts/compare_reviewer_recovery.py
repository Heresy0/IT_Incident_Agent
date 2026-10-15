"""Frozen Reviewer recovery regression: 18 paired runs plus 3 low-cap probes."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts.compare_orchestration_live import (
    ROOT, UNSEEN_CASES, CAP_PER_RUN, RequestLedger, LiveRecorder, digest,
    provider_for, summarize, build_roles, capture, render, write_json,
    IncidentConfig, Principal, implementation_version,
)
from agents.models import build_model
from runtime.context import Limits

LIMITS = Limits(model_calls=16, tool_calls=8, seconds=600,
                reserve_model_calls=3, reserve_seconds=90)
LOCK = ROOT / 'evals/reviewer_recovery/freeze.json'


def execution_plan(config):
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if implementation_version() != lock['source_version'] or config.model != lock['model']:
        raise ValueError('Application or model changed after Reviewer freeze')
    files = {**lock['files'], **lock['driver_files']}
    for relative, expected in files.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != expected:
            raise ValueError('Frozen input or driver changed')
    jobs = [{'case_id': case, 'orchestration': mode, 'repetition': rep + 1,
             'profile': 'primary_4800', 'reviewer_output_tokens': 4800}
            for rep in range(3) for index, case in enumerate(UNSEEN_CASES)
            for mode in (('legacy', 'review_driven') if (index + rep) % 2 == 0
                         else ('review_driven', 'legacy'))]
    jobs += [{'case_id': case, 'orchestration': 'review_driven', 'repetition': 1,
              'profile': 'probe_2200', 'reviewer_output_tokens': 2200}
             for case in UNSEEN_CASES]
    return {'suite': 'reviewer_recovery', 'source_version': implementation_version(),
        'application_commit': lock['application_commit'], 'dataset_lock': digest(LOCK),
        'frozen_file_hashes': files, 'previous_summary': lock['previous_summary'],
        'previous_summary_sha256': lock['previous_summary_sha256'],
        'model': config.model, 'effective_settings_hash': hashlib.sha256(json.dumps(
            {'base_url': config.base_url, 'extra_body': config.extra_body}, sort_keys=True).encode()).hexdigest(),
        'model_settings': {'other_roles': {'temperature': 0, 'max_output_tokens': 2200,
            'timeout': 30, 'max_retries': 0}, 'reviewer': {'temperature': 0, 'timeout': 60,
            'max_retries': 0, 'max_output_tokens': 'per_job'}},
        'limits': asdict(LIMITS), 'paid_cap_per_run': CAP_PER_RUN,
        'maximum_paid_attempts': CAP_PER_RUN * len(jobs), 'execution_order': jobs,
        'seeded_turns': 0, 'replayed_turns': 0, 'synthetic_observations': True,
        'benchmark_type': 'previously_seen_development_regression',
        'independent_blind_holdout': False, 'independent_quality_score': False,
        'estimated_cost': None, 'assessment': 'All failures retained; no tuning or retry-until-pass. '
            'Primary arms share quotas. Three low-cap probes are descriptive, not statistical attribution.',
        'rubric_defect_preserved': lock['rubric_defect_preserved']}


def models_for(config, job, ledger):
    models = build_roles(config.api_key, config.model, base_url=config.base_url,
                         extra_body=config.extra_body)
    models['reviewer'] = build_model(config.api_key, config.model, base_url=config.base_url,
        extra_body=config.extra_body, max_output_tokens=job['reviewer_output_tokens'],
        request_timeout=60)
    return {role: LiveRecorder(model, ledger, role) for role, model in models.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    args = parser.parse_args(argv)
    config = IncidentConfig.from_file()
    plan = execution_plan(config)
    principal = Principal('synthetic_demo', 'cli_reader')
    for case in UNSEEN_CASES:
        provider = provider_for(case)(case, principal)
        provider.authorize(principal, provider.ticket().scope)
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False, indent=2))
        return 0
    if not config.api_key or config.api_key == 'test-only':
        parser.error('Configured model credential required; no requests sent')
    directory = ROOT / 'output/orchestration-autonomous' / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / 'execution-plan.json', plan)
    bundle = {'plan': plan, 'paid_attempts': 0, 'runs': []}
    for job in plan['execution_order']:
        if execution_plan(config) != plan:
            raise ValueError('Frozen application or evaluation changed during batch')
        stem = f"{job['case_id']}-{job['orchestration']}-r{job['repetition']}-{job['profile']}"
        print(json.dumps({'starting': job, 'directory': str(directory)}), flush=True)
        ledger_path = directory / f'{stem}-requests.json'
        ledger = RequestLedger(path=ledger_path)
        write_json(ledger_path, {'paid_attempts': 0, 'records': []})
        report = capture(job['case_id'], 'multi', lambda _: models_for(config, job, ledger),
            principal, LIMITS, provider_factory=provider_for(job['case_id']),
            investigation_mode='programmatic', knowledge_mode='programmatic',
            blind_review=False, orchestration=job['orchestration'])
        report.update(model=config.model, execution_mode='live', source_version=plan['source_version'], **job)
        path = directory / f'{stem}.json'
        write_json(path, report)
        try:
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
            render_error = None
        except Exception as exc:
            render_error = type(exc).__name__
        entry = {**job, **summarize(report, ledger), 'render_error': render_error,
            'report_file': path.relative_to(ROOT).as_posix(), 'report_sha256': digest(path),
            'requests_file': ledger_path.relative_to(ROOT).as_posix(), 'requests_sha256': digest(ledger_path)}
        bundle['runs'].append(entry)
        bundle['paid_attempts'] += ledger.attempts
        write_json(directory / 'summary.json', bundle)
        print(json.dumps({'completed': job, 'status': entry['status'], 'review_status': entry['review_status'],
            'paid_attempts': ledger.attempts, 'token_usage': entry['recorded_token_usage'],
            'duration_ms': entry['run_summary']['duration_ms']}, ensure_ascii=False), flush=True)
        if execution_plan(config) != plan:
            raise ValueError('Frozen application or evaluation changed during batch')
    print(json.dumps({'summary_file': str(directory / 'summary.json'),
                      'paid_attempts': bundle['paid_attempts']}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
