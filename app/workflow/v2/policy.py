"""Admission quotes for actual remaining stages; RunContext enforces every call."""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class BudgetView:
    models: int
    tools: int
    seconds: float


def remaining(context):
    with context.lock:
        return BudgetView(context.limits.model_calls - context.counts['model_calls'],
                          context.limits.tool_calls - context.counts['tool_calls'],
                          context.limits.seconds - (time.monotonic() - context.started))


def can_explore(context, *, correction_available=True):
    """One decision, one final draft, one review, and optional protocol recovery."""
    view = remaining(context)
    return (view.models >= 3 + int(correction_available) and view.tools > 0
            and view.seconds > context.limits.reserve_seconds)


def can_finalize(context):
    view = remaining(context)
    return view.models > 0 and view.seconds > 0
