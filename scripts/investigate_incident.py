"""Unified v2 CLI; fake controls and explicitly budgeted live use the same engine."""
import argparse
import json
import os
from pathlib import Path
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from api.auth import Principal, get_current_principal
from config.incident import IncidentConfig
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits
from workflow.v2.engine import investigate, render_report
from workflow.v2.scripted import scripted_model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fake', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--workflow', choices=['autonomous_v2', 'collaboration'], default='autonomous_v2',
                        help='collaboration is a compatibility spelling for the v2 closed loop')
    parser.add_argument('--model')
    parser.add_argument('--model-budget', type=int)
    parser.add_argument('--tool-budget', type=int)
    parser.add_argument('--case', choices=[f'case_{n:03d}' for n in range(1, 7)], default='case_001')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.live and (args.model_budget is None or args.tool_budget is None):
        parser.error('Live v2 requires explicit --model-budget and --tool-budget; no request sent.')
    model_budget = args.model_budget if args.model_budget is not None else 16
    tool_budget = args.tool_budget if args.tool_budget is not None else 8
    if not 6 <= model_budget <= 16 or not 1 <= tool_budget <= 24:
        parser.error('Allowance must be model 6-16 / tool 1-24; no request sent.')
    if args.live:
        config = IncidentConfig.from_file()
        if not config.api_key or config.api_key == 'test-only':
            parser.error('A model credential is required; no request sent.')
        token = os.getenv('INCIDENT_BEARER_TOKEN', '')
        if not token:
            parser.error('INCIDENT_BEARER_TOKEN is required; no request sent.')
        try:
            principal = get_current_principal(HTTPAuthorizationCredentials(scheme='Bearer', credentials=token))
        except HTTPException:
            parser.error('Local bearer identity unavailable; no request sent.')
        selected_model = args.model or config.model
    else:
        principal, selected_model = Principal('synthetic_demo', 'cli_reader'), 'scripted'
    try:
        provider = FixtureProvider(args.case, principal)
        if args.live:
            from agents.models import build_roles
            model = build_roles(config.api_key, selected_model, base_url=config.base_url, extra_body=config.extra_body)
        else:
            model = scripted_model(provider)
        result = investigate(model, provider, principal,
            limits=Limits(model_calls=model_budget, tool_calls=tool_budget, reserve_model_calls=3, reserve_seconds=20),
            enable_rework=True, enable_challenge=True)
    except ProviderError:
        parser.error('Case unavailable for this identity; no observation fabricated.')
    result.update(execution_mode='live' if args.live else 'scripted_control_only', model=selected_model)
    result['final'] = render_report(result)
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / (result['run_id'] + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (args.output / (result['run_id'] + '.md')).write_text(result['final'], encoding='utf-8')
    print('Mode: ' + result['execution_mode'] + '; engine: autonomous_v2; variant: closed_loop')
    print(result['final'])
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
