"""Admission quotes for actual remaining stages; RunContext enforces every call.

These helpers never reserve calls or grant a second budget. Optional rework and
protocol correction are quoted only when they become actual pending work.
"""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class BudgetView:
    models: int
    tools: int
    seconds: float


@dataclass(frozen=True)
class StageQuote:
    stages: tuple[str, ...]

    @property
    def models(self):
        return len(self.stages)


def stage_quote(*, phase='initial', enable_challenge=False, correction_needed=False):
    """Quote one materialization and its checks, without speculative retries.

    A known protocol failure may need one correction. An unused correction
    allowance does not consume admission capacity before any failure occurs.
    """
    if phase not in {'initial', 'rework'}:
        raise ValueError('unknown collection phase')
    stages = ['draft' if phase == 'initial' else 'revision', 'review']
    if enable_challenge:
        stages.append('challenge')
    if correction_needed:
        stages.append('protocol_correction')
    return StageQuote(tuple(stages))


def remaining(context):
    with context.lock:
        return BudgetView(context.limits.model_calls - context.counts['model_calls'],
                          context.limits.tool_calls - context.counts['tool_calls'],
                          context.limits.seconds - (time.monotonic() - context.started))


def collection_reservation(context, *, phase='initial', enable_challenge=False,
                           correction_needed=False):
    """Protect the actual remaining stages, keeping one initial read at low caps.

    At a three-call cap the initial read and draft/review may use the whole cap.
    The caller must leave a challenge unassessed if it cannot actually run; this
    exception does not turn a missing check into an approval. Rework receives no
    such exception because an existing draft must retain its complete recheck.
    """
    models = stage_quote(phase=phase, enable_challenge=enable_challenge,
                         correction_needed=correction_needed).models
    if phase == 'initial':
        return min(models, max(0, context.limits.model_calls - 1))
    return models


def collect_stop_reason(context, *, reserve_models, new_reads=0, max_new_reads=None,
                        no_progress=0, max_no_progress=3):
    """Explain an admission boundary without labeling saved capacity exhausted.

    ``new_reads`` counts novel scoped read attempts, including complete empty
    results and provider failures. It is a local work bound, not a claim of new
    observations or semantic progress. Reused/rejected proposals instead count
    towards the independent consecutive ``no_progress`` bound.
    """
    view = remaining(context)
    if view.seconds <= 0:
        return 'TIME_BUDGET_EXCEEDED'
    if view.models <= 0:
        return 'MODEL_BUDGET_EXHAUSTED'
    if max_new_reads is not None and new_reads >= max_new_reads:
        return 'COLLECTION_READ_LIMIT_REACHED'
    if no_progress >= max_no_progress:
        return 'NO_TOOL_PROGRESS'
    if view.tools <= 0:
        return 'TOOL_BUDGET_EXHAUSTED'
    if view.seconds <= context.limits.reserve_seconds:
        return 'COLLECTION_TIME_RESERVED'
    if view.models <= reserve_models:
        return 'COLLECTION_FINALIZATION_RESERVED'
    return None


def can_collect(context, *, reserve_models, new_reads=0, max_new_reads=None,
                no_progress=0, max_no_progress=3):
    return collect_stop_reason(context, reserve_models=reserve_models,
        new_reads=new_reads, max_new_reads=max_new_reads, no_progress=no_progress,
        max_no_progress=max_no_progress) is None


def can_explore(context, *, correction_available=True):
    """One decision, one final draft, one review, and optional protocol recovery."""
    view = remaining(context)
    return (view.models >= 3 + int(correction_available) and view.tools > 0
            and view.seconds > context.limits.reserve_seconds)


def can_finalize(context):
    view = remaining(context)
    return view.models > 0 and view.seconds > 0
