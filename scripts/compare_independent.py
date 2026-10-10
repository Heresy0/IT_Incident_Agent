"""Independent pairs: free planning by default; explicit bounded live execution only."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from runtime.context import Limits
from evals.common import implementation_version, write_json
from evals.independent.provider import IndependentProvider
from evals.independent.suite import digest
from scripts.compare_incident import capture


def parse_args(argv=None, *, suite=None):
    if suite is None:
        from evals.independent import suite
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--fake', action='store_true')
    modes.add_argument('--live', action='store_true')
    default_split = getattr(suite, 'DEFAULT_SPLIT', 'validation')
    parser.add_argument('--split', choices=(default_split, 'holdout'), default=default_split)
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--release-holdout', action='store_true')
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--model-budget', type=int)
    parser.add_argument('--tool-budget', type=int)
    parser.add_argument('--model', default='qwen-turbo')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/independent-comparison')
    args = parser.parse_args(argv)
    if args.execute_live and not args.live:
        parser.error('--execute-live requires --live; no request sent')
    if args.live and (not args.execute_live or not args.cases or args.model_budget is None or args.tool_budget is None):
        parser.error('Live requires explicit cases, both budgets and --execute-live; no request sent')
    try:
        args.cases = suite.select_cases(args.split, args.cases, args.release_holdout)
    except ValueError as exc:
        parser.error(str(exc))
    if args.live and len(args.cases) > 2:
        parser.error('At most two cases per live batch; authorize bounded batches separately')
    args.model_budget = 16 if args.model_budget is None else args.model_budget
    args.tool_budget = 8 if args.tool_budget is None else args.tool_budget
    if not 6 <= args.model_budget <= 16 or not 1 <= args.tool_budget <= 24:
        parser.error('Per-run budgets: model 6..16 / tool 1..24; no request sent')
    return parser, args


def main(argv=None, *, suite=None, provider_factory=IndependentProvider, scoring=None):
    if suite is None:
        from evals.independent import suite
    parser, args = parse_args(argv, suite=suite)
    suite_root, data_root = suite.SUITE_ROOT, suite.DATA_ROOT
    lock = suite.verify_freeze()
    if args.check:
        print(json.dumps(suite.check_suite(), ensure_ascii=False, indent=2))
        return 0
    limits = Limits(model_calls=args.model_budget, tool_calls=args.tool_budget,
                    reserve_model_calls=3, reserve_seconds=20)
    config = {'cases': args.cases, 'split': args.split, 'release_holdout': args.release_holdout,
              'limits': asdict(limits), 'model': 'scripted' if args.fake else args.model,
              'execution_mode': 'live' if args.live else 'scripted_control_only' if args.fake else 'plan_only',
              'model_settings': {'temperature': 0, 'max_tokens': 2200, 'max_retries': 1, 'request_timeout': 30}}
    plan = {'plan_only': not (args.live or args.fake), 'dataset_version': lock['dataset_version'],
            'configuration': config, 'paired_runs': 2 * len(args.cases),
            'maximum_model_calls': 2 * len(args.cases) * limits.model_calls,
            'maximum_tool_calls': 2 * len(args.cases) * limits.tool_calls, 'estimated_cost': None,
            'note': '默认免费计划。真实批次最多两个案例；需用户显式授权。合成观测，不访问真实IT服务。'}
    if not args.live and not args.fake:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.live:
        from dotenv import load_dotenv
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials
        from api.auth import get_current_principal
        from providers.fixtures import ProviderError
        load_dotenv(ROOT / '.env.local', override=False)
        key, token = os.getenv('DASHSCOPE_API_KEY', ''), os.getenv('INCIDENT_BEARER_TOKEN', '')
        if not key or key == 'test-only' or not token:
            parser.error('Model key and registered bearer identity required; no request sent')
        try:
            principal = get_current_principal(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
            for cid in args.cases:
                provider = provider_factory(cid, principal)
                provider.authorize(principal, provider.ticket().scope)
        except (HTTPException, ProviderError):
            parser.error('Identity/scope unavailable; no request sent')
        from agents.models import build_model, build_roles
        factory = lambda flow: (build_model if flow == 'single' else build_roles)(key, args.model)
    else:
        principal = Principal('synthetic_demo', 'cli_reader')
    directory = args.output / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    provenance = {'implementation_sha256_prefix': implementation_version(),
        'git_commit': subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        'working_tree_dirty': bool(subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
            capture_output=True, text=True, check=True).stdout.strip()),
        'dataset_version': lock['dataset_version'], 'dataset_freeze_sha256': digest(suite_root / 'freeze.json'),
        'fixture_sha256': {cid: digest(data_root / f'{cid}.json') for cid in args.cases},
        'evaluation_code_sha256': {name: digest(ROOT / name) for name in (*(
            'scripts/compare_incident.py', 'scripts/compare_independent.py', 'evals/independent/provider.py',
            'evals/independent/control.py', 'evals/independent/suite.py', 'evals/independent/scoring.py',
            'evals/incident/comparison.py', 'evals/incident/acceptance.py'),
            *getattr(suite, 'EVALUATION_FILES', ()))}}
    write_json(directory / 'execution-plan.json', {**plan, 'provenance': provenance})
    bundle = {'configuration': config, 'provenance': provenance, 'runs': []}
    # Alternate arm order by neutral case number; report all runs including partial/failure.
    for cid in args.cases:
        if args.fake:
            from evals.independent.control import smoke_factory
            factory = smoke_factory(provider_factory(cid, principal))
        order = ('single', 'multi') if int(cid.rsplit('_', 1)[1]) % 2 else ('multi', 'single')
        for flow in order:
            report = {**capture(cid, flow, factory, principal, limits, provider_factory=provider_factory),
                      'execution_mode': config['execution_mode'], 'model': config['model'], 'provenance': provenance}
            path = directory / f"{report['run_id']}.json"
            write_json(path, report)
            bundle['runs'].append({'case_id': cid, 'workflow': flow, 'report_file': path.name,
                                  'report_sha256': digest(path)})
            # Retain completed attempts even if a later process is interrupted.
            write_json(directory / 'run-index.json', bundle)
    # Reference answers are hashed and opened only AFTER both arms of all selected cases finish.
    for entry in bundle['runs']:
        entry['answer_sha256'] = digest(suite_root / 'answers' / f"{entry['case_id']}.json")
    write_json(directory / 'bundle.json', bundle)
    if scoring is None:
        from evals.independent import scoring
    summary, reports = scoring.evaluate(bundle, directory)
    write_json(directory / 'comparison.json', summary)
    write_json(directory / 'review-template.json', {'rubric_version': suite.read_json(suite_root / 'rubric.json')['version'],
        'runs': [scoring.semantic_template(run['report'], run['sha256']) for _, run in sorted(reports.items())]})
    print(json.dumps({'directory': str(directory), 'quality_status': summary['quality_status'],
                      'workflows': summary['workflows']}, ensure_ascii=False, indent=2))
    # A control run with missing evidence is an expected diagnostic limitation, not a quality pass.
    if args.fake:
        checks = ('event_sequence', 'step_accounting', 'within_budget')
        return 0 if all(all(row['mechanical_checks'][key] for key in checks) for row in summary['runs']) else 2
    return 0 if all(row['mechanical_pass'] for row in summary['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
