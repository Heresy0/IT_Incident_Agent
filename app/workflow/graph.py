"""Bounded serial LangGraph, sharing the existing role contracts and execution ledger.

The persisted run/events remain the audit record. This graph does not promise
checkpoint resume: an interrupted worker is still marked failed by RunStore.
"""
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from runtime.context import ExecutionError
from evidence.diagnosis import ProtocolError


class IncidentState(TypedDict, total=False):
    phase: str
    next_node: str
    termination_reason: str
    evidence_ids: list[str]
    task_count: int
    draft_revision: int
    review_count: int
    rework_rounds: int
    counts: dict[str, int]


def build_graph(coordinator):
    """Build one graph per run; every role is a distinct conditional graph node."""
    c = coordinator

    def snapshot():
        return IncidentState(phase=c.phase, next_node='finish' if c.stop else c.next_node,
            termination_reason=c.stop, evidence_ids=list(c.evidence), task_count=len(c.tasks),
            draft_revision=len(c.drafts), review_count=len(c.reviews),
            rework_rounds=c.reworks, counts=dict(c.context.counts))

    def guarded(name, operation):
        def node(state):
            c.event('phase', name, node=name, phase=c.phase)
            try:
                operation()
            except (ExecutionError, ProtocolError) as exc:
                c.context.error(exc if isinstance(exc, ExecutionError) else ExecutionError(exc.code), name)
                if name != 'supervisor' or not c.recover_supervisor(exc):
                    c.stop = exc.code
            return snapshot()
        return node

    def finish(state):
        c.context.stop_reason = c.stop
        c.event('workflow_completed', status=c.status(), reason=c.stop)
        return snapshot()

    graph = StateGraph(IncidentState)
    graph.add_node('supervisor', guarded('supervisor', c.supervise))
    for role in ('investigation', 'knowledge'):
        graph.add_node(role, guarded(role, lambda role=role: c.execute_task(role)))
    graph.add_node('diagnosis', guarded('diagnosis', c.diagnose))
    graph.add_node('reviewer', guarded('reviewer', c.review_draft))
    graph.add_node('finish', finish)
    graph.add_edge(START, 'supervisor')
    routes = {n: n for n in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer', 'finish')}
    for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer'):
        graph.add_conditional_edges(role, lambda state: state['next_node'], routes)
    graph.add_edge('finish', END)
    return graph.compile()
