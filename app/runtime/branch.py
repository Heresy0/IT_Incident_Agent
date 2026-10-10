"""Local task accounting with atomic reservation against the original run budget."""
import time
from runtime.context import RunContext, ExecutionError


class BranchContext(RunContext):
    def __init__(self, parent, *, model_calls, tool_calls):
        self.parent = parent
        self.events = []
        self.model_cap, self.tool_cap = model_calls, tool_calls
        super().__init__(run_id=parent.run_id, limits=parent.limits, scope=parent.scope)
        self.started, self.root_span = parent.started, parent.root_span

    def emit(self, event):
        event = {'run_id': self.run_id, 'trace_id': self.run_id, 'timestamp': time.time(), **event}
        with self.event_lock:
            self.events.append({**event, 'seq': len(self.events) + 1})
            self.parent.emit(event)

    def reserve(self, kind, terminal=False):
        key = 'model_calls' if kind == 'model' else 'tool_calls'
        cap = self.model_cap if kind == 'model' else self.tool_cap
        with self.lock:
            with self.parent.lock:
                blocked = next((error for error in self.parent.errors if error['provider'] == 'model'
                    and error['code'] in {'AUTH_ERROR', 'QUOTA_EXCEEDED'}), None)
            if kind == 'model' and blocked:
                raise ExecutionError(blocked['code'], 'model')
            if self.counts[key] >= cap:
                self.stop_reason = 'BUDGET_EXCEEDED'
                raise ExecutionError('BUDGET_EXCEEDED')
            # A task never consumes the run's finalization reserve, even if a
            # caller tries to mark a leaf call terminal.
            self.parent.reserve(kind, terminal=False)
            self.counts[key] += 1
            if kind == 'web':
                self.counts['web_calls'] += 1

    def can_research(self):
        with self.lock:
            local = (not self.stop_reason and self.counts['model_calls'] < self.model_cap
                     and self.counts['tool_calls'] < self.tool_cap)
        with self.parent.lock:
            shared = (not self.parent.stop_reason
                and time.monotonic() - self.started < self.limits.seconds - self.limits.reserve_seconds
                and self.parent.counts['model_calls'] < self.limits.model_calls - self.limits.reserve_model_calls
                and self.parent.counts['tool_calls'] < self.limits.tool_calls)
        return local and shared

    def add_usage(self, usage):
        super().add_usage(usage)
        self.parent.add_usage(usage)

    def claim_repair(self, state):
        return self.parent.claim_repair(state)

    def error(self, exc, node=''):
        # Register once in both ledgers; super emits one warning through parent.
        item = {**exc.as_dict(), 'node': node}
        with self.parent.lock:
            if item not in self.parent.errors:
                self.parent.errors.append(item)
            if exc.code in {'AUTH_ERROR', 'QUOTA_EXCEEDED'}:
                self.parent.blocked_providers.add(exc.provider)
        super().error(exc, node)
