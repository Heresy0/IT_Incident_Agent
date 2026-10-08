import json
import re
from collections import Counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .runtime import ExecutionError


class Plan(BaseModel):
    objective: str = ""
    sub_questions: list[str] = Field(min_length=1, max_length=5)
    outline: list[dict] = Field(default_factory=list, max_length=6)
    research_questions: list[str] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)


SourceId = Annotated[str, Field(strict=True, min_length=1, max_length=80)]
ShortReason = Annotated[str, Field(strict=True, max_length=160)]


class EvidenceSelection(BaseModel):
    """The model selects a record; source text and locators stay in Python."""
    model_config = ConfigDict(extra="forbid")
    source_id: SourceId
    notes: str = Field(default="", max_length=80)


class EvidenceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="", max_length=160)
    evidence: list[EvidenceSelection] = Field(max_length=10)
    gaps: list[ShortReason] = Field(default_factory=list, max_length=3)
    rejected_source_ids: list[SourceId] = Field(default_factory=list, max_length=10)
    reject_reason: str = Field(default="", max_length=160)


class AuditedSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: SourceId
    reliability_score: float = Field(default=0.5, ge=0, le=1)
    reliability_reason: str = Field(default="", max_length=80)


class AuditFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["low_confidence", "conflict", "missing_evidence"]
    target: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=160)


class Audit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="", max_length=160)
    evidence_pool: list[AuditedSelection] = Field(max_length=10)
    audit_flags: list[AuditFlag] = Field(default_factory=list, max_length=3)


class Finding(BaseModel):
    claim_id: str = Field(min_length=1, max_length=80)
    claim: str = Field(min_length=1, max_length=1200)
    confidence: Literal["high", "medium", "low"] = "low"
    source_ids: list[str] = Field(min_length=1, max_length=6)
    kind: Literal["fact", "inference", "recommendation"] = "fact"
    question_ids: list[str] = Field(default_factory=list, max_length=5)


class QuestionAssessment(BaseModel):
    question_id: str = Field(min_length=1, max_length=40)
    status: Literal["answered", "partial", "unanswered"]
    claim_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(default="", max_length=600)


class Analysis(BaseModel):
    analysis_summary: str = ""
    needs_more_research: bool = Field(default=False, strict=True)
    missing_gaps: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(max_length=12)
    claim_map: list[dict] = Field(default_factory=list)
    question_coverage: list[QuestionAssessment] = Field(default_factory=list, max_length=5)


class Query(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    section_id: str = "gap"
    source_preference: Literal["web", "local", "hybrid"] = "hybrid"
    reason: str = ""


class Reflection(BaseModel):
    reflection_summary: str = ""
    supplementary_queries: list[Query] = Field(max_length=6)


class EvidenceQuote(BaseModel):
    source_id: str
    quote_id: str = Field(default="", max_length=100)
    quote: str = Field(default="", max_length=600)


class SupportDecision(BaseModel):
    claim_id: str
    verdict: Literal["supported", "unsupported", "uncertain"]
    source_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    evidence_quotes: list[EvidenceQuote] = Field(default_factory=list, max_length=6)


class Support(BaseModel):
    decisions: list[SupportDecision] = Field(max_length=12)
    question_coverage: list[QuestionAssessment] = Field(default_factory=list, max_length=5)
    missing_gaps: list[str] = Field(default_factory=list, max_length=8)


class Section(BaseModel):
    heading: Literal["核心结论", "方案比较", "部署与集成", "成本与维护", "风险与限制", "实施建议"]
    claim_ids: list[str] = Field(min_length=1, max_length=12)


class ReportLayout(BaseModel):
    sections: list[Section] = Field(min_length=1, max_length=6)


SCHEMAS = {"intent": None, "plan": Plan, "web_search": EvidenceBatch, "local_rag": EvidenceBatch,
           "deep_dive": Audit, "analyze": Analysis, "reflect": Reflection, "verify": Support, "write": ReportLayout}


def parse_output(text, node):
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value)
    try:
        data = json.loads(value)
        schema = SCHEMAS.get(node)
        if schema:
            return schema.model_validate(data).model_dump()
        if not isinstance(data, dict) or data.get("route") not in {"direct", "multiagent"}:
            raise ValueError("route")
        return data
    except (ValueError, TypeError, ValidationError) as exc:
        raise ExecutionError("MODEL_OUTPUT_INVALID", "model") from exc


def supported_findings(findings, evidence):
    valid = {e["source_id"] for e in evidence if e.get("source_id") and e.get("snippet")}
    counts = Counter(f.get("claim_id") for f in findings)
    accepted, seen = [], set()
    for finding in findings:
        ids = finding.get("source_ids", [])
        claim_id = finding.get("claim_id")
        if claim_id and counts[claim_id] == 1 and claim_id not in seen and ids and set(ids) <= valid and not re.search(r"[?？]", finding.get("claim", "")):
            accepted.append(finding)
            seen.add(claim_id)
    return accepted


def qualifier_violation(finding, evidence):
    """Conservative checks for known failures; not a general entailment validator."""
    claim = finding["claim"]
    source_text = " ".join(evidence[sid]["snippet"] for sid in finding["source_ids"])
    # Trusted fixture provenance is rendered explicitly; ordinary retrieval must retain textual qualifiers.
    simulated = all(evidence[sid].get("is_simulated") is True for sid in finding["source_ids"])
    if not simulated and re.search(r"(?:模拟|示例|假设)(?:方案|套餐|价格|数据)", source_text) and not re.search(r"模拟|示例|假设", claim):
        return "结论遗漏资料中的模拟、示例或假设限定。"
    if finding.get("kind", "fact") == "fact":
        generalizations = [word for word in ("通常", "普遍", "行业标准") if word in claim and word not in source_text]
        if generalizations:
            return "结论将具体资料扩大为通常行为或行业通则。"
    budget_claim = re.search(r"(?:符合|满足|不超过|低于)[^。]{0,30}预算|预算[^。]{0,20}(?:足够|充足|范围内)", claim)
    budget_scope_unknown = re.search(r"(?:未|没有)(?:明确)?说明[^。]{0,60}预算[^。]{0,40}(?:服务器|模型|费用)", source_text)
    if budget_claim and budget_scope_unknown and not re.search(r"无法|不能|尚未|未能|不确定|待确认|待核对", claim):
        return "资料未明确预算是否包含服务器或模型费用，不能断言方案整体满足预算。"
    return ""


def render_report(state, sections=None, limited=False):
    findings = state.get("verified_findings", [])
    by_id = {f["claim_id"]: f for f in findings}
    sections = list(sections or [{"heading": "核心结论", "claim_ids": list(by_id)}])
    selected = {cid for section in sections for cid in section.get("claim_ids", [])}
    omitted = [cid for cid in by_id if cid not in selected]
    if omitted:
        sections.append({"heading": "核心结论", "claim_ids": omitted})
    grouped = {}
    for section in sections:
        grouped.setdefault(section["heading"], []).extend(section.get("claim_ids", []))
    sections = [{"heading": heading, "claim_ids": ids} for heading, ids in grouped.items()]
    lines = ["# 研究结果", "", "本报告依据检索摘要与本地资料片段；网络来源未抓取全文。", ""]
    evidence = {e["source_id"]: e for e in state.get("evidence_pool", [])}
    if any(e.get("is_simulated") is True for e in evidence.values()):
        lines.extend(["本次包含模拟评测资料；模拟结论不描述真实商业产品或市场数据。", ""])
    used, rendered = [], set()
    for section in sections:
        items = [by_id[cid] for cid in section.get("claim_ids", []) if cid in by_id and cid not in rendered]
        if not items:
            continue
        lines.extend([f"## {section['heading']}", ""])
        for finding in items:
            refs = " ".join(f"[{sid}]" for sid in finding["source_ids"])
            label = {"fact": "事实", "inference": "推断", "recommendation": "建议"}.get(finding.get("kind", "fact"), "事实")
            if all(evidence.get(sid, {}).get("is_simulated") is True for sid in finding["source_ids"]):
                label = "模拟" + label
            # Model text cannot insert arbitrary links/citations into deterministic output.
            claim = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", finding["claim"])
            claim = re.sub(r"\[(?:WEB|LOC)[^]]*\]", "", claim).replace("\n", " ")
            lines.append(f"- 【{label}】{claim} {refs}")
            used.extend(finding["source_ids"])
            rendered.add(finding["claim_id"])
        lines.append("")
    from .coverage import final_coverage
    coverage = final_coverage(state)
    if coverage:
        from .coverage import STATUS_LABELS
        lines.extend(["## 研究问题覆盖", ""])
        for row in coverage:
            links = "、".join(row.get("claim_ids", []))
            detail = f"；结论：{links}" if links else ""
            reason = str(row.get("reason", "")).replace("\n", " ")
            lines.append(f"- {row['question_id']} · {row['question']}：{STATUS_LABELS[row['status']]}{detail}。{reason}")
        lines.append("")
    lines.extend(["## 研究限制", ""])
    if not findings:
        lines.append("未取得可支持结论的证据，未生成事实性研究报告。")
    if limited:
        lines.append("本次返回有限结果，尚未完整回答全部研究问题。")
    for gap in state.get("missing_gaps", [])[:8]:
        lines.append(f"- 未覆盖：{gap}")
    for error in state.get("retrieval_errors", []):
        lines.append(f"- {error.get('provider', 'workflow')}：{error.get('message', '执行失败')}")
    if state.get("termination_reason"):
        from .runtime import MESSAGES
        lines.append(f"- 停止原因：{MESSAGES.get(state['termination_reason'], state['termination_reason'])}")
    rejected = state.get("verification_rejections", [])
    if rejected:
        lines.extend(["", "## 复核排除", ""])
        for item in rejected[:12]:
            claim_id = str(item.get("claim_id", "")).replace("\n", " ")
            reason = str(item.get("reason", "")).replace("\n", " ")
            lines.append(f"- 候选结论 {claim_id} 已排除：{reason}")
    lines.extend(["", "## 参考资料", ""])
    lookup = {e["source_id"]: e for e in state.get("evidence_pool", []) if e.get("source_id")}
    for sid in dict.fromkeys(used):
        e = lookup[sid]
        title = str(e.get("title") or sid).replace("\n", " ")
        locator = str(e.get("url") or e.get("doc_id") or "")
        if locator.startswith(("http://", "https://")):
            # Percent-encode characters which would break a Markdown URL.
            locator = locator.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
            lines.append(f"- [{sid}] [{title.replace('[', '').replace(']', '')}]({locator})")
        else:
            lines.append(f"- [{sid}] {title}（本地资料：{locator}）")
    if not used:
        lines.append("- 无已引用来源。")
    return "\n".join(lines)
