"""CLI: fake is free; --live is the only network path, --probe caps it to 3/6."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from backend.auth import Principal, get_current_principal
from mult_agents.harness.runtime import Limits
from mult_agents.incident.providers import FixtureProvider, ProviderError
from mult_agents.incident.investigation import investigate, INCIDENT_LIMITS
from mult_agents.incident.render import render

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fake", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--probe", action="store_true", help="live maximum 3 requests / 6 tool attempts")
    parser.add_argument("--case", choices=["case_001", "case_002", "case_003"], default="case_001")
    parser.add_argument("--output", type=Path, help="explicit local report directory (JSON and Markdown)")
    args = parser.parse_args(argv)
    if args.probe and not args.live:
        parser.error("--probe requires --live")
    load_dotenv(ROOT / ".env.local", override=False)
    if args.live:
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
        from mult_agents.incident.agents import build_model
        model = build_model(key, os.getenv("MODEL", "qwen-turbo"))
        # Initial live entry always uses the handoff's small allowance. Library defaults
        # retain the full shared budget; expanding live execution requires a later decision.
        limits = Limits(model_calls=3, tool_calls=6, reserve_model_calls=0, reserve_seconds=20)
        steps = 3
    else:
        from mult_agents.incident.fake import ScriptedModel
        principal, model, limits, steps = Principal("synthetic_demo", "cli_reader"), ScriptedModel(), INCIDENT_LIMITS, 4
    try:
        provider = FixtureProvider(args.case, principal)
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
