"""Paired development comparison. Default planning is free; live needs explicit case/budgets/execution."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT))
from api.auth import Principal
from runtime.context import Limits, RunContext, ExecutionError
from workflow.coordinator import Collaboration
from agents.contracts import DiagnosisDraft
from providers.fixtures import FixtureProvider, ProviderError
from agents.single import single_agent
from tools.executor import ToolExecutor
from evals.common import implementation_version, write_json
from evals.incident.comparison import evaluate, review_entry


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(case_id, workflow, factory, principal, limits):
    """Use one existing harness for both paths; retain accounting/evidence on unexpected failure."""
    provider = FixtureProvider(case_id, principal)
    events = []
    def sink(event):
        events.append({**event, 'seq': len(events) + 1})
    context = RunContext(limits=limits, scope=provider.ticket().scope, emit=sink)
    evidence, executor = {}, None
    try:
        model = factory(workflow)
        if workflow == 'single':
            executor = ToolExecutor(provider, principal, context.scope, context, role='single')
            # The loop's own executor is shared so error reports keep its snapshots too.
            result = single_agent(model, provider, principal, limits=limits, context=context, event_log=events, executor=executor)
        else:
            coordinator = Collaboration(model, provider, principal, limits=limits, context=context, event_log=events)
            evidence = coordinator.evidence
            result = coordinator.run()
    except Exception as exc:
        code = exc.code if isinstance(exc, ExecutionError) else 'INTERNAL_ERROR'
        context.stop_reason = code
        snapshots = executor.evidence if executor else evidence
        result = {'run_id': context.run_id, 'incident_id': case_id, 'task_type': 'incident_' + workflow,
            'scope': context.scope.model_dump(mode='json'), 'data_source': provider.name, 'status': 'failed',
            'review_status': 'not_performed', 'business_result': 'needs_information', 'prompt_version': 'unavailable',
            'output': DiagnosisDraft(findings=[], hypotheses=[], recommended_actions=[],
                missing_information=[code], escalation_team=None).model_dump(mode='json'),
            'evidence': [e.model_dump(mode='json') for e in snapshots.values()], 'used_evidence_ids': [],
            'events': events, 'run_summary': context.summary(), 'repairs': 0, 'rework_rounds': 0,
            'execution_error_type': type(exc).__name__}
    return {**result, 'workflow': workflow}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--fake', action='store_true')
    modes.add_argument('--live', action='store_true')
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--cases', nargs='+', help='Explicit development case IDs; free default selects all six')
    parser.add_argument('--model-budget', type=int)
    parser.add_argument('--tool-budget', type=int)
    parser.add_argument('--model', default='qwen-turbo')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/incident-comparison')
    args = parser.parse_args(argv)
    known = json.loads((ROOT / 'evals/incident/manifest.json').read_text(encoding='utf-8'))['cases']
    if args.execute_live and not args.live:
        parser.error('--execute-live requires --live; no request sent')
    if args.live and (not args.execute_live or not args.cases or args.model_budget is None or args.tool_budget is None):
        parser.error('Live requires --execute-live, explicit --cases and both budgets; no request sent')
    args.cases = args.cases or known
    if len(args.cases) != len(set(args.cases)) or not set(args.cases).issubset(known):
        parser.error('Unknown/duplicate development case; no request sent')
    args.model_budget = 16 if args.model_budget is None else args.model_budget
    args.tool_budget = 8 if args.tool_budget is None else args.tool_budget
    if not 6 <= args.model_budget <= 16 or not 1 <= args.tool_budget <= 24:
        parser.error('Per-run budget must be model 6..16 / tool 1..24; no request sent')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    limits = Limits(model_calls=args.model_budget, tool_calls=args.tool_budget, reserve_model_calls=3, reserve_seconds=20)
    configuration = {'cases': args.cases, 'limits': asdict(limits), 'model': args.model if args.live else 'scripted',
        'execution_mode': 'live' if args.live else 'scripted_control_only',
        'model_settings': {'temperature': 0, 'max_tokens': 2200, 'max_retries': 1, 'request_timeout': 30}}
    if not args.live and not args.fake:
        print(json.dumps({'plan_only': True, 'configuration': configuration, 'paired_runs': 2 * len(args.cases),
            'maximum_model_calls': 2 * len(args.cases) * limits.model_calls,
            'maximum_tool_calls': 2 * len(args.cases) * limits.tool_calls, 'estimated_cost': None,
            'note': 'No model/credential loaded. Live requires explicit case selection, budgets and --execute-live.'}, ensure_ascii=False, indent=2))
        return 0
    if args.live:
        from dotenv import load_dotenv
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials
        from api.auth import get_current_principal
        load_dotenv(ROOT / '.env.local', override=False)
        key, token = os.getenv('DASHSCOPE_API_KEY', ''), os.getenv('INCIDENT_BEARER_TOKEN', '')
        if not key or key == 'test-only' or not token:
            parser.error('This project needs a configured model key and registered bearer identity; no request sent')
        try:
            principal = get_current_principal(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
            for cid in args.cases:
                FixtureProvider(cid, principal)  # Preflight every scope before any model invocation.
        except (HTTPException, ProviderError):
            parser.error('Selected cases unavailable for this identity; no request sent')
        from agents.models import build_model, build_roles
        factory = lambda flow: (build_model if flow == 'single' else build_roles)(key, args.model)
    else:
        from agents.scripted import ScriptedSingle, scripted_models
        principal = Principal('synthetic_demo', 'cli_reader')
        factory = lambda flow: ScriptedSingle() if flow == 'single' else scripted_models()
    directory = args.output / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    provenance = {'implementation_sha256_prefix': implementation_version(),
        'git_commit': subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        'working_tree_dirty': bool(subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()),
        'dataset_version': json.loads((ROOT / 'evals/incident/manifest.json').read_text(encoding='utf-8'))['dataset_version'],
        'fixture_sha256': {cid: sha256(ROOT / 'fixtures/incident' / f'{cid}.json') for cid in args.cases}}
    bundle = {'configuration': configuration, 'provenance': provenance, 'runs': []}
    # Runtime only sees tickets/tool snapshots. Read evaluator gold after all paired runs finish.
    for cid in args.cases:
        for flow in ('single', 'multi'):
            report = {**capture(cid, flow, factory, principal, limits),
                'execution_mode': configuration['execution_mode'], 'model': configuration['model'], 'provenance': provenance}
            path = directory / f"{report['run_id']}.json"
            write_json(path, report)
            bundle['runs'].append({'case_id': cid, 'workflow': flow, 'report_file': path.name, 'report_sha256': sha256(path)})
    for entry in bundle['runs']:
        entry['gold_sha256'] = sha256(ROOT / 'evals/incident/gold' / f"{entry['case_id']}.json")
    write_json(directory / 'bundle.json', bundle)
    summary, reports = evaluate(bundle, directory)
    write_json(directory / 'comparison.json', summary)
    reviews = [review_entry(run['report'], run['sha256'], json.loads(
        (ROOT / 'evals/incident/gold' / f"{run['report']['incident_id']}.json").read_text(encoding='utf-8'))) for run in reports.values()]
    write_json(directory / 'review-template.json', {'runs': reviews})
    print(json.dumps({'directory': str(directory), 'quality_status': summary['quality_status'],
        'workflows': summary['workflows']}, ensure_ascii=False, indent=2))
    return 0 if all(row['mechanical_pass'] for row in summary['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
