"""Existing workflow with joins and evidence/budget termination gates."""
from langgraph.graph import StateGraph, START, END
from .harness.runtime import lookup
from .nodes import (bind_agent, intent_node, direct_answer_node, plan_node, web_search_node,
                    local_rag_node, deep_dive_node, analyze_node, reflect_node, verify_node,
                    write_node, finish_node)
from .state import ResearchState


def route_after_intent(state):
    if state.get("termination_reason"):
        return "finish"
    return "direct_answer" if state.get("intent") == "direct" else "plan"


def route_after_plan(state):
    return "finish" if state.get("termination_reason") else ["web_search", "local_rag"]


def route_after_evidence(state):
    return "finish" if state.get("termination_reason") or not state.get("evidence_pool") else "analyze"


def should_continue_research(state):
    if state.get("termination_reason"):
        return "finish"
    context = lookup(state.get("run_id"))
    if (state.get("needs_more_research") and state.get("iteration", 0) < state.get("max_iterations", 1)
            and (not context or context.can_research())):
        return "reflect"
    return "verify" if state.get("findings") else "finish"


def route_after_reflect(state):
    if state.get("termination_reason"):
        return "verify" if state.get("findings") and state["termination_reason"] == "NO_NEW_QUERIES" else "finish"
    return ["web_search", "local_rag"]


def build_app(agents, checkpointer):
    graph = StateGraph(ResearchState)
    for name, fn, role in (
        ("intent", intent_node, "intent_router"), ("direct_answer", direct_answer_node, "direct_responder"),
        ("plan", plan_node, "planner"), ("web_search", web_search_node, "scout_web"),
        ("local_rag", local_rag_node, "scout_local"), ("deep_dive", deep_dive_node, "evidence_judge"),
        ("analyze", analyze_node, "analyst"), ("reflect", reflect_node, "planner"),
        ("verify", verify_node, "evidence_judge"), ("write", write_node, "writer"),
    ):
        graph.add_node(name, bind_agent(fn, getattr(agents, role), role))
    graph.add_node("finish", bind_agent(finish_node, None, "workflow"))
    graph.add_edge(START, "intent")
    graph.add_conditional_edges("intent", route_after_intent, ["finish", "direct_answer", "plan"])
    graph.add_conditional_edges("plan", route_after_plan, ["finish", "web_search", "local_rag"])
    graph.add_edge(["web_search", "local_rag"], "deep_dive")
    graph.add_conditional_edges("deep_dive", route_after_evidence, ["finish", "analyze"])
    graph.add_conditional_edges("analyze", should_continue_research, ["finish", "reflect", "verify"])
    graph.add_conditional_edges("reflect", route_after_reflect, ["finish", "verify", "web_search", "local_rag"])
    graph.add_conditional_edges("verify", lambda s: "write" if s.get("verified_findings") else "finish", ["write", "finish"])
    graph.add_conditional_edges("write", lambda s: "finish" if not s.get("final") else END, ["finish", END])
    graph.add_conditional_edges("direct_answer", lambda s: END if s.get("final") else "finish", [END, "finish"])
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)
