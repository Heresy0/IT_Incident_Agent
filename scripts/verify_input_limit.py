"""Three autonomous checks at input48000; all input28000 failures retained."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts.compare_reviewer_recovery import (
    UNSEEN_CASES, CAP_PER_RUN, digest, models_for, IncidentConfig,
    Principal, provider_for, implementation_version,
)
from runtime.context import Limits
from evals.live_batch import run_batch

LIMITS = Limits(model_calls=16, tool_calls=8, seconds=600,
                reserve_model_calls=3, reserve_seconds=90, input_chars=48000)
LOCK = ROOT / 'evals/input_limit/freeze.json'


def execution_plan(config):
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if implementation_version() != lock['source_version'] or config.model != lock['model']:
        raise ValueError('Application or model changed after input-limit freeze')
    files = {**lock['files'], **lock['driver_files']}
    for relative, expected in files.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or digest(path) != expected:
            raise ValueError('Frozen input-limit data or driver changed')
    previous_paid = 0
    for previous in lock['previous_batches']:
        path = ROOT / previous['summary_file']
        if digest(path) != previous['summary_sha256']:
            raise ValueError('Previous failed batch changed')
        summary = json.loads(path.read_text(encoding='utf-8'))
        if len(summary['runs']) != previous['runs']:
            raise ValueError('Previous batch incomplete')
        previous_paid += summary['paid_attempts']
    jobs = [{'case_id': case, 'orchestration': 'review_driven', 'repetition': 1,
             'profile': 'input_limit_48000', 'reviewer_output_tokens': 4800} for case in UNSEEN_CASES]
    maximum = CAP_PER_RUN * len(jobs)
    if previous_paid + maximum > 336:
        raise ValueError('Cumulative paid cap exceeded')
    return {'suite': 'input_limit', 'source_version': implementation_version(),
        'application_commit': lock['application_commit'], 'dataset_lock': digest(LOCK),
        'frozen_file_hashes': files, 'previous_batches': lock['previous_batches'],
        'previous_paid_attempts': previous_paid, 'cumulative_paid_cap': 336,
        'maximum_paid_attempts': maximum, 'maximum_cumulative_paid_attempts': previous_paid + maximum,
        'model': config.model, 'effective_settings_hash': hashlib.sha256(json.dumps(
            {'base_url': config.base_url, 'extra_body': config.extra_body}, sort_keys=True).encode()).hexdigest(),
        'model_settings': {'reviewer_output_tokens': 4800, 'reviewer_timeout': 60,
            'other_output_tokens': 2200, 'other_timeout': 30, 'temperature': 0, 'max_retries': 0},
        'limits': asdict(LIMITS), 'paid_cap_per_run': CAP_PER_RUN, 'execution_order': jobs,
        'seeded_turns': 0, 'replayed_turns': 0, 'synthetic_observations': True,
        'benchmark_type': 'seen_case_input_limit_regression_not_paired',
        'independent_blind_holdout': False, 'independent_quality_score': False,
        'estimated_cost': None, 'assessment': 'Only input limit changed; three predefined checks, '
            'no tuning within batch or retry-until-pass. Earlier failures remain separate.',
        'rubric_defect_preserved': lock['rubric_defect_preserved']}


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
    return run_batch(config, plan, execution_plan, models_for, LIMITS)


if __name__ == '__main__':
    raise SystemExit(main())
