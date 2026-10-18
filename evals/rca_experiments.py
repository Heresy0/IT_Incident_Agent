"""Bounded local program experiments, never human adjudication or incident gold.

These are executable minimal models of three evaluation mechanisms. They do not
retroactively prove the authored observations in rca_v1. In particular, the
pool experiment adds an explicit burst distribution and acquisition deadline
that were absent from the old 30 requests/s, 20 ms fixture.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import sys
from threading import Barrier, Event, Lock, Semaphore, Thread
import time


SCHEMA_VERSION = 'rca-program-experiments-v1'
CASE_IDS = ('001', '019', '025')
POOL_PARAMETERS = {
    'requests_per_window': 30,
    'window_seconds': 1.0,
    'resource_hold_ms': 20,
    'acquisition_timeout_ms': 100,
    'burst_width_ms': 0,
    'pool_capacities': [40, 4, 40],
    'smooth_negative_control_capacity': 4,
}
MUTEX_PARAMETERS = {'acquisition_timeout_ms': 80, 'holder_release': 'after_waiter_returns'}
GATEWAY_PARAMETERS = {'transport': 'local_os_pipe', 'max_payload_bytes': 4096}


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _check(name, passed, observed):
    return {'check': name, 'passed': bool(passed), 'observed': observed}


def _finish(case_id, parameters, trials, checks, limitations):
    return {
        'schema_version': SCHEMA_VERSION,
        'case_id': 'rca_candidate_' + case_id,
        'origin': 'local_controlled_program_execution',
        'human_verified': False,
        'is_gold': False,
        'human_review_status': 'pending',
        'causal_truth_validated': False,
        'status': 'passed' if all(item['passed'] for item in checks) else 'failed',
        'parameters': parameters,
        'trials': trials,
        'checks': checks,
        'limitations': limitations,
    }


class _MeasuredPool:
    """An actual bounded resource pool with observed concurrent occupancy."""
    def __init__(self, capacity):
        self.semaphore = Semaphore(capacity)
        self.guard = Lock()
        self.in_use = 0
        self.waiters = 0
        self.max_in_use = 0

    def use(self, request_id, epoch, hold_seconds, timeout_seconds, arrival_offset):
        arrival = time.perf_counter()
        with self.guard:
            self.waiters += 1
        acquired = self.semaphore.acquire(timeout=timeout_seconds)
        acquired_at = time.perf_counter()
        with self.guard:
            self.waiters -= 1
            if acquired:
                self.in_use += 1
                self.max_in_use = max(self.max_in_use, self.in_use)
            occupancy = self.in_use
            waiters = self.waiters
        hold_started = time.perf_counter()
        if acquired:
            try:
                # Explicit service time in a minimal resource model, not SQL.
                time.sleep(hold_seconds)
            finally:
                with self.guard:
                    self.in_use -= 1
                self.semaphore.release()
        completed = time.perf_counter()
        return {
            'request_id': request_id,
            'scheduled_arrival_ms': arrival_offset * 1000,
            'actual_arrival_ms': (arrival - epoch) * 1000,
            'acquired': acquired,
            'wait_ms': (acquired_at - arrival) * 1000,
            'resource_hold_ms': (completed - hold_started) * 1000 if acquired else 0,
            'completed_at_ms': (completed - epoch) * 1000,
            'observed_in_use_at_acquire_result': occupancy,
            'observed_waiters_at_acquire_result': waiters,
        }


def _pool_trial(capacity, distribution, repeat, phase):
    parameters = POOL_PARAMETERS
    count = parameters['requests_per_window']
    pool = _MeasuredPool(capacity)
    epoch = time.perf_counter()
    if distribution == 'burst':
        barrier = Barrier(count + 1, timeout=5)

        def request(index):
            barrier.wait()
            return pool.use(index, epoch, parameters['resource_hold_ms'] / 1000,
                parameters['acquisition_timeout_ms'] / 1000, 0)

        with ThreadPoolExecutor(max_workers=count) as workers:
            tasks = [workers.submit(request, index) for index in range(count)]
            epoch = time.perf_counter()
            barrier.wait()
            requests = [task.result(timeout=5) for task in tasks]
    elif distribution == 'smooth':
        with ThreadPoolExecutor(max_workers=count) as workers:
            tasks = []
            for index in range(count):
                offset = index * parameters['window_seconds'] / count
                time.sleep(max(0, epoch + offset - time.perf_counter()))
                tasks.append(workers.submit(pool.use, index, epoch,
                    parameters['resource_hold_ms'] / 1000,
                    parameters['acquisition_timeout_ms'] / 1000, offset))
            requests = [task.result(timeout=5) for task in tasks]
    else:
        raise ValueError('Unknown arrival distribution')
    timed_out = sum(not item['acquired'] for item in requests)
    return {'repeat': repeat, 'phase': phase, 'capacity': capacity,
        'distribution': distribution, 'requests': requests,
        'request_count': len(requests), 'acquired': count - timed_out,
        'acquisition_timeouts': timed_out, 'max_observed_in_use': pool.max_in_use,
        'resources_released': pool.in_use == 0 and pool.waiters == 0,
        'wall_seconds': time.perf_counter() - epoch}


def reproduce_pool(repeats=3):
    """Actual burst 40 -> 4 -> 40 intervention plus smooth low-load control."""
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        smooth = _pool_trial(4, 'smooth', repeat, 'smooth_negative_control')
        before = _pool_trial(40, 'burst', repeat, 'before')
        reduced = _pool_trial(4, 'burst', repeat, 'capacity_reduced')
        restored = _pool_trial(40, 'burst', repeat, 'capacity_restored')
        trials.extend([smooth, before, reduced, restored])
        checks.extend([
            _check(f'repeat_{repeat}_smooth_capacity4_no_timeout',
                smooth['acquisition_timeouts'] == 0, smooth['acquisition_timeouts']),
            _check(f'repeat_{repeat}_burst_capacity40_before_no_timeout',
                before['acquisition_timeouts'] == 0, before['acquisition_timeouts']),
            _check(f'repeat_{repeat}_burst_capacity4_timeout',
                reduced['acquisition_timeouts'] > 0, reduced['acquisition_timeouts']),
            _check(f'repeat_{repeat}_burst_capacity40_restored_no_timeout',
                restored['acquisition_timeouts'] == 0, restored['acquisition_timeouts']),
            _check(f'repeat_{repeat}_capacity_and_cleanup', all(
                trial['max_observed_in_use'] <= trial['capacity'] and
                trial['resources_released'] and trial['request_count'] == 30
                for trial in (smooth, before, reduced, restored)),
                [{'phase': trial['phase'], 'max_observed_in_use': trial['max_observed_in_use'],
                  'resources_released': trial['resources_released']}
                 for trial in (smooth, before, reduced, restored)]),
        ])
    return _finish('001', dict(POOL_PARAMETERS), trials, checks, [
        'A local Python semaphore pool and measured sleep model resource occupancy; no real database or production service is exercised.',
        '30 requests per one-second offered-load window is insufficient to characterize burstiness. Smooth load at 30/s and 20ms has mean occupancy about 0.6, below capacity4.',
        'The burst arm releases all30 requests together and uses an explicit100ms acquisition timeout. These conditions were absent from rca_v1 and are new experimental parameters.',
        'This evidence does not validate or rewrite the authored rca_v1 replay row, original timestamps, in_use/waiter counts, or SQL claims.',
        'OS scheduling affects measured timing. Checks retain failures rather than rewriting observations or silently retrying.',
    ])


def _mutex_trial(holder_kind, repeat):
    mutex, ready, release = Lock(), Event(), Event()
    owner = {'holder_kind': holder_kind, 'local_work_iterations': 0}
    errors = []

    def holder():
        try:
            with mutex:
                owner['acquired_at'] = time.perf_counter()
                ready.set()
                if holder_kind == 'awaiting_io_signal':
                    owner['release_signal_observed'] = release.wait(timeout=5)
                elif holder_kind == 'local_maintenance_work':
                    deadline = time.perf_counter() + 5
                    digest = b'local-maintenance'
                    while not release.is_set() and time.perf_counter() < deadline:
                        digest = hashlib.sha256(digest).digest()
                        owner['local_work_iterations'] += 1
                        release.wait(timeout=0.001)
                    owner['work_checksum'] = digest.hex()
                else:
                    raise ValueError('Unknown holder kind')
                owner['released_at'] = time.perf_counter()
        except BaseException as exc:
            errors.append(type(exc).__name__ + ': ' + str(exc))
            ready.set()

    thread = Thread(target=holder, daemon=True)
    thread.start()
    if not ready.wait(timeout=5):
        release.set()
        raise RuntimeError('Mutex owner did not initialize')
    started = time.perf_counter()
    acquired = mutex.acquire(timeout=MUTEX_PARAMETERS['acquisition_timeout_ms'] / 1000)
    returned = time.perf_counter()
    if acquired:
        mutex.release()
    release.set()
    thread.join(timeout=5)
    public = {'request_id': 'q31', 'acquired': acquired,
        'outcome': 'acquired' if acquired else 'local_mutex_acquisition_timeout',
        'configured_deadline_ms': MUTEX_PARAMETERS['acquisition_timeout_ms'],
        'owner_stack_captured': False, 'outbound_attempted': False}
    return {'repeat': repeat, 'holder_kind': holder_kind,
        'public_observation': public,
        'private_execution_trace': {'owner': owner, 'wait_ms': (returned - started) * 1000,
            'errors': errors, 'holder_joined': not thread.is_alive(),
            'mutex_released': not mutex.locked()}}


def reproduce_mutex(repeats=3):
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        baseline = Lock()
        baseline_acquired = baseline.acquire(timeout=0.08)
        if baseline_acquired:
            baseline.release()
        pair = [_mutex_trial(kind, repeat) for kind in
                ('awaiting_io_signal', 'local_maintenance_work')]
        trials.extend(pair)
        checks.extend([
            _check(f'repeat_{repeat}_unheld_baseline_acquires', baseline_acquired,
                {'acquired': baseline_acquired}),
            _check(f'repeat_{repeat}_held_mutex_times_out', all(
                not trial['public_observation']['acquired'] for trial in pair),
                [trial['public_observation']['outcome'] for trial in pair]),
            _check(f'repeat_{repeat}_different_holder_paths_same_available_observation',
                pair[0]['public_observation'] == pair[1]['public_observation'],
                [canonical_sha(trial['public_observation']) for trial in pair]),
            _check(f'repeat_{repeat}_holders_cleanup', all(
                trial['private_execution_trace']['holder_joined'] and
                trial['private_execution_trace']['mutex_released'] and
                not trial['private_execution_trace']['errors'] for trial in pair),
                [trial['private_execution_trace']['holder_joined'] for trial in pair]),
        ])
    return _finish('019', dict(MUTEX_PARAMETERS), trials, checks, [
        'Actual local mutex contention demonstrates the wait mechanism. Owner stack and operation are deliberately outside the public observation contract.',
        'The two real owner paths produce the same available public observation. This constructive ambiguity supports only the mechanism boundary for this experiment.',
        'The experimental80ms deadline differs from the authored2100ms row. No CPU12% value is measured or claimed.',
        'Private traces reveal the experimental holder paths; sending them to the model would change the information boundary.',
        'This does not establish what caused any original incident or authorize a human truth signature.',
    ])


class _PipeTransport:
    """HTTPResponse's stream interface backed by an actual, closed-writer pipe.

    Small bounded writes avoid blocking on pipe capacity. A closed writer makes
    EOF deterministic; Windows socketpair's loopback fallback is not required.
    """
    def __init__(self, payload):
        if len(payload) > GATEWAY_PARAMETERS['max_payload_bytes']:
            raise ValueError('HTTP experiment payload exceeds the bounded pipe limit')
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(write_fd, 'wb') as output:
                output.write(payload)
        except BaseException:
            os.close(read_fd)
            raise
        self.stream = os.fdopen(read_fd, 'rb')

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('The experiment transport is read-only')
        return self.stream

    def close(self):
        self.stream.close()


def _gateway_trial(failure_kind, repeat):
    upstream_client = gateway_client = None
    private = {'failure_kind': failure_kind}
    try:
        if failure_kind == 'upstream_closed_before_headers':
            upstream_payload = b''
        elif failure_kind == 'upstream_malformed_status_line':
            upstream_payload = b'THIS IS NOT AN HTTP STATUS\r\n\r\n'
        else:
            raise ValueError('Unknown gateway failure kind')
        upstream_client = _PipeTransport(upstream_payload)
        try:
            upstream_response = http.client.HTTPResponse(upstream_client)
            upstream_response.begin()
            private['unexpected_upstream_status'] = upstream_response.status
            gateway_status = 200
        except (http.client.HTTPException, OSError) as exc:
            private['observed_exception_type'] = type(exc).__name__
            private['observed_exception_message'] = str(exc)
            gateway_status = 502
        body = b'Bad Gateway' if gateway_status == 502 else b'Unexpected success'
        response = (f'HTTP/1.1 {gateway_status} Bad Gateway\r\n'
            f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
        gateway_client = _PipeTransport(response)
        received = http.client.HTTPResponse(gateway_client)
        received.begin()
        public = {'request_id': 'q37', 'http_status': received.status,
            'body': received.read().decode('ascii'),
            'original_exception_captured': False,
            'downstream_status_captured': False, 'wire_trace_captured': False,
            'change_source_status': 'SOURCE_NOT_CONFIGURED'}
        return {'repeat': repeat, 'public_observation': public,
            'private_execution_trace': private,
            'gateway_response_bytes_hex': response.hex()}
    finally:
        for stream in (upstream_client, gateway_client):
            if stream is not None:
                stream.close()


def reproduce_gateway(repeats=3):
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        pair = [_gateway_trial(kind, repeat) for kind in
            ('upstream_closed_before_headers', 'upstream_malformed_status_line')]
        trials.extend(pair)
        checks.extend([
            _check(f'repeat_{repeat}_real_gateway_client_receives502', all(
                trial['public_observation']['http_status'] == 502 for trial in pair),
                [trial['public_observation']['http_status'] for trial in pair]),
            _check(f'repeat_{repeat}_distinct_actual_upstream_parser_failures',
                pair[0]['private_execution_trace'].get('observed_exception_type') == 'RemoteDisconnected'
                and pair[1]['private_execution_trace'].get('observed_exception_type') == 'BadStatusLine',
                [trial['private_execution_trace'].get('observed_exception_type') for trial in pair]),
            _check(f'repeat_{repeat}_same_available_public_observation',
                pair[0]['public_observation'] == pair[1]['public_observation'],
                [canonical_sha(trial['public_observation']) for trial in pair]),
        ])
    return _finish('025', dict(GATEWAY_PARAMETERS), trials, checks, [
        'Actual OS pipe byte streams and the stdlib HTTP parser execute two distinct upstream failures, then the same generic502 response. No TCP transport is claimed.',
        'Only the configured public gateway projection is indistinguishable; private parser exceptions distinguish the causes and are withheld from the model.',
        'This is an observational-equivalence counterexample within the minimal local gateway. It does not enumerate or prove all production gateway failure modes.',
        'The source-unavailable flag is an experimental observability configuration, not a claim that a real external source was contacted.',
        'No production network, remote dependency, DNS, TLS, database, or original incident is exercised.',
    ])


def run_experiments(output_dir, repeats=3, cases=CASE_IDS):
    """Write append-only experiment evidence and source hashes; no model calls."""
    if not isinstance(repeats, int) or not 1 <= repeats <= 5:
        raise ValueError('repeats must be an integer between1 and5')
    cases = tuple(cases)
    if not cases or len(cases) != len(set(cases)) or set(cases) - set(CASE_IDS):
        raise ValueError('cases must contain unique values from001,019,025')
    folder = Path(output_dir)
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('Experiment output must be absent or empty')
    folder.mkdir(parents=True, exist_ok=True)
    entrypoint = Path(__file__).resolve().parents[1] / 'scripts' / 'reproduce_rca.py'
    source_paths = [Path(__file__).resolve(), entrypoint]
    sources = {str(path): sha_file(path) for path in source_paths}
    started = datetime.now(timezone.utc).isoformat()
    dispatch = {'001': reproduce_pool, '019': reproduce_mutex, '025': reproduce_gateway}
    files, statuses = [], []
    for case_id in cases:
        try:
            result = dispatch[case_id](repeats)
        except Exception as exc:
            result = _finish(case_id, {}, [],
                [_check('program_execution_completed', False,
                    {'exception_type': type(exc).__name__, 'message': str(exc)})],
                ['Experiment execution failed; no successful reproduction is asserted.'])
        result['source_sha256'] = sources
        path = folder / ('experiment_' + case_id + '.json')
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        files.append({'case_id': result['case_id'], 'path': path.name,
            'sha256': sha_file(path), 'status': result['status']})
        statuses.append(result['status'])
    source_unchanged = sources == {str(path): sha_file(path) for path in source_paths}
    manifest = {'schema_version': SCHEMA_VERSION,
        'started_at': started, 'completed_at': datetime.now(timezone.utc).isoformat(),
        'origin': 'local_controlled_program_execution', 'human_verified': False,
        'is_gold': False, 'human_review_status': 'pending', 'model_calls': 0,
        'business_application_imported': False, 'original_incident_reproduced': False,
        'source_sha256': sources, 'source_unchanged_during_run': source_unchanged,
        'python_version': sys.version, 'platform': platform.platform(),
        'repeats': repeats, 'cases': files,
        'status': 'passed' if all(status == 'passed' for status in statuses) and source_unchanged else 'failed',
        'interpretation': 'Measured local-program execution evidence. Human causal adjudication and original-fixture validation remain pending.'}
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False,
        indent=2) + '\n', encoding='utf-8')
    return manifest
