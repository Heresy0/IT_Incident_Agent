"""Per-run business state; budgets and role query ledgers keep their own owners.

This is an in-process aggregate, not a checkpoint or a persistence contract.
The Coordinator commits it; parallel workers return outcomes for the join.
"""
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from agents.contracts import DiagnosisDraft, EvidenceNeed, ReviewDecision, TaskRequest
from evidence.contracts import Evidence, EvidenceRef

if TYPE_CHECKING:
    from workflow.collection import ParallelPlan
    from workflow.diagnostic_pack import DiagnosticPack


@dataclass
class ControlState:
    phase: str = 'collect'
    next_node: str = 'supervisor'
    stop: str = ''
    supervisor_calls: int = 0
    reworks: int = 0
    pending: dict[str, Any] | None = None
    additional_rework_authorized: bool = False
    review_follow_up_used: bool = False
    collection_follow_up_used: bool = False
    protocol_fallback_used: bool = False
    last_supervisor_failure: str | None = None
    pure_revisions: int = 0
    scope_revisions: int = 0
    initial_replans: int = 0


@dataclass
class CollectionState:
    tasks: list[dict[str, Any]] = field(default_factory=list)
    reused_requests: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    references: dict[str, EvidenceRef] = field(default_factory=dict)
    seen_tasks: set[tuple] = field(default_factory=set)
    dispatch_queue: list[TaskRequest] = field(default_factory=list)
    dispatch_follow_up: bool = False
    dispatch_previous: set[str] = field(default_factory=set)
    dispatch_reads: set[tuple] = field(default_factory=set)
    parallel_batch: str | None = None
    parallel_plans: dict[str, 'ParallelPlan'] = field(default_factory=dict)
    parallel_batches: int = 0
    collection_exits: list[dict[str, Any]] = field(default_factory=list)
    diagnostic_pack: 'DiagnosticPack | None' = None
    diagnostic_pack_result: dict[str, Any] | None = None
    diagnostic_pack_exit: dict[str, Any] | None = None


@dataclass
class DiagnosisState:
    draft: DiagnosisDraft | None = None
    review: ReviewDecision | None = None
    evidence_needs: dict[str, EvidenceNeed] = field(default_factory=dict)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    observation_reviews: list[dict[str, Any]] = field(default_factory=list)
    deferred_review_requests: list[dict[str, Any]] = field(default_factory=list)
    review_hint_status: list[dict[str, Any]] = field(default_factory=list)
    extra_gaps: list[str] = field(default_factory=list)
    team: str | None = None
    revision_task: Any = None
    pending_revision_requests: tuple = ()
    revision_concerns: dict[str, Any] | None = None
    handoff_reason: str | None = None


@dataclass
class AuditState:
    events: list[dict[str, Any]] = field(default_factory=list)
    negotiation: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    repairs: dict[str, Any] = field(default_factory=lambda: {'repairs': 0})
    workflow_span: str = ''


@dataclass
class WorkflowState:
    control: ControlState = field(default_factory=ControlState)
    collection: CollectionState = field(default_factory=CollectionState)
    diagnosis: DiagnosisState = field(default_factory=DiagnosisState)
    audit: AuditState = field(default_factory=AuditState)

    def graph_projection(self, counts: dict[str, int]) -> dict[str, Any]:
        """Fresh routing summary, borrowing no mutable lists or runtime counts."""
        return {
            'phase': self.control.phase,
            'next_node': 'finish' if self.control.stop else self.control.next_node,
            'termination_reason': self.control.stop,
            'evidence_ids': list(self.collection.evidence),
            'task_count': len(self.collection.tasks),
            'draft_revision': len(self.diagnosis.drafts),
            'review_count': len(self.diagnosis.reviews),
            'rework_rounds': self.control.reworks,
            'counts': dict(counts),
        }


def state_property(group: str, name: str) -> property:
    """Explicit compatibility alias; it never stores a second copy of a field."""
    def read(coordinator):
        return getattr(getattr(coordinator.state, group), name)

    def write(coordinator, value):
        setattr(getattr(coordinator.state, group), name, value)

    return property(read, write, doc=f'Alias of WorkflowState.{group}.{name}.')
