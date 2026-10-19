"""Fenced call receipts and bounded reconstruction of an interrupted stage.

A cursor is the stage's operation/event/clock ordinal. Reconstructing a stage
reuses committed responses and historical budget observations; only its unseen
tail may make calls, always checked against the current durable budget/deadline.
No graph retries, extra agents, or model prompt changes are involved.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import time

from langchain_core.messages import AIMessage
from storage.recovery import (RecoveryInterrupted, RecoveryConflict, ResultUnknown,
                              digest, request_digest, serializable_error)


class Journal:
    def __init__(self, backend, context):
        self.backend, self.context = backend, context
        row = backend.read()
        self.base_elapsed, self.base_monotonic = float(row['elapsed']), time.monotonic()
        self.stage_name = None
        self.clocks, self.clock_cursor, self.operation_cursor, self.event_cursor = [], 0, 0, 0
        self.active = None
        self.span_aliases = {}
        self.tool_timestamp = None
        self.run, self.state = None, None
        self.recoveries = row['recoveries']
        context.journal = self

    def fault(self, point):
        callback = getattr(self.backend.store, '_recovery_fault', None)
        if callback:
            callback(point, self)

    def real_elapsed(self):
        return self.base_elapsed + time.monotonic() - self.base_monotonic

    def elapsed(self):
        if self.stage_name is None:
            return self.real_elapsed()
        if self.clock_cursor < len(self.clocks):
            value = self.clocks[self.clock_cursor]
        else:
            value = self.real_elapsed()
            self.clocks.append(value)
        self.clock_cursor += 1
        return value

    def emit(self, event):
        if event.get('type') == 'workflow_completed':
            return True  # Committed with the report and final SSE event.
        if self.stage_name is None:
            return False
        key = f'{self.stage_name}/event/{self.event_cursor}'
        self.event_cursor += 1
        original_span = event.get('span_id')
        event = {**event}
        for field in ('span_id', 'parent_span_id'):
            if field in event:
                event[field] = self.span_aliases.get(event[field], event[field])
        if event.get('type') == 'call_end' and self.active and self.active['saved']:
            receipt = self.active['saved']
            if receipt.get('settled_at') is not None:
                event['duration_ms'] = round((receipt['settled_at'] - receipt['reserved_at']).total_seconds() * 1000)
        saved, created = self.backend.emit(key, event, self.clocks)
        if event.get('type') == 'call_start' and original_span and saved.get('span_id'):
            self.span_aliases[original_span] = saved['span_id']
        if not self.run.events or self.run.events[-1]['seq'] < saved['seq']:
            self.run.events.append(saved)
        if created:
            from observability.telemetry import record_event
            record_event(saved)
        return True

    def reserve(self, kind, terminal):
        self.fault('before_reserve')
        before = dict(self.context.counts)
        self.context._reserve_memory(kind, terminal)
        if not self.active:
            self.context.counts = before
            raise RecoveryConflict('A durable reservation requires an operation cursor')
        if self.active['saved'] is None:
            try:
                self.backend.reserve(self.stage_name, self.active['ordinal'], kind,
                    self.active['request'], terminal, self.clocks)
                self.active['reserved'] = True
                self.fault('after_reserve')
            except BaseException:
                self.context.counts = before
                raise

    @contextmanager
    def operation(self, kind, request):
        parent = self.active
        if parent and (parent['kind'] != 'tool' or kind != 'model' or request.get('name') != 'knowledge_embedding'):
            raise RecoveryConflict('Only a tool-owned embedding may nest a durable operation')
        self.operation_cursor += 1
        ordinal = self.operation_cursor
        row = self.backend.operation(self.stage_name, ordinal)
        if row and (row['kind'] != kind or row['request_sha256'] != request_digest(request)):
            raise RecoveryConflict('Operation cursor/request mismatch')
        if row and row['status'] in {'reserved', 'unknown'}:
            self.backend.unknown(self.stage_name, ordinal)
            raise ResultUnknown(f'{self.stage_name}/{ordinal}: response is unknown; not resent')
        self.active = {'ordinal': ordinal, 'kind': kind, 'request': request, 'saved': row, 'reserved': False}
        try:
            yield
        finally:
            self.active = parent

    def tail(self):
        return {'operation_cursor': self.operation_cursor, 'event_cursor': self.event_cursor,
            'clock_cursor': self.clock_cursor, 'clocks': list(self.clocks),
            'counts': dict(self.context.counts), 'tokens': dict(self.context.tokens),
            'errors': deepcopy(self.context.errors), 'blocked_providers': sorted(self.context.blocked_providers),
            'stop_reason': self.context.stop_reason}

    def restore_tail(self, tail):
        # A cached outer tool result subsumes its embedding child. Reconstruct
        # that child's accounting and committed events without calling RAG/HTTP.
        for event in self.backend.event_range(self.stage_name, self.event_cursor, tail['event_cursor']):
            if not self.run.events or self.run.events[-1]['seq'] < event['seq']:
                self.run.events.append(event)
        if len(self.clocks) < len(tail['clocks']):
            self.clocks = list(tail['clocks'])
        self.operation_cursor, self.event_cursor, self.clock_cursor = (
            tail['operation_cursor'], tail['event_cursor'], tail['clock_cursor'])
        self.context.counts, self.context.tokens = dict(tail['counts']), dict(tail['tokens'])
        self.context.errors = deepcopy(tail['errors'])
        self.context.blocked_providers = set(tail['blocked_providers'])
        self.context.stop_reason = tail['stop_reason']

    def response(self, callback, *, kind):
        active, saved = self.active, self.active['saved']
        if saved:
            envelope = saved['response']
            if envelope is None or 'tail' not in envelope:
                raise RecoveryConflict('Operation response envelope missing')
            self.restore_tail(envelope['tail'])
            if saved['status'] == 'failed':
                error = saved['error']
                if kind in {'tool', 'embedding'}:
                    from providers.fixtures import ProviderError
                    raise ProviderError(error['code'], error['retryable'])
                from runtime.context import ExecutionError
                raise ExecutionError(error['code'], error['provider'], error['retryable'])
            value = envelope['value']
            settled = saved['settled_at']
        else:
            try:
                self.fault('before_external_call')
                result = callback()
            except RecoveryInterrupted:
                raise
            except Exception as exc:
                self.backend.settle(self.stage_name, active['ordinal'],
                    response={'value': None, 'tail': self.tail()}, error=serializable_error(exc))
                raise
            if kind == 'model':
                message = result['messages'][-1]
                if not isinstance(message, AIMessage):
                    raise RecoveryConflict('Only AIMessage responses may be persisted')
                value = message.model_dump(mode='json')
                usage = getattr(message, 'usage_metadata', None) or getattr(message, 'response_metadata', {}).get('token_usage')
                usage = usage if usage and all(key in usage for key in ('input_tokens', 'output_tokens')) else None
            elif kind == 'embedding':
                value = result
                tokens = result.get('usage', {}).get('total_tokens') if isinstance(result, dict) else None
                usage = {'input_tokens': tokens, 'output_tokens': 0} if type(tokens) is int and tokens >= 0 else None
            else:
                from workflow.v2.snapshot import json_value
                value, usage = json_value(result), None
            self.backend.settle(self.stage_name, active['ordinal'],
                response={'value': value, 'tail': self.tail()}, usage=usage)
            settled = self.backend.operation(self.stage_name, active['ordinal'])['settled_at']
            self.fault('after_response_saved')
        if kind == 'model':
            return AIMessage.model_validate(value)
        if kind == 'embedding':
            return value
        self.tool_timestamp = settled
        return value[0], value[1]

    def claim(self, key):
        owner = f'{self.stage_name}/{self.operation_cursor}/{self.event_cursor}'
        return self.backend.claim(key, owner)

    def authoritative_counts(self, *, validate=False):
        row = self.backend.read()
        if validate and self.context.counts != row['counts']:
            raise RecoveryConflict('Reconstructed counts differ from durable reservations')
        self.context.counts, self.context.tokens = dict(row['counts']), dict(row['tokens'])

    def stage(self, name, operation, state, run):
        from workflow.v2.snapshot import capture, restore
        self.run = run
        completed = self.backend.snapshot(name, 'done')
        if completed is not None:
            self.state = restore(completed, run)
            return {'snapshot': name, 'sha256': digest(completed)}
        entry = self.backend.snapshot(name, 'entry')
        if entry is None:
            entry = capture(self.state if self.state is not None else state, run)
            self.backend.save_snapshot(name, 'entry', entry, [])
        self.state = restore(entry, run)
        self.stage_name, self.operation_cursor, self.event_cursor, self.clock_cursor = name, 0, 0, 0
        self.clocks = list(self.backend.read()['clocks'])
        try:
            self.state.update(operation(self.state, type('RuntimeView', (), {'context': run})()))
            self.authoritative_counts(validate=True)
            payload = capture(self.state, run)
            signature = self.backend.save_snapshot(name, 'done', payload, self.clocks)
            self.fault('after_stage_saved')
            return {'snapshot': name, 'sha256': signature}
        except ResultUnknown:
            self.authoritative_counts()
            raise
        finally:
            self.stage_name = None
