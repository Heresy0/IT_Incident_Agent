import json
import re
from dataclasses import dataclass, replace
from threading import Lock
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from mult_agents.graph import build_app
from mult_agents.harness.runtime import RunContext, activate, register, unregister
from mult_agents.state import create_initial_state

WEB = {"title": "模拟方案 A 的部署说明", "url": "https://example.com/plan-a", "snippet": "模拟方案 A 支持私有部署，使用 Python SDK。",
       "source_type": "web", "retrieval_level": "summary_only", "domain": "example.com"}
LOCAL = {"title": "团队需求", "doc_id": "fixtures/team.md", "snippet": "团队有五名研发人员，需要私有部署。",
         "source_type": "local", "retrieval_level": "document_chunk"}


class FakeAgent:
    def __init__(self, role, scenario):
        self.role, self.scenario = role, scenario
        self.calls = 0
        self.lock = Lock()
        self.prompts = []

    def invoke(self, payload):
        with self.lock:
            self.calls += 1
            self.prompts.append(payload["messages"])
        text = "\n".join(str(m.content) for m in payload["messages"])
        # Repair text includes the rejected response; it is not a new source list.
        source_text = text.split("\n待修正输出：", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"(?:WEB|LOC)\d+_\d+-\d+", source_text)))
        role = self.role
        if self.scenario.get("invalid_role") == role:
            return {"messages": [AIMessage(content="invalid JSON") ]}
        if role == "intent_router":
            data = {"route": self.scenario.get("route", "multiagent"), "reason": "fixture"}
        elif role == "planner" and any("逐条返回" in str(m.content) for m in payload["messages"]):
            raise AssertionError("wrong reflection model")
        elif role == "planner" and any("补搜计划" in str(m.content) for m in payload["messages"]):
            data = {"reflection_summary": "fixture", "supplementary_queries": [{"query": self.scenario.get("supplement_query", "新的部署限制资料"), "source_preference": "hybrid"}]}
        elif role == "planner":
            data = {"objective": "fixture", "sub_questions": self.scenario.get("sub_questions", ["部署要求"]),
                    "outline": [{"id": "s1", "search_queries": ["模拟方案的部署说明"]}], "budget": {}}
        elif role in {"scout_web", "scout_local"}:
            prefix = "WEB" if role == "scout_web" else "LOC"
            data = {"summary": "fixture", "evidence": [{"source_id": sid} for sid in ids if sid.startswith(prefix)]}
            if self.scenario.get("scout_reject"):
                data["rejected_source_ids"] = [entry["source_id"] for entry in data["evidence"]]
                data["evidence"] = []
            if self.scenario.get("scout_bad_id") or (self.scenario.get("scout_bad_id_once") and self.calls == 1):
                data["evidence"] = [{"source_id": "WEB9_9-999"}]
            if self.scenario.get("duplicate_selection"):
                data["evidence"] *= 2
            if self.scenario.get("conflicting_selection"):
                data["rejected_source_ids"] = [entry["source_id"] for entry in data["evidence"]]
        elif role == "evidence_judge" and any('"decisions"' in str(m.content) for m in payload["messages"]):
            if self.scenario.get("invalid_verify"):
                return {"messages": [AIMessage(content="invalid JSON")]}
            human = json.loads(payload["messages"][-1].content.split("\n只输出一个 JSON 对象", 1)[0])
            sources = {e["source_id"]: e for e in human["evidence"]}
            data = {"decisions": [{"claim_id": f["claim_id"], "verdict": "unsupported" if self.scenario.get("reject_support") or f["claim_id"] in self.scenario.get("reject_ids", []) else "supported",
                                   "source_ids": self.scenario.get("review_sources", f["source_ids"]), "reason": "fixture",
                                   "evidence_quotes": [{"source_id": sid, "quote": "资料中不存在的引文" if self.scenario.get("bad_quote") or (self.scenario.get("bad_quote_once") and self.calls == 2) else sources[sid]["snippet"]} for sid in self.scenario.get("review_sources", f["source_ids"])]} for f in human["findings"]]}
            data["question_coverage"] = self.scenario.get("review_coverage", [
                {"question_id": task["question_id"], "status": "answered", "claim_ids": [f["claim_id"] for f in human["findings"] if task["question_id"] in f["question_ids"]], "reason": "fixture"}
                for task in human["research_tasks"]])
            data["missing_gaps"] = self.scenario.get("review_gaps", [])
            if self.scenario.get("quote_selector"):
                for decision in data["decisions"]:
                    for quote in decision["evidence_quotes"]:
                        quote["quote_id"] = "fake:excerpt" if self.scenario.get("bad_quote_id") else f"{quote['source_id']}:excerpt"
                        if not self.scenario.get("bad_quote"):
                            quote["quote"] = ""
            if self.scenario.get("duplicate_decisions"):
                data["decisions"] *= 2
        elif role == "evidence_judge":
            data = {"summary": "fixture", "evidence_pool": [{"source_id": sid} for sid in ids if sid not in self.scenario.get("excluded", [])], "audit_flags": []}
            if self.scenario.get("audit_bad_id"):
                data["evidence_pool"] = [{"source_id": "WEB9_9-999"}]
        elif role == "analyst":
            raw = payload["messages"][-1].content.split("\n只输出一个 JSON 对象", 1)[0]
            human = json.loads(raw[raw.index('{"query"'):])
            refs = self.scenario.get("analysis_sources", ids[:1])
            data = {"analysis_summary": "fixture", "needs_more_research": self.scenario.get("needs_more", False), "missing_gaps": self.scenario.get("analysis_gaps", []),
                    "findings": self.scenario.get("findings", [{"claim_id": "c_1", "claim": self.scenario.get("claim", "模拟方案 A 支持私有部署。"), "source_ids": refs, "confidence": "medium",
                                 "kind": self.scenario.get("kind", "fact"), "question_ids": self.scenario.get("question_ids", [t["question_id"] for t in human["research_tasks"]])}])}
            data["question_coverage"] = self.scenario.get("analysis_coverage", [
                {"question_id": task["question_id"], "status": "answered", "claim_ids": [f["claim_id"] for f in data["findings"] if task["question_id"] in f.get("question_ids", [])], "reason": "fixture"}
                for task in human["research_tasks"]])
        elif role == "writer":
            data = {"sections": [{"heading": "核心结论", "claim_ids": ["c_bad" if self.scenario.get("bad_layout") else "c_1"]}]}
        else:
            return {"messages": [AIMessage(content="你好，这是直接回答。", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]}
        return {"messages": [AIMessage(content=json.dumps(data, ensure_ascii=False), usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]}


def bundle(scenario=None):
    scenario = scenario or {}
    roles = ["intent_router", "planner", "scout_web", "scout_local", "evidence_judge", "analyst", "direct_responder", "writer"]
    return SimpleNamespace(**{role: FakeAgent(role, scenario) for role in roles})


def execute(agents, limits=None, max_iterations=0, query="研究模拟方案的部署"):
    events = []
    runtime = RunContext(limits=limits, emit=events.append)
    register(runtime)
    graph = build_app(agents, InMemorySaver())
    state = create_initial_state(query, max_iterations, "test-user", "test-tenant", run_id=runtime.run_id)
    try:
        with activate(runtime):
            result = graph.invoke(state, {"configurable": {"run_id": runtime.run_id, "thread_id": runtime.run_id}})
        return result, runtime, events
    finally:
        unregister(runtime.run_id)


@dataclass(frozen=True)
class TestConfig:
    model: str = "test-model"
    api_key: str = "test-only"
    postgres_dsn: str = ""
    max_iterations: int = 1
    enable_memory: bool = False
    user_id: str = "u"
    thread_id: str = "t"
    tenant_id: str = "tenant"
    memory_top_k: int = 3

    def with_overrides(self, **values):
        return replace(self, **values)


REQUEST = {"query": "研究模拟方案的部署", "user_id": "u", "thread_id": "same-client-thread", "tenant_id": "tenant",
           "max_iterations": 0, "enable_memory": False}
