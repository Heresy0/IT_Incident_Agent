"""Opt-in experiment: planned program reads versus the existing Investigation agent."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from agents.contracts import DiagnosisDraft
from agents.models import build_roles
from config.incident import IncidentConfig
from evidence.render import render
from evals.common import write_json
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from scripts.run_fresh_validation import FreshProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--cases', nargs='+', choices=('fresh_001', 'fresh_003'), default=['fresh_001', 'fresh_003'])
    parser.add_argument('--arms', nargs='+', choices=('agent', 'programmatic'), default=['agent', 'programmatic'])
    parser.add_argument('--reviewer-repair-mode', choices=('shared', 'split'), default='shared')
    args = parser.parse_args()
    if len(args.cases) != len(set(args.cases)) or len(args.arms) != len(set(args.arms)):
        parser.error('Duplicate cases or arms')
    lock = json.loads((ROOT / 'evals/fresh_validation/freeze.json').read_text(encoding='utf-8'))
    for relative, expected in lock['files'].items():
        path = ROOT / relative
        assert path.resolve().is_relative_to(ROOT.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    config = IncidentConfig.from_file()
    limits = Limits.from_env()
    source = implementation_version()
    plan = {'source_version': source, 'model': config.model, 'cases': args.cases,
            'arms': args.arms, 'reviewer_repair_mode': args.reviewer_repair_mode,
            'limits': asdict(limits), 'attempts_per_arm_case': 1,
            'maximum_model_calls': len(args.cases) * len(args.arms) * limits.model_calls,
            'read_only': True, 'estimated_cost': None,
            'dataset_freeze_sha256': hashlib.sha256((ROOT / 'evals/fresh_validation/freeze.json').read_bytes()).hexdigest()}
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return
    directory = ROOT / 'output/investigation-stage' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    summaries = []
    for case in args.cases:
        for arm in plan['arms']:
            assert implementation_version() == source
            print(json.dumps({'starting': case, 'arm': arm, 'directory': str(directory)}, ensure_ascii=False), flush=True)
            models = build_roles(config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body)
            report = capture(case, 'multi', lambda flow: models, Principal('synthetic_demo', 'cli_reader'), limits,
                             provider_factory=FreshProvider, investigation_mode=arm, reviewer_repair_mode=args.reviewer_repair_mode)
            report.update(source_version=source, model=config.model, execution_mode='live', experiment_arm=arm)
            path = directory / f'{case}-{arm}.json'
            write_json(path, report)
            valid = True
            try:
                DiagnosisDraft.model_validate_json(json.dumps(report['output'], ensure_ascii=False))
                path.with_suffix('.md').write_text(render(report), encoding='utf-8')
            except Exception as exc:
                valid = False
                path.with_suffix('.validation.txt').write_text(type(exc).__name__, encoding='utf-8')
            assert implementation_version() == source
            row = {'case_id': case, 'arm': arm, 'status': report['status'], 'review_status': report['review_status'],
                   'source_version': source, 'contract_valid': valid, 'run_summary': report['run_summary'],
                   'report_file': path.relative_to(ROOT).as_posix(), 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'investigation_model_calls': sum(t['model_calls'] for t in report['task_results'] if t['role'] == 'investigation'),
                   'repair_policy': report['repair_policy'], 'repair_counts': report['repair_counts'],
                   'validation_failures': [{k: e.get(k) for k in ('role', 'reason', 'details')}
                                           for e in report['events'] if e['type'] == 'validation_failure']}
            summaries.append(row)
            write_json(directory / 'summary.json', summaries)
            print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
