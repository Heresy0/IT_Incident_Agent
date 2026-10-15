"""Autonomous paired development benchmark. Plan only unless --execute-live.

Models see only tickets, registered tool observations and their own generated
plans/drafts/reviews. No seeded turns, reference answers or response replay.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from threading import Lock
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from agents.models import build_roles
from api.auth import Principal
from config.incident import IncidentConfig
from evidence.render import render
from evals.common import write_json
from evals.independent.provider import IndependentProvider
from runtime.context import ExecutionError, Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from scripts.run_fresh_validation import FreshProvider

LIMITS = Limits(model_calls=16, tool_calls=8, seconds=360,
                reserve_model_calls=3, reserve_seconds=20)
CASES = ('fresh_001', 'fresh_002', 'fresh_003')
RECOVERY_CASES = (*CASES, 'eval_005', 'eval_008')
CAP_PER_RUN = 16
UNSEEN_CASES = ('closeout_001', 'closeout_002', 'closeout_003')


class CloseoutProvider(IndependentProvider):
    """Only observations, never the separately stored assessment criteria."""
    name = 'closeout_new_synthetic_fixture'
    data_root = ROOT / 'fixtures/incident_closeout_unseen'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provider_for(case):
    if case in UNSEEN_CASES:
        return CloseoutProvider
    return FreshProvider if case in CASES else IndependentProvider


def execution_plan(config, suite='baseline'):
    unseen = suite == 'unseen_closeout'
    lock_path = ROOT / ('evals/closeout_unseen/freeze.json' if unseen else 'evals/fresh_validation/freeze.json')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if unseen and implementation_version() != lock['source_version']:
        raise ValueError('Application changed after unseen-case freeze')
    if unseen and config.model != lock['model']:
        raise ValueError('Model changed after unseen-case freeze')
    files = dict(lock['files'])
    if suite == 'collection_recovery':
        independent = json.loads((ROOT / 'evals/independent/freeze.json').read_text(encoding='utf-8'))
        files.update({path: value for path, value in independent['files'].items() if
            path in {'evals/independent/manifest.json', 'evals/independent/rubric.json',
                     'fixtures/incident_independent/catalog.json'} or
            any(path.endswith(f'{case}.json') for case in RECOVERY_CASES if case.startswith('eval_'))})
    elif suite not in ('baseline', 'unseen_closeout'):
        raise ValueError('Unknown benchmark suite')
    cases = UNSEEN_CASES if unseen else RECOVERY_CASES if suite == 'collection_recovery' else CASES
    for relative, expected in files.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != expected:
            raise ValueError('Frozen benchmark input changed')
    scripts = ('scripts/compare_orchestration_live.py', 'scripts/compare_incident.py',
               'scripts/run_fresh_validation.py', 'evals/independent/provider.py')
    repeats = 3 if unseen else 1
    jobs = [{'case_id': case, 'orchestration': mode, **({'repetition': rep + 1} if unseen else {})}
            for rep in range(repeats) for index, case in enumerate(cases)
            for mode in (('legacy', 'review_driven') if (index + rep) % 2 == 0 else ('review_driven', 'legacy'))]
    return {'suite': suite, 'source_version': implementation_version(), 'script_hashes': {p: digest(ROOT / p) for p in scripts},
        'fixture_and_criteria_hashes': files, 'dataset_lock': digest(lock_path),
        **({'application_commit_before_new_cases': lock['application_commit']} if unseen else
           {'historical_dataset_lock': digest(lock_path)}),
        **({'additional_dataset_lock': digest(ROOT / 'evals/independent/freeze.json')} if suite == 'collection_recovery' else {}),
        'model': config.model, 'effective_settings_hash': hashlib.sha256(json.dumps(
            {'base_url': config.base_url, 'extra_body': config.extra_body}, sort_keys=True).encode()).hexdigest(),
        'model_settings': {'temperature': 0, 'max_output_tokens': 2200, 'max_retries': 0, 'timeout': 30},
        'limits': asdict(LIMITS), 'paid_cap_per_run': CAP_PER_RUN, 'maximum_paid_attempts': CAP_PER_RUN * len(jobs),
        'execution_order': jobs, 'attempts_per_case_arm': repeats, 'synthetic_observations': True,
        'seeded_turns': 0, 'replayed_turns': 0, 'independent_quality_score': False,
        'benchmark_type': 'new_cases_after_application_freeze_author_assessed' if unseen else 'previously_seen_development_cases',
        'independent_blind_holdout': False, 'estimated_cost': None,
        'workflow_settings': {'investigation_mode': 'programmatic', 'knowledge_mode': 'programmatic',
            'blind_review': False, 'reviewer_repair_mode': 'shared', 'review_protection': 'default'},
        'assessment': 'Frozen criteria; author review after every run finishes. Partial may be appropriate. No retry-until-pass.'}


class RequestLedger:
    """Per-run hard cap includes failed invocations; atomic per-response audit."""
    def __init__(self, maximum=CAP_PER_RUN, path=None):
        self.maximum, self.path = maximum, path
        self.attempts, self.records, self.lock = 0, [], Lock()

    def claim(self):
        with self.lock:
            if self.attempts >= self.maximum:
                raise ExecutionError('BUDGET_EXCEEDED')
            self.attempts += 1
            return self.attempts

    def append(self, record):
        with self.lock:
            self.records.append(record)
            if self.path:
                write_json(self.path, {'paid_attempts': self.attempts, 'records': self.records})


def reported_usage(response):
    usage = getattr(response, 'usage_metadata', None) or getattr(response, 'response_metadata', {}).get('token_usage', {})
    # Unknown usage is explicitly null, never a zero-cost response.
    if not isinstance(usage, dict):
        return None
    values = [usage.get('input_tokens', usage.get('prompt_tokens')),
              usage.get('output_tokens', usage.get('completion_tokens'))]
    if not all(type(value) is int and value >= 0 for value in values):
        return None
    return dict(zip(('input_tokens', 'output_tokens'), values))


class LiveRecorder:
    json_output = True
    blind_review = False

    def __init__(self, model, ledger, role):
        self.model, self.ledger, self.role = model, ledger, role

    def invoke(self, messages):
        index = self.ledger.claim()
        started = time.monotonic()
        record = {'attempt': index, 'role': self.role,
                  'input_chars': sum(len(str(m.content)) for m in messages), 'token_usage': None}
        try:
            response = self.model.invoke(messages)
            record.update(content=response.content, token_usage=reported_usage(response),
                          finish_reason=getattr(response, 'response_metadata', {}).get('finish_reason'))
            return response  # Preserve the original usage metadata for RunContext.
        except Exception as exc:
            record['error_code'] = getattr(exc, 'code', type(exc).__name__)
            raise
        finally:
            record['duration_ms'] = round((time.monotonic() - started) * 1000)
            self.ledger.append(record)


def summarize(report, ledger):
    events = report.get('events', [])
    known = [row['token_usage'] for row in ledger.records if row['token_usage'] is not None]
    usage = {key: sum(row[key] for row in known) for key in ('input_tokens', 'output_tokens')}
    if not known:
        usage = {'input_tokens': None, 'output_tokens': None}
    usage.update(known_attempts=len(known), status='known' if len(known) == ledger.attempts else 'partial_or_unknown')
    return {'status': report['status'], 'review_status': report['review_status'], 'stop_reason': report.get('stop_reason'),
        'paid_attempts': ledger.attempts, 'attempts_by_role': dict(Counter(row['role'] for row in ledger.records)),
        'request_errors': [row for row in ledger.records if 'error_code' in row], 'recorded_token_usage': usage,
        'run_summary': report['run_summary'], 'draft_count': len(report.get('drafts', [])),
        'review_count': len(report.get('reviews', [])), 'event_counts': dict(Counter(e['type'] for e in events)),
        'routes': [e for e in events if e['type'] in {'review_routed', 'collection_exit_routed'}],
        'validation_failures': [e for e in events if e['type'] == 'validation_failure'],
        'handoff': report.get('handoff'), 'error': report.get('error'), 'estimated_cost': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--suite', choices=('baseline', 'collection_recovery', 'unseen_closeout'), default='baseline')
    args = parser.parse_args(argv)
    config = IncidentConfig.from_file()
    plan = execution_plan(config, args.suite)
    principal = Principal('synthetic_demo', 'cli_reader')
    for case in dict.fromkeys(j['case_id'] for j in plan['execution_order']):
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
        if execution_plan(config, args.suite) != plan:
            raise ValueError('Implementation or benchmark changed during execution')
        stem = f"{job['case_id']}-{job['orchestration']}"
        if 'repetition' in job:
            stem += f"-r{job['repetition']}"
        print(json.dumps({'starting': job, 'directory': str(directory)}), flush=True)
        ledger_path = directory / f'{stem}-requests.json'
        ledger = RequestLedger(path=ledger_path)
        write_json(ledger_path, {'paid_attempts': 0, 'records': []})
        def factory(_):
            models = build_roles(config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body)
            return {role: LiveRecorder(model, ledger, role) for role, model in models.items()}
        report = capture(job['case_id'], 'multi', factory, principal, LIMITS, provider_factory=provider_for(job['case_id']),
            investigation_mode='programmatic', knowledge_mode='programmatic', blind_review=False,
            orchestration=job['orchestration'])
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
        if execution_plan(config, args.suite) != plan:
            raise ValueError('Implementation or benchmark changed during execution')
    print(json.dumps({'summary_file': str(directory / 'summary.json'), 'paid_attempts': bundle['paid_attempts']}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
