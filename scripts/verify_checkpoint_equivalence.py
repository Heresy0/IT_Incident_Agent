"""Free paired controls for checkpoint-off/on; requires an isolated *_test DB."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time
import statistics
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from providers.bound import catalog
from providers.fixtures import FixtureProvider
from runtime.context import Limits, RunContext
from runtime.journal import Journal
from storage.recovery import RecoveryStore, digest
from storage.runs import PostgresRunStore
from workflow.v2.engine import investigate
from workflow.v2.scripted import scripted_model
from tests.workflow.test_autonomous_closed_engine import ClosedModel


class Recording:
    def __init__(self, delegate):
        self.delegate, self.inputs = delegate, []

    def invoke(self, messages):
        self.inputs.append([{'type': item.type, 'content': item.content} for item in messages])
        return self.delegate.invoke(messages)


def normalize(value, run_id=''):
    if isinstance(value, dict):
        return {key: normalize(item, run_id) for key, item in value.items()
                if key not in {'span_id', 'parent_span_id', 'retrieved_at', 'duration_ms'}}
    if isinstance(value, list):
        return [normalize(item, run_id) for item in value]
    if isinstance(value, str) and run_id:
        return value.replace(run_id, 'paired-control-run')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.environ['INCIDENT_TEST_POSTGRES_DSN']
    if not conninfo_to_dict(dsn).get('dbname', '').endswith('_test'):
        raise ValueError('Only an isolated *_test database may be used')
    args.output.mkdir(parents=True, exist_ok=False)
    logging.disable(logging.CRITICAL)
    original_connect = socket.socket.connect
    def local_only(self, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            raise AssertionError('External network forbidden')
        return original_connect(self, address)
    socket.socket.connect = local_only
    principal = Principal('team_a', 'alice')
    store = PostgresRunStore(dsn)
    store.setup_checkpoints()
    scenarios = []
    for case in range(1, 7):
        for budget in (6, 16):
            for rework, challenge in ((False, False), (True, False), (True, True)):
                scenarios.append((f'case_{case:03d}-b{budget}-r{int(rework)}-c{int(challenge)}',
                    f'case_{case:03d}', budget, rework, challenge, None, False))
    for name, options in (('revision', {}), ('withdraw', {'withdraw': True}),
            ('malformed', {'malformed': True}), ('duplicate', {'duplicate': True}),
            ('challenge', {'primary_pass': True})):
        scenarios.append((name, 'case_001', 16, True, name == 'challenge', options, False))
    scenarios.append(('status-check', 'case_001', 16, True, True, None, True))
    rows = []
    try:
        for name, case, budget, rework, challenge, options, status_check in scenarios:
            limits = Limits(model_calls=budget, tool_calls=8)
            pair = []
            timings = []
            for durable in (False, True):
                run_id = str(uuid4())
                provider = FixtureProvider(case, principal)
                ticket = provider.ticket()
                if status_check:
                    ticket = ticket.model_copy(update={'purpose': 'status_check'})
                model = Recording(ClosedModel(**options) if options is not None else scripted_model(provider))
                events = []
                incident = store.create_incident(catalog(principal)[0]['fields'], principal)
                spec = {'limits': limits.__dict__}
                if durable:
                    spec['recovery'] = {'mode': 'postgres_v1'}
                store.begin_diagnosis(incident['id'], run_id,
                    {'request_key': name, 'execution_mode': 'scripted_control_only'}, principal, {'run_spec': spec})
                context = RunContext(run_id, scope=ticket.scope, limits=limits,
                    emit=lambda event: events.append(store.append(run_id, event)))
                started = time.perf_counter()
                with patch.object(provider, 'ticket', return_value=ticket), patch('time.time', return_value=1700000000.0):
                    if durable:
                        with RecoveryStore.own(store, run_id) as backend, patch.object(Journal, 'real_elapsed', return_value=0.0):
                            Journal(backend, context)
                            result = investigate(model, provider, principal, context=context, event_log=events,
                                enable_rework=rework, enable_challenge=challenge)
                            store.finish(run_id, backend.prepare(result), epoch=backend.epoch)
                            result = store.get(run_id)['result']
                    else:
                        with patch.object(context, 'elapsed', return_value=0.0):
                            result = investigate(model, provider, principal, context=context, event_log=events,
                                enable_rework=rework, enable_challenge=challenge)
                        store.finish(run_id, result)
                        result = store.get(run_id)['result']
                timings.append(round((time.perf_counter() - started) * 1000, 3))
                # Admission is outside the direct engine control. Renumber only
                # the engine events; keep every event and its ordering/content.
                result['events'] = [{**event, 'seq': index + 1} for index, event in enumerate(result['events'])]
                value = normalize({'result': result, 'model_inputs': model.inputs}, run_id)
                path = args.output / (name + ('-on.json' if durable else '-off.json'))
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                pair.append((digest(value), result))
            rows.append({'scenario': name, 'equal': pair[0][0] == pair[1][0],
                'off_sha256': pair[0][0], 'on_sha256': pair[1][0],
                'models': pair[1][1]['run_summary']['model_calls'], 'tools': pair[1][1]['run_summary']['tool_calls'],
                'off_ms': timings[0], 'on_ms': timings[1]})
            print(json.dumps({'scenario': name, 'equal': rows[-1]['equal']}), flush=True)
    finally:
        store.close()
    summary = {'passed': all(row['equal'] for row in rows), 'scenarios': len(rows), 'paid_model_calls': 0,
        'normalization': 'Canonical run ID, zero logical elapsed/fixed event clock; omit random spans, retrieval times and durations; renumber engine event seq after admission. Retain all model input strings, tool parameters, evidence, reviews, reports and budgets.',
        'latency': {mode: {'mean_ms': round(statistics.mean(row[mode+'_ms'] for row in rows), 3),
            'p95_ms': sorted(row[mode+'_ms'] for row in rows)[int(.95*(len(rows)-1))]} for mode in ('off', 'on')},
        'latency_scope': 'Local deterministic model and synthetic provider, both groups persist events and terminal delivery. Admission and schema setup excluded. Does not measure real Flash latency.', 'rows': rows}
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
