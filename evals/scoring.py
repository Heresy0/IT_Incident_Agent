"""Mechanical checks and separately attributed semantic review. No LLM judge."""
import hashlib
import json
import re

SCORER_VERSION = "evaluation-v2"


def fingerprint(row):
    fields = {k: row.get(k) for k in ("run_id", "attempt", "case", "model", "status", "report",
                                     "verified_findings", "evidence", "summary", "workflow_version", "model_config",
                                     "dataset_version", "fixture_version", "prompt_version", "implementation_version",
                                     "workflow_termination_reason")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def grade(row):
    evidence = row.get("evidence", [])
    ids = [e["source_id"] for e in evidence]
    by_id = {e["source_id"]: e for e in evidence}
    refs = re.findall(r"\[((?:WEB|LOC)[^\]\s]*)\]", row.get("report", ""))
    body = row.get("report", "").split('## 参考资料', 1)[0]
    body_refs = re.findall(r"\[((?:WEB|LOC)[^\]\s]*)\]", body)
    findings = row.get("verified_findings", [])
    violations = []
    if len(ids) != len(set(ids)):
        violations.append("duplicate_evidence_id")
    if any(s not in by_id for s in refs):
        violations.append("unknown_report_citation")
    if findings and not body_refs:
        violations.append("findings_without_report_citations")
    claim_ids = [f["claim_id"] for f in findings]
    if len(claim_ids) != len(set(claim_ids)):
        violations.append("duplicate_claim_id")
    quoted, valid_quotes = 0, 0
    for finding in findings:
        sources = finding.get("source_ids", [])
        if not sources or any(s not in by_id for s in sources):
            violations.append("invalid_claim_source")
        if any(s not in body_refs for s in sources):
            violations.append("uncited_accepted_source")
        quotes = finding.get("support", {}).get("evidence_quotes", [])
        if not quotes:
            violations.append("missing_support_quote")
        for item in quotes:
            quoted += 1
            sid, quote = item.get("source_id"), item.get("quote", "").strip()
            valid = sid in sources and sid in by_id and bool(quote) and quote in by_id[sid].get("snippet", "")
            valid_quotes += int(valid)
            if not valid:
                violations.append("non_verbatim_support_quote")
    summary = row.get("summary", {})
    limits = summary.get("limits", {})
    for name in ("model_calls", "web_calls", "tool_calls"):
        if name not in summary or name not in limits:
            violations.append("missing_budget_measurement")
        elif summary[name] > limits[name] or summary[name] < 0:
            violations.append("call_budget_exceeded")
    if not row.get("report", "").strip():
        violations.append("empty_report")
    if row.get("status") == "failed":
        violations.append("execution_failed")
    behavior = row["case"]["expected_behavior"]
    if behavior == "abstain" and (findings or row.get("status") == "completed"):
        violations.append("no_evidence_case_generated_facts_or_completed")
    claims = " ".join(f["claim"] for f in findings) if findings else row.get("report", "")
    words = row["case"].get("keywords", [])
    return {"automated_pass": not violations, "violations": sorted(set(violations)),
            "citation_id_validity": sum(s in by_id for s in refs) / len(refs) if refs else None,
            "citation_count": len(refs), "valid_citation_count": sum(s in by_id for s in refs),
            "quote_verbatim_rate": valid_quotes / quoted if quoted else None,
            "keyword_coverage_proxy": sum(k.casefold() in claims.casefold() for k in words) / len(words) if words else None,
            "semantic_status": "pending", "semantic_pass": None,
            "requirement_coverage": None, "claim_support_rate": None}


def review_template(artifact):
    return {"schema_version": 1, "evaluation_id": artifact["evaluation_id"],
            "dataset_version": artifact["dataset_version"],
            "rubric": {"requirements": "met=1, partial=0.5, missing=0; assess the FINAL report, not planned questions",
                       "claims": "supported/unsupported/unclear; check negation, numbers, scope and recommendation premises",
                       "report_quality": "check all final text, including unknowns, recommendations and forbidden conclusions"},
            "reviews": [{"run_id": r["run_id"], "fingerprint": fingerprint(r), "review_status": "pending",
                         "reviewer": {"name": "", "kind": "human"},
                         "requirements": [{"id": q["id"], "verdict": None, "reason": ""} for q in r["case"]["requirements"]],
                         "claims": [{"claim_id": f["claim_id"], "verdict": None, "reason": ""} for f in r["verified_findings"]],
                         "factual_correctness": None, "uncertainty_handling": None,
                         "forbidden_conclusion_present": None, "notes": ""} for r in artifact["results"]]}


def apply_reviews(artifact, review_file=None):
    rows = [{**r, "grading": grade(r)} for r in artifact["results"]]
    if review_file is None:
        return rows
    if review_file.get("evaluation_id") != artifact["evaluation_id"] or review_file.get("dataset_version") != artifact["dataset_version"]:
        raise ValueError("Review belongs to another evaluation/dataset")
    by_id = {r["run_id"]: r for r in rows}
    seen = set()
    for review in review_file["reviews"]:
        rid = review["run_id"]
        if rid in seen or rid not in by_id:
            raise ValueError("Duplicate or unknown review run_id")
        seen.add(rid)
        row = by_id[rid]
        if review.get("fingerprint") != fingerprint(row):
            raise ValueError("Review fingerprint does not match the captured result")
        if review["review_status"] == "pending":
            continue
        if review["review_status"] != "complete":
            raise ValueError("Invalid review status")
        reviewer = review["reviewer"]
        if not reviewer.get("name") or reviewer.get("kind") not in {"human", "assistant"}:
            raise ValueError("Reviewer must identify name and human/assistant provenance")
        requirements = review["requirements"]
        claims = review["claims"]
        req_ids = [q["id"] for q in requirements]
        claim_ids = [q["claim_id"] for q in claims]
        if len(req_ids) != len(set(req_ids)) or set(req_ids) != {q["id"] for q in row["case"]["requirements"]}:
            raise ValueError("Review must cover every gold requirement exactly once")
        if len(claim_ids) != len(set(claim_ids)) or set(claim_ids) != {f["claim_id"] for f in row["verified_findings"]}:
            raise ValueError("Review must cover every accepted claim exactly once")
        if any(q["verdict"] not in {"met", "partial", "missing"} or not q.get("reason") for q in requirements):
            raise ValueError("Requirement verdict/reason missing")
        if any(q["verdict"] not in {"supported", "unsupported", "unclear"} or not q.get("reason") for q in claims):
            raise ValueError("Claim verdict/reason missing")
        if review["factual_correctness"] not in {"correct", "incorrect", "unclear"} or review["uncertainty_handling"] not in {"appropriate", "inappropriate", "unclear"}:
            raise ValueError("Report correctness and uncertainty require explicit review")
        if type(review["forbidden_conclusion_present"]) is not bool:
            raise ValueError("Forbidden conclusion check must be explicit")
        coverage = sum({"met": 1, "partial": .5, "missing": 0}[q["verdict"]] for q in requirements) / len(requirements)
        support = sum(q["verdict"] == "supported" for q in claims) / len(claims) if claims else None
        passed = (row["grading"]["automated_pass"] and coverage == 1 and (support is None or support == 1)
                  and review["factual_correctness"] == "correct" and review["uncertainty_handling"] == "appropriate"
                  and not review["forbidden_conclusion_present"])
        row["grading"].update(semantic_status="reviewed", semantic_pass=passed,
                              requirement_coverage=coverage, claim_support_rate=support,
                              reviewer=reviewer, review=review)
    return rows
