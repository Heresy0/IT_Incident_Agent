"""PostgreSQL authority for one run's reservations, receipts and snapshots.

Only a fenced owner may mutate a running execution. No retry or paid admission
is inferred from the presence of a LangGraph checkpoint.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_VERSION = 'incident_recovery_v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def request_digest(request):
    # JSONB may reorder object fields on restore. Compare the JSON request's
    # meaning while retaining its original wire text for audit.
    value = dict(request)
    if 'messages' in value:
        messages = []
        for message in value['messages']:
            item = dict(message)
            if item.get('type') == 'human' and isinstance(item.get('content'), str):
                try:
                    item['content'] = json.loads(item['content'])
                except ValueError:
                    pass
            messages.append(item)
        value['messages'] = messages
    return digest(value)


def event_digest(event):
    return digest({key: value for key, value in event.items() if key not in {
        'run_id', 'trace_id', 'seq', 'timestamp', 'span_id', 'parent_span_id', 'duration_ms'}})


class RecoveryInterrupted(RuntimeError):
    """Infrastructure interruption: escape model/tool error conversion."""


class RecoveryConflict(RecoveryInterrupted):
    pass


class ResultUnknown(RecoveryInterrupted):
    pass


def storage_boundary(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except psycopg.Error as exc:
            raise RecoveryInterrupted('Recovery storage is unavailable') from exc
    return guarded


def create_execution(conn, run_id, spec):
    conn.execute('INSERT INTO run_execution(run_id,schema_version,spec_sha256,limits) VALUES (%s,%s,%s,%s)',
                 (run_id, SCHEMA_VERSION, digest(spec), Jsonb(spec['limits'])))


class RecoveryStore:
    def __init__(self, store, run_id, epoch):
        self.store, self.pool, self.run_id, self.epoch = store, store.pool, run_id, epoch

    @classmethod
    @contextmanager
    def own(cls, store, run_id):
        # A dedicated connection holds the session lock; never occupy a pooled
        # transaction/connection for the duration of an HTTP request.
        lock_key = int.from_bytes(hashlib.sha256(('incident:' + run_id).encode()).digest()[:8], 'big', signed=True)
        with psycopg.connect(store.dsn, autocommit=True, row_factory=dict_row) as owner:
            locked = owner.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (lock_key,)).fetchone()['acquired']
            if not locked:
                yield None
                return
            try:
                with store.pool.connection() as conn:
                    row = conn.execute("SELECT e.*,r.status FROM run_execution e JOIN research_runs r USING(run_id) WHERE run_id=%s FOR UPDATE OF e,r", (run_id,)).fetchone()
                    if not row or row['status'] != 'running':
                        epoch = None
                    else:
                        row = conn.execute("UPDATE run_execution SET epoch=epoch+1,recoveries=recoveries+CASE WHEN started_at IS NULL THEN 0 ELSE 1 END,started_at=COALESCE(started_at,NOW()),deadline_at=COALESCE(deadline_at,NOW()+(limits->>'seconds')::double precision*INTERVAL '1 second') WHERE run_id=%s RETURNING epoch", (run_id,)).fetchone()
                        epoch = row['epoch']
                yield cls(store, run_id, epoch) if epoch is not None else None
            finally:
                if not owner.closed:
                    owner.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))

    def guard(self, conn):
        row = conn.execute("SELECT e.*,r.status,EXTRACT(EPOCH FROM (clock_timestamp()-e.started_at)) AS elapsed,EXTRACT(EPOCH FROM (e.deadline_at-clock_timestamp())) AS remaining FROM run_execution e JOIN research_runs r USING(run_id) WHERE run_id=%s FOR UPDATE OF e,r", (self.run_id,)).fetchone()
        if not row or row['epoch'] != self.epoch or row['status'] != 'running':
            raise RecoveryConflict('Execution ownership or terminal status changed')
        return row

    @storage_boundary
    def read(self):
        with self.pool.connection() as conn:
            return self.guard(conn)

    @storage_boundary
    def snapshot(self, stage, boundary):
        with self.pool.connection() as conn:
            self.guard(conn)
            row = conn.execute('SELECT payload,sha256 FROM run_snapshots WHERE run_id=%s AND stage=%s AND boundary=%s',
                               (self.run_id, stage, boundary)).fetchone()
        if row and digest(row['payload']) != row['sha256']:
            raise RecoveryConflict('Snapshot digest mismatch')
        return row['payload'] if row else None

    @storage_boundary
    def save_snapshot(self, stage, boundary, payload, clocks):
        with self.pool.connection() as conn:
            self.guard(conn)
            old = conn.execute('SELECT sha256 FROM run_snapshots WHERE run_id=%s AND stage=%s AND boundary=%s',
                               (self.run_id, stage, boundary)).fetchone()
            value_hash = digest(payload)
            if old and old['sha256'] != value_hash:
                raise RecoveryConflict('Immutable snapshot changed')
            conn.execute('INSERT INTO run_snapshots(run_id,stage,boundary,payload,sha256) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (self.run_id, stage, boundary, Jsonb(payload), value_hash))
            conn.execute('UPDATE run_execution SET active_stage=%s,clocks=%s,updated_at=NOW() WHERE run_id=%s',
                         (stage, Jsonb(clocks), self.run_id))
        return value_hash

    @storage_boundary
    def operation(self, stage, ordinal):
        with self.pool.connection() as conn:
            self.guard(conn)
            return conn.execute('SELECT * FROM run_operations WHERE run_id=%s AND stage=%s AND ordinal=%s',
                                (self.run_id, stage, ordinal)).fetchone()

    @storage_boundary
    def reserve(self, stage, ordinal, kind, request, terminal, clocks):
        from runtime.context import ExecutionError
        with self.pool.connection() as conn:
            row = self.guard(conn)
            limits, counts = row['limits'], dict(row['counts'])
            key = 'model_calls' if kind == 'model' else 'tool_calls'
            cap = limits[key] - (limits['reserve_model_calls'] if kind == 'model' and not terminal else 0)
            seconds = 0 if terminal else limits['reserve_seconds']
            if counts[key] >= cap or float(row['remaining']) <= seconds or (kind == 'web' and counts['web_calls'] >= limits['web_calls']):
                raise ExecutionError('BUDGET_EXCEEDED')
            counts[key] += 1
            if kind == 'web':
                counts['web_calls'] += 1
            conn.execute('INSERT INTO run_operations(run_id,stage,ordinal,kind,request_sha256,request,status) VALUES (%s,%s,%s,%s,%s,%s,\'reserved\')',
                         (self.run_id, stage, ordinal, kind, request_digest(request), Jsonb(request)))
            conn.execute('UPDATE run_execution SET counts=%s,clocks=%s,updated_at=NOW() WHERE run_id=%s',
                         (Jsonb(counts), Jsonb(clocks), self.run_id))

    @storage_boundary
    def settle(self, stage, ordinal, response=None, error=None, usage=None):
        with self.pool.connection() as conn:
            row = self.guard(conn)
            changed = conn.execute("UPDATE run_operations SET status=%s,response=%s,error=%s,settled_at=clock_timestamp() WHERE run_id=%s AND stage=%s AND ordinal=%s AND status='reserved' RETURNING ordinal",
                ('failed' if error else 'responded', Jsonb(response), Jsonb(error), self.run_id, stage, ordinal)).fetchone()
            if changed and usage:
                tokens = dict(row['tokens'])
                for key in ('input_tokens', 'output_tokens'):
                    tokens[key] += int(usage[key])
                tokens['known_calls'] += 1
                conn.execute('UPDATE run_execution SET tokens=%s WHERE run_id=%s', (Jsonb(tokens), self.run_id))

    @storage_boundary
    def claim(self, key, owner, maximum=1):
        with self.pool.connection() as conn:
            row = self.guard(conn)
            allowances = dict(row['allowances'])
            owners = list(allowances.get(key, []))
            if owner in owners:
                return True
            if len(owners) >= maximum:
                return False
            allowances[key] = [*owners, owner]
            conn.execute('UPDATE run_execution SET allowances=%s WHERE run_id=%s', (Jsonb(allowances), self.run_id))
            return True

    @storage_boundary
    def emit(self, key, event, clocks):
        with self.pool.connection() as conn:
            self.guard(conn)
            saved = conn.execute('SELECT event FROM research_events WHERE run_id=%s AND event_key=%s', (self.run_id, key)).fetchone()
            if saved:
                if event_digest(saved['event']) != event_digest(event):
                    raise RecoveryConflict('Event cursor diverged from its committed receipt')
                return saved['event'], False
            event = self.store._append(conn, self.run_id, event)
            conn.execute('UPDATE research_events SET event_key=%s WHERE run_id=%s AND seq=%s', (key, self.run_id, event['seq']))
            conn.execute('UPDATE run_execution SET clocks=%s WHERE run_id=%s', (Jsonb(clocks), self.run_id))
            return event, True

    @storage_boundary
    def event_range(self, stage, start, end):
        keys = [f'{stage}/event/{index}' for index in range(start, end)]
        if not keys:
            return []
        with self.pool.connection() as conn:
            self.guard(conn)
            rows = conn.execute('SELECT event_key,event FROM research_events WHERE run_id=%s AND event_key=ANY(%s)',
                                (self.run_id, keys)).fetchall()
        values = {row['event_key']: row['event'] for row in rows}
        if set(values) != set(keys):
            raise RecoveryConflict('Nested operation event receipt missing')
        return [values[key] for key in keys]

    @storage_boundary
    def prepare(self, candidate):
        with self.pool.connection() as conn:
            row = self.guard(conn)
            if row['candidate'] is not None:
                if digest(row['candidate']) != row['candidate_sha256']:
                    raise RecoveryConflict('Report candidate digest mismatch')
                return row['candidate']
            conn.execute('UPDATE run_execution SET candidate=%s,candidate_sha256=%s WHERE run_id=%s',
                         (Jsonb(candidate), digest(candidate), self.run_id))
        return candidate

    @storage_boundary
    def unknown(self, stage, ordinal):
        with self.pool.connection() as conn:
            self.guard(conn)
            conn.execute("UPDATE run_operations SET status='unknown' WHERE run_id=%s AND stage=%s AND ordinal=%s AND status='reserved'", (self.run_id, stage, ordinal))


def serializable_error(exc, *, kind='model'):
    from runtime.context import ExecutionError
    from providers.fixtures import ProviderError
    error_type = ('execution' if isinstance(exc, ExecutionError) else
                  'provider' if isinstance(exc, ProviderError) else 'unexpected')
    if error_type == 'unexpected':
        # Record the caller's public fallback, never a vendor-specific code or
        # exception message. Replay must enter that same generic error handler.
        return {'error_type': error_type,
            'code': 'MODEL_ERROR' if kind == 'model' else 'PROVIDER_UNAVAILABLE',
            'provider': 'model' if kind == 'model' else 'provider', 'retryable': False}
    return {'error_type': error_type, 'code': exc.code,
        'provider': getattr(exc, 'provider', 'provider'), 'retryable': bool(exc.retryable)}


def raise_saved_error(error):
    if error.get('error_type') == 'execution':
        from runtime.context import ExecutionError
        raise ExecutionError(error['code'], error['provider'], error['retryable'])
    if error.get('error_type') == 'provider':
        from providers.fixtures import ProviderError
        raise ProviderError(error['code'], error['retryable'])
    if error.get('error_type') == 'unexpected':
        raise RuntimeError('Previously committed external call failed')
    raise RecoveryConflict('Unknown failure receipt type')
