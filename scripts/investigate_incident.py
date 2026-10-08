"""CLI: explicit fake/live; M2 probe is 3/6, M3 live requires explicit allowances."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from api.auth import Principal, get_current_principal
from runtime.context import Limits
from providers.fixtures import FixtureProvider, ProviderError
from agents.investigation import investigate, INCIDENT_LIMITS
from evidence.render import render

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fake", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--probe", action="store_true", help="live maximum 3 requests / 6 tool attempts")
    parser.add_argument("--workflow", choices=["investigation", "collaboration"], default="investigation")
    parser.add_argument("--model-budget", type=int, help="explicit M3 live allowance, 6-16")
    parser.add_argument("--tool-budget", type=int, help="explicit M3 live allowance, 1-24")
    parser.add_argument("--case", choices=["case_001", "case_002", "case_003", "case_004", "case_005", "case_006"], default="case_001")
    parser.add_argument("--output", type=Path, help="explicit local report directory (JSON and Markdown)")
    args = parser.parse_args(argv)
    if args.probe and not args.live:
        parser.error("--probe requires --live")
    if args.probe and args.workflow != "investigation":
        parser.error("--probe is the M2 3/6 probe; M3 live needs explicit budgets")
    if args.workflow == "investigation" and (args.model_budget is not None or args.tool_budget is not None):
        parser.error("M2 retains the 3/6 live cap; budget options are only for M3")
    if args.workflow == "collaboration" and args.live:
        if args.model_budget is None or args.tool_budget is None:
            parser.error("M3 live needs explicit --model-budget and --tool-budget; no request sent")
        if not 6 <= args.model_budget <= 16 or not 1 <= args.tool_budget <= 24:
            parser.error("M3 live allowance must be model 6-16 / tool 1-24; no request sent")
    if args.live:
        load_dotenv(ROOT / ".env.local", override=False)
        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key or key == "test-only":
            parser.error("Set DASHSCOPE_API_KEY in this project's .env.local; no request sent.")
        token = os.getenv("INCIDENT_BEARER_TOKEN", "")
        if not token:
            parser.error("Set INCIDENT_BEARER_TOKEN to this project's registered local token; no request sent.")
        try:
            principal = get_current_principal(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        except HTTPException:
            parser.error("Local bearer identity unavailable/invalid; no request sent.")
        from agents.models import build_model, build_roles
        factory = build_roles if args.workflow == "collaboration" else build_model
        model = factory(key, os.getenv("MODEL", "qwen-turbo"))
        # Keep M2's small allowance; M3 uses only explicitly specified bounded allowances.
        limits = Limits(model_calls=args.model_budget, tool_calls=args.tool_budget, reserve_model_calls=3, reserve_seconds=20) \
            if args.workflow == "collaboration" else Limits(model_calls=3, tool_calls=6, reserve_model_calls=0, reserve_seconds=20)
        steps = 3
    else:
        if args.workflow == "collaboration":
            from agents.scripted import scripted_models
            model = scripted_models()
        else:
            if args.case in {'case_004', 'case_005', 'case_006'}:
                from agents.scripted import ScriptedRole
                model = ScriptedRole('investigation')
            else:
                from agents.scripted_investigation import ScriptedModel
                model = ScriptedModel()
        principal, limits, steps = Principal("synthetic_demo", "cli_reader"), INCIDENT_LIMITS, 4
    try:
        provider = FixtureProvider(args.case, principal)
        if args.workflow == "collaboration":
            from workflow.coordinator import collaborate
            result = collaborate(model, provider, principal, limits=limits)
        else:
            result = investigate(model, provider, principal, limits=limits, max_steps=steps)
    except ProviderError:
        parser.error("Case is unavailable for this identity; no observation fabricated.")
    result["execution_mode"] = "live" if args.live else "scripted_control_only"
    result["model"] = os.getenv("MODEL", "qwen-turbo") if args.live else "scripted"
    report = render(result)
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / f"{result['run_id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output / f"{result['run_id']}.md").write_text(report, encoding="utf-8")
    print(f"Mode: {result['execution_mode']}")
    print(report)
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
