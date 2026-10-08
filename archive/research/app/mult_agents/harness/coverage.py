"""Question coverage is derived from linked claims, never from a completion flag."""

from collections import Counter
import re


STATUS_LABELS = {"answered": "已覆盖", "partial": "部分覆盖", "unanswered": "未覆盖"}


def research_questions(state):
    return state.get("research_tasks") or [
        {"question_id": f"q{i}", "question": text}
        for i, text in enumerate(state.get("sub_questions", []), 1)
    ]


def requires_advice(question):
    # Do not let "给出来源引用；不要...采购推荐" turn a factual check into advice.
    for clause in re.split(r"[。！？!?；;，,\n]", question):
        for match in re.finditer(r"给出[^。！？!?；;，,\n]{0,24}(?:建议|推荐)|建议优先|优先验证|选型建议|推荐方案|哪款[^。！？!?；;，,\n]*适合", clause):
            prefix = clause[:match.start()]
            if not re.search(r"(?:不要|不必|无需|不需要|不要求|不用|禁止|勿|不提供|不给出)[^。！？!?；;，,\n]{0,12}$", prefix):
                return True
    return False


def assess_coverage(questions, assessments, findings):
    """Keep the model's semantic assessment only if its claim links survive checks."""
    by_id = {f["claim_id"]: f for f in findings}
    counts = Counter(a["question_id"] for a in assessments)
    by_question = {a["question_id"]: a for a in assessments}
    rows = []
    for question in questions:
        qid = question["question_id"]
        assessment = by_question.get(qid)
        status, claims, reason = "unanswered", [], "未提供可核验的问题覆盖判断。"
        if assessment and counts[qid] == 1:
            requested = list(dict.fromkeys(assessment.get("claim_ids", [])))
            claims = [cid for cid in requested if cid in by_id and qid in by_id[cid].get("question_ids", [])]
            status = assessment["status"]
            reason = assessment.get("reason", "")
            if status == "unanswered":
                claims = []
            elif not claims:
                status, reason = "unanswered", "对应结论缺失、未关联该问题或未通过验证。"
            elif len(claims) < len(requested):
                status, reason = "partial", "部分支撑结论缺失、未关联该问题或未通过验证。"
        elif assessment:
            reason = "同一问题存在重复覆盖判断，无法确认结果。"
        needs_advice = requires_advice(question["question"])
        if status == "answered" and needs_advice and not any(by_id[cid].get("kind") == "recommendation" for cid in claims):
            status, reason = "partial", "缺少对应的已验证建议；背景事实不能代替用户要求的决策建议。"
        rows.append({**question, "status": status, "claim_ids": claims, "reason": reason})
    return rows


def coverage_gaps(rows):
    return [f"研究问题 {r['question_id']}：{r['question']}（{STATUS_LABELS[r['status']]}）；{r['reason']}"
            for r in rows if r["status"] != "answered"]


def final_coverage(state):
    """Never expose an analyst's provisional coverage as a verified result."""
    if state.get("coverage_stage") == "verified":
        return state.get("question_coverage", [])
    return assess_coverage(research_questions(state), [], state.get("verified_findings", []))


def coverage_summary(rows, stage="verified"):
    return {"total": len(rows), **{status: sum(r["status"] == status for r in rows) for status in STATUS_LABELS}, "stage": stage}
