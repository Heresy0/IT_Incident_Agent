"""Offline M2 checks of explicit saved reports; never invokes a model or a Provider.

Gold is read here only. Mechanical coverage is not a semantic correctness score.
"""
import argparse
import hashlib
import json
from pathlib import Path

from evidence.contracts import Evidence, InvestigationOutput
from agents.investigation import validate_output

ROOT = Path(__file__).resolve().parents[2]


def assess(report, gold):
    summary = report["run_summary"]
    events = report["events"]
    checks = {
        "live": report.get("execution_mode") == "live",
        "completed": report["status"] == "completed" and summary["termination_reason"] == "FINISHED",
        "selection_protocol": report.get("output_protocol") == "reference_selection_v2",
        "small_budget": 0 < summary["model_calls"] <= summary["limits"]["model_calls"] <= 3
            and 0 < summary["tool_calls"] <= summary["limits"]["tool_calls"] <= 6
            and report["repairs"] <= 1,
        "event_sequence": [e["seq"] for e in events] == list(range(1, len(events) + 1))
            and all(e["run_id"] == report["run_id"] for e in events),
        "step_accounting": sum(e["type"] == "call_start" and e["kind"] == "model" for e in events) == summary["model_calls"]
            and sum(e["type"] == "call_start" and e["kind"] == "tool" for e in events) == summary["tool_calls"],
    }
    evidence, hashes, refs_valid = {}, True, False
    try:
        for raw in report["evidence"]:
            item = Evidence.model_validate_json(json.dumps(raw))
            encoded = json.dumps(item.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            expected_id = "EV_" + hashlib.sha256(f"{item.provider}/{item.locator}/{digest}".encode()).hexdigest()[:24]
            hashes = hashes and item.hash == digest and item.excerpt == encoded and item.evidence_id == expected_id
            if item.evidence_id in evidence:
                hashes = False
            evidence[item.evidence_id] = item
    except (ValueError, TypeError, KeyError):
        hashes = False
    try:
        output = InvestigationOutput.model_validate_json(json.dumps(report["output"]))
        validate_output(output, evidence)
        refs_valid = bool(output.findings)
    except (ValueError, TypeError, KeyError):
        refs_valid = False
    checks["snapshot_integrity"] = hashes and bool(evidence)
    checks["references_valid"] = refs_valid
    observed = set()
    for item in evidence.values():
        payload = item.payload
        observed.update(v for k in ("metric", "error_code") if isinstance((v := payload.get(k)), str))
        observed.update(payload.get("config_summary", {}).keys())
    missing = sorted(set(gold["necessary_checks"]) - observed)
    checks["necessary_observations"] = not missing
    results = [e for e in events if e["type"] == "tool_result"]
    # Older reports have no query profiles. Describe only actual returned data in that case.
    path = []
    for event in results:
        fields = sorted({str(evidence[eid].payload[key]) for eid in event["evidence_ids"] if eid in evidence
                         for key in ("metric", "category") if key in evidence[eid].payload})
        path.append({"tool": event["name"], "status": event["status"],
                     "query_profile": event.get("query_profile"), "returned_fields": fields,
                     "truncated": event["truncated"]})
    return {"case_id": report["incident_id"], "run_id": report["run_id"],
            "model": report.get("model"), "prompt_version": report["prompt_version"],
            "checks": checks, "mechanical_pass": all(checks.values()), "missing_checks": missing,
            "observed_path": path, "model_calls": summary["model_calls"], "tool_calls": summary["tool_calls"],
            "token_usage": summary["token_usage"], "duration_ms": summary["duration_ms"],
            "repairs": report["repairs"], "semantic_review": "required",
            "note": "Necessary-observation coverage and valid references do not prove statement support or root cause."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    manifest = json.loads((ROOT / "evals/incident/manifest.json").read_text(encoding="utf-8"))
    results = []
    for path in args.report:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["incident_id"] not in manifest["cases"]:
            parser.error("Unknown development case; no gold path selected.")
        gold = json.loads((ROOT / "evals/incident/gold" / f"{report['incident_id']}.json").read_text(encoding="utf-8"))
        result = assess(report, gold)
        result["report_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        results.append(result)
    all_cases = sorted(r["case_id"] for r in results) == sorted(manifest.get("m2_acceptance_cases", manifest["cases"]))
    profiles_known = all(all(p["query_profile"] is not None for p in r["observed_path"]) for r in results)
    query_paths = [[{"tool": p["tool"], "query_profile": p["query_profile"]} for p in r["observed_path"]] for r in results]
    distinct_paths = len({json.dumps(path, sort_keys=True) for path in query_paths}) == len(results) if profiles_known else None
    mechanical_pass = bool(all_cases and distinct_paths is True and all(r["mechanical_pass"] for r in results))
    output = {"dataset_version": manifest["dataset_version"], "split": "development",
              "all_three_cases": all_cases, "distinct_query_paths": distinct_paths,
              "mechanical_pass": mechanical_pass,
              "acceptance_status": "semantic_review_required" if mechanical_pass
                  else "not_passed", "runs": results}
    encoded = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0 if output["mechanical_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
