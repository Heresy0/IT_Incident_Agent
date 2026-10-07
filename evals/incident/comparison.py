"""Offline comparison checks and manual review; gold never enters workflow inputs."""
import argparse
import hashlib
import json
from pathlib import Path
from .acceptance import assess, ROOT
from mult_agents.incident.contracts import Evidence, Finding, InvestigationOutput
from mult_agents.incident.collaboration_contracts import DiagnosisDraft
from mult_agents.incident.investigation import validate_output

SCORER_VERSION = "incident-comparison-v1"


def targets(report):
    output = report["output"]
    return [{"target_id": f"{prefix}{n}", "text": row[field], "references": row.get("refs", row.get("support_refs", [])),
             "counter_refs": row.get("counter_refs", []), "supported": None, "reason": ""}
        for prefix, name, field in (("F", "findings", "statement"), ("H", "hypotheses", "cause"), ("A", "recommended_actions", "action"))
        for n, row in enumerate(output[name], 1)]


def score_report(report, gold):
    # Reuse the existing snapshot/hash/field/accounting checker, selecting its common checks.
    projected = {**report, "output": {k: report["output"][k] for k in ("findings", "missing_information", "escalation_team")}}
    legacy = assess(projected, gold)
    checks = {k: legacy["checks"][k] for k in ("event_sequence", "step_accounting", "snapshot_integrity", "references_valid")}
    all_refs_valid = False
    try:
        draft = DiagnosisDraft.model_validate_json(json.dumps(report["output"]))
        evidence = {raw["evidence_id"]: Evidence.model_validate_json(json.dumps(raw)) for raw in report["evidence"]}
        for h in draft.hypotheses:
            if h.support_refs or h.counter_refs:
                validate_output(InvestigationOutput(findings=[Finding(statement="Reference check", refs=[*h.support_refs, *h.counter_refs])]),
                    evidence, ("observation", "runbook", "past_incident"))
        all_refs_valid = set(report["used_evidence_ids"]).issubset(evidence)
    except (ValueError, TypeError, KeyError):
        pass
    summary = report["run_summary"]
    checks.update(diagnosis_contract=all_refs_valid, within_budget=summary["model_calls"] <= summary["limits"]["model_calls"]
        and summary["tool_calls"] <= summary["limits"]["tool_calls"] and report["repairs"] <= 1)
    tools = [e for e in report["events"] if e["type"] == "tool_result"]
    return {"case_id": report["incident_id"], "run_id": report["run_id"], "workflow": report["workflow"],
        "execution_mode": report["execution_mode"], "status": report["status"], "business_result": report["business_result"],
        "review_status": report["review_status"], "mechanical_checks": checks, "mechanical_pass": all(checks.values()),
        "necessary_check_coverage": (len(gold["necessary_checks"])-len(legacy["missing_checks"])) / len(gold["necessary_checks"]),
        "missing_checks": legacy["missing_checks"], "observed_path": legacy["observed_path"],
        "invalid_tool_results": sum(e["status"] == "error" for e in tools), "empty_queries": sum(e["status"] == "empty" for e in tools),
        "truncated_queries": sum(e["truncated"] for e in tools), "rework_rounds": report["rework_rounds"],
        "model_calls": summary["model_calls"], "tool_calls": summary["tool_calls"], "duration_ms": summary["duration_ms"],
        "token_usage": summary["token_usage"], "estimated_cost": None, "termination_reason": summary["termination_reason"],
        "semantic_review": "excluded_scripted" if report["execution_mode"] != "live" else "pending"}


def review_entry(report, report_hash, gold):
    return {"run_id": report["run_id"], "case_id": report["incident_id"], "workflow": report["workflow"],
        "report_sha256": report_hash, "reviewer": "", "approved": False, "cause_correct": None,
        "overconfident": None, "escalation_appropriate": None, "rationale": "",
        "expected_cause": gold["cause"], "necessary_checks": gold["necessary_checks"], "forbidden": gold["forbidden"],
        "claim_assessments": targets(report)}


def summarise(scores, reports, reviews=None):
    reviewed = {}
    if reviews is not None:
        rows = reviews.get("runs", [])
        if len({r["run_id"] for r in rows}) != len(rows):
            raise ValueError("Duplicate manual review run_id")
        for row in rows:
            if not row.get("approved"):
                continue
            run = reports.get(row["run_id"])
            if run is None or run["report"]["execution_mode"] != "live":
                raise ValueError("Only an exact live report can enter semantic scoring")
            if row.get("report_sha256") != run["sha256"] or row.get("case_id") != run["report"]["incident_id"] or row.get("workflow") != run["report"]["workflow"]:
                raise ValueError("Manual review provenance mismatch")
            expected = targets(run["report"])
            assessments = row.get("claim_assessments", [])
            if len(assessments) != len(expected) or {r["target_id"] for r in assessments} != {r["target_id"] for r in expected}:
                raise ValueError("Manual review must cover every original claim/action")
            originals = {r["target_id"]: r for r in expected}
            if any(any(r.get(k) != originals[r["target_id"]][k] for k in ("text", "references", "counter_refs")) for r in assessments):
                raise ValueError("Manual review must retain original claim text/references")
            if not row.get("reviewer", "").strip() or not row.get("rationale", "").strip() \
                    or any(type(row.get(k)) is not bool for k in ("cause_correct", "overconfident", "escalation_appropriate")) \
                    or any(type(r.get("supported")) is not bool or not r.get("reason", "").strip() for r in assessments):
                raise ValueError("Incomplete approved semantic review")
            reviewed[row["run_id"]] = row
    scores = [{**s, 'semantic_review': 'reviewed' if s['run_id'] in reviewed else s['semantic_review']} for s in scores]
    grouped = {}
    for workflow in ("single", "multi"):
        selected = [s for s in scores if s["workflow"] == workflow]
        judged = [reviewed[s["run_id"]] for s in selected if s["run_id"] in reviewed]
        claims = [a for j in judged for a in j["claim_assessments"]]
        def mean(values):
            return sum(values)/len(values) if values else None
        grouped[workflow] = {"runs": len(selected), "mechanical_passes": sum(s["mechanical_pass"] for s in selected),
            "failed_runs": sum(s["status"] == "failed" for s in selected),
            "necessary_check_coverage_mean": mean([s["necessary_check_coverage"] for s in selected]),
            "model_calls_total": sum(s["model_calls"] for s in selected), "tool_calls_total": sum(s["tool_calls"] for s in selected),
            "duration_ms_mean": mean([s["duration_ms"] for s in selected]), "semantic_reviewed_runs": len(judged),
            "live_runs": sum(s["execution_mode"] == "live" for s in selected),
            "cause_accuracy_reviewed": mean([j["cause_correct"] for j in judged]),
            "overconfident_rate_reviewed": mean([j["overconfident"] for j in judged]),
            "escalation_appropriate_rate_reviewed": mean([j["escalation_appropriate"] for j in judged]),
            "claim_support_rate_reviewed": mean([j["supported"] for j in claims]), "estimated_cost": None}
    return {"scorer_version": SCORER_VERSION, "workflows": grouped, "runs": scores,
        "quality_status": "excluded_scripted" if all(s["execution_mode"] != "live" for s in scores) else
            "reviewed" if len(reviewed) == len(scores) else "manual_review_pending",
        "note": "Field coverage and valid source references do not prove semantic support. Scripted results are excluded from business quality; failed/partial runs are retained. Costs remain unknown."}


def evaluate(bundle, directory, reviews=None):
    scores, reports = [], {}
    fingerprints = set()
    known = set(json.loads((ROOT / 'evals/incident/manifest.json').read_text(encoding='utf-8'))['cases'])
    selected = bundle['configuration']['cases']
    if not selected or len(selected) != len(set(selected)) or not set(selected).issubset(known):
        raise ValueError("Unknown or duplicate development case")
    for entry in bundle["runs"]:
        path = directory / entry["report_file"]
        if path.parent.resolve() != directory.resolve() or path.suffix != '.json':
            raise ValueError("Report path must stay in the comparison directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["report_sha256"]:
            raise ValueError("Run report changed since bundle creation")
        report = json.loads(raw)
        if report['incident_id'] not in selected:
            raise ValueError("Unexpected case in report")
        if report["run_id"] in reports:
            raise ValueError("Duplicate report run_id")
        expected = bundle["configuration"]
        if report["workflow"] != entry["workflow"] or report["incident_id"] != entry["case_id"] \
                or report["provenance"] != bundle["provenance"] or report["run_summary"]["limits"] != expected["limits"] \
                or report["execution_mode"] != expected["execution_mode"] or report["model"] != expected["model"]:
            raise ValueError("Comparison configuration/provenance mismatch")
        fingerprints.add((report['incident_id'], report['workflow']))
        gold_path = ROOT / 'evals/incident/gold' / f'{report["incident_id"]}.json'
        if hashlib.sha256(gold_path.read_bytes()).hexdigest() != entry['gold_sha256']:
            raise ValueError("Gold changed since bundle creation")
        gold = json.loads(gold_path.read_text(encoding='utf-8'))
        scores.append(score_report(report, gold))
        reports[report["run_id"]] = {"report": report, "sha256": entry["report_sha256"]}
    required = {(cid, flow) for cid in bundle["configuration"]["cases"] for flow in ('single', 'multi')}
    if fingerprints != required or len(bundle["runs"]) != len(required):
        raise ValueError("Every selected case needs exactly one single/multi run, including failures")
    result = summarise(scores, reports, reviews)
    result.update(configuration=bundle["configuration"], provenance=bundle["provenance"])
    return result, reports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    bundle = json.loads(args.bundle.read_text(encoding='utf-8'))
    reviews = json.loads(args.reviews.read_text(encoding='utf-8')) if args.reviews else None
    result, _ = evaluate(bundle, args.bundle.parent, reviews)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(r['mechanical_pass'] for r in result['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
