"""状态定义模块：声明多智能体工作流共享的 ResearchState 结构。"""

import operator
from typing import Annotated, List
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage


class ResearchState(TypedDict):
    run_id: str
    status: str
    termination_reason: str
    retrieval_errors: Annotated[list[dict], operator.add]
    verified_findings: list[dict]
    research_tasks: list[dict]
    question_coverage: list[dict]
    coverage_stage: str
    verification_rejections: list[dict]
    query: str
    user_id: str
    tenant_id: str
    memory_context: str
    messages: Annotated[List[BaseMessage], operator.add]
    intent: str
    phase: str
    plan: str
    outline: list[dict]
    sub_questions: list[str]
    research_questions: list[str]
    search_plan: list[dict]
    budget: dict
    web_search: str
    local_rag: str
    web_evidence: list[dict]
    local_evidence: list[dict]
    evidence_pool: list[dict]
    deep_dive: str
    audit: str
    audit_flags: list[dict]
    analysis: str
    needs_more_research: bool
    missing_gaps: list[str]
    supplementary_queries: list[dict]
    findings: list[dict]
    claim_map: list[dict]
    source_index: list[dict]
    web_retrieval_stats: dict
    local_retrieval_stats: dict
    web_search_trace: list[dict]
    local_rag_trace: list[dict]
    code: str
    draft: str
    final: str
    iteration: int
    max_iterations: int


def create_initial_state(
    query: str,
    max_iterations: int,
    user_id: str,
    tenant_id: str,
    memory_context: str = "",
    run_id: str = "",
) -> ResearchState:
    return {
        "run_id": run_id,
        "status": "running",
        "termination_reason": "",
        "retrieval_errors": [],
        "verified_findings": [],
        "research_tasks": [],
        "question_coverage": [],
        "coverage_stage": "not_assessed",
        "verification_rejections": [],
        "query": query,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "memory_context": memory_context,
        "messages": [],
        "intent": "",
        "phase": "initialized",
        "plan": "",
        "outline": [],
        "sub_questions": [],
        "research_questions": [],
        "search_plan": [],
        "budget": {},
        "web_search": "",
        "local_rag": "",
        "web_evidence": [],
        "local_evidence": [],
        "evidence_pool": [],
        "deep_dive": "",
        "audit": "",
        "audit_flags": [],
        "analysis": "",
        "needs_more_research": False,
        "missing_gaps": [],
        "supplementary_queries": [],
        "findings": [],
        "claim_map": [],
        "source_index": [],
        "web_retrieval_stats": {},
        "local_retrieval_stats": {},
        "web_search_trace": [],
        "local_rag_trace": [],
        "code": "",
        "draft": "",
        "final": "",
        "iteration": 0,
        "max_iterations": max_iterations,
    }
