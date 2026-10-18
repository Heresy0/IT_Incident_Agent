"""Frozen Flash four-arm budget experiment; planning and tests never call APIs.

Only the parent owns credentials and paid admission. Every independent worker
imports the same frozen application. The one experimental adapter overrides the
review-rework read ceiling, including the baseline arm, without editing sources.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
from threading import Lock, RLock, Thread, local
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_frozen_versions import (LIMITS, SETTINGS, ModelBroker, ProxyModel,
    WorkerTransport, RPC_PREFIX, application_freeze, fingerprint, normalize_report,
    safe_error, write_json, _loaded_application_paths, _worker_provider, _refs)

ARMS = ('m12_r2', 'm12_r4', 'm16_r2', 'm16_r4')
ROLES = ('investigation', 'diagnosis', 'reviewer', 'challenger')
ARM_CONFIGS = {name: {'limits': {**LIMITS, 'model_calls': calls}, 'max_new_reads': reads}
               for name, calls, reads in (('m12_r2', 12, 2), ('m12_r4', 12, 4),
                                         ('m16_r2', 16, 2), ('m16_r4', 16, 4))}
ADAPTER_POLICY = {'version': 'budget-matrix-rework-reads-v1',
    'target': 'workflow.v2.engine.collect', 'condition': "phase == 'review_rework'",
    'overrides': ['max_new_reads'], 'applied_to_every_arm': True,
    'source_files_modified': False, 'all_other_arguments_preserved': True}
EVALUATION_FILES = ('scripts/evaluate_budget_matrix.py', 'evals/budget_matrix_cases.py',
    'scripts/evaluate_frozen_versions.py', 'scripts/evaluate_autonomous.py',
    'evals/frozen_version_cases.py', 'evals/closed_loop_cases.py')
QUANTITIES = {'requests': 'max_requests', 'input_tokens': 'max_input_tokens',
              'output_tokens': 'max_output_tokens', 'model_seconds': 'max_model_seconds'}
ACCOUNTING_POLICY = {'request_timeout_seconds': 30, 'reservation_seconds_margin': 5,
                     'accounting_seconds_bound': 35, 'changes_api_timeout': False}


def schedule(cases, repeats):
    for repeat in range(1, repeats + 1):
        for index, case_id in enumerate(cases):
            offset = (repeat - 1 + index) % len(ARMS)
            for arm in (*ARMS[offset:], *ARMS[:offset]):
                yield {'case_id': case_id, 'arm': arm, 'repeat': repeat}


def evaluation_freeze():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in EVALUATION_FILES}


class MemoryPaidBudget:
    """One-process admission, durable snapshots, and auditable exact settlements.

    No watcher reads a file while the owner is settling. An already returned API
    response is never replaced by a ledger-write exception. In that event the
    complete in-memory state is saved in the bundle and admission is closed.
    """
    def __init__(self, path, max_calls):
        self.path, self.max_calls = Path(path), max_calls
        self.lock, self.session_requests = RLock(), 0
        self.request_context = local()
        self.state = json.loads(self.path.read_text(encoding='utf-8-sig'))
        for key, cap in QUANTITIES.items():
            if any(not isinstance(self.state.get(field), (int, float)) or
                   not math.isfinite(self.state[field]) for field in (key, cap)) \
                    or self.state[key] < 0 or self.state[cap] <= 0:
                raise ValueError('Invalid paid ledger')
        if not isinstance(self.state.get('unknown_usage_calls'), int) or self.state['unknown_usage_calls'] < 0:
            raise ValueError('Invalid usage status')
        if self.state.get('pending_reservation_ids'):
            raise ValueError('An earlier owner has unresolved paid reservations')
        self.events = deepcopy(self.state.pop('reservation_events', []))
        self.persistence_errors = deepcopy(self.state.pop('persistence_errors', []))
        self.state.pop('pending_reservation_ids', None)
        self.initial = deepcopy(self.state)
        self._initial_event_count = len(self.events)
        self._counter, self._pending = int(self.state['requests']), {}

    def _persist(self):
        value = {**self.state, 'reservation_events': self.events,
                 'pending_reservation_ids': sorted(self._pending),
                 'persistence_errors': self.persistence_errors}
        temporary = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
        try:
            write_json(temporary, value)
            for attempt in range(4):
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep((0.02, 0.08, 0.2)[attempt])
        finally:
            if temporary.exists():
                temporary.unlink()

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def remaining(self):
        with self.lock:
            return {key: max(0, self.state[cap] - self.state[key]) for key, cap in QUANTITIES.items()}

    def reserve(self, input_bound, output_bound, seconds_bound):
        from runtime.context import ExecutionError
        reservation = {'requests': 1, 'input_tokens': input_bound,
                       'output_tokens': output_bound,
                       'model_seconds': seconds_bound + ACCOUNTING_POLICY['reservation_seconds_margin']}
        with self.lock:
            if self.state.get('admission_closed') or self.session_requests >= self.max_calls or any(
                    self.state[key] + amount > self.state[QUANTITIES[key]]
                    for key, amount in reservation.items()):
                raise ExecutionError('BUDGET_EXCEEDED', 'evaluation')
            self._counter += 1
            identifier = self._counter
            reservation['reservation_id'] = identifier
            reservation['request_context'] = deepcopy(getattr(self.request_context, 'value', None))
            self.request_context.reservation_id = identifier
            previous = deepcopy(self.state)
            for key in QUANTITIES:
                self.state[key] += reservation[key]
            self.session_requests += 1
            self._pending[identifier] = deepcopy(reservation)
            self.events.append({'type': 'reserve', **reservation})
            try:
                self._persist()
            except OSError as exc:
                self.state = previous
                self.state['admission_closed'] = True
                self.session_requests -= 1
                self._pending.pop(identifier)
                self.events.append({'type': 'reservation_not_admitted', 'reservation_id': identifier})
                self.persistence_errors.append({'phase': 'reserve', **safe_error(exc)})
                raise ExecutionError('LEDGER_PERSISTENCE_FAILURE', 'evaluation') from None
            return reservation

    def settle(self, reservation, usage, seconds):
        with self.lock:
            identifier = reservation['reservation_id']
            if identifier not in self._pending:
                raise ValueError('Reservation settled twice or unknown')
            known = isinstance(usage, dict) and all(isinstance(usage.get(key), int)
                and not isinstance(usage[key], bool) and usage[key] >= 0
                for key in ('input_tokens', 'output_tokens'))
            if known:
                for key in ('input_tokens', 'output_tokens'):
                    self.state[key] += usage[key] - reservation[key]
                    if usage[key] > reservation[key]:
                        self.state.update(provider_usage_over_reservation=True, admission_closed=True)
            else:
                self.state['unknown_usage_calls'] += 1
            self.state['model_seconds'] += seconds - reservation['model_seconds']
            if seconds > reservation['model_seconds']:
                self.state.update(timeout_contract_exceeded=True, admission_closed=True)
            self._pending.pop(identifier)
            self.events.append({'type': 'settle', 'reservation_id': identifier,
                'known_usage': known, 'usage': deepcopy(usage) if known else None,
                'seconds': seconds, 'unknown_retains_token_reservation': not known})
            try:
                self._persist()
            except OSError as exc:
                self.state['admission_closed'] = True
                self.persistence_errors.append({'phase': 'settle', **safe_error(exc)})

    def summary(self):
        with self.lock:
            return {'session_requests': self.session_requests,
                'charged_delta': {key: self.state[key] - self.initial[key] for key in QUANTITIES},
                'unknown_usage_calls_delta': self.state['unknown_usage_calls'] - self.initial['unknown_usage_calls'],
                'remaining': self.remaining(), 'token_accounting': 'known_or_conservative_upper_bound',
                'single_process_ledger': True, 'admission_closed': self.state.get('admission_closed', False),
                'initial': deepcopy(self.initial), 'final': deepcopy(self.state),
                'reservation_events': deepcopy(self.events), 'pending_reservation_ids': sorted(self._pending),
                'session_reservation_events': deepcopy(self.events[self._initial_event_count:]),
                'persistence_errors': deepcopy(self.persistence_errors)}


class MatrixBroker(ModelBroker):
    def __init__(self, model, budget, model_name, *, run_identity=None):
        super().__init__(model, budget, model_name)
        self.decision_trace, self.run_identity = [], deepcopy(run_identity)

    def handle(self, request):
        if self.budget is not None:
            self.budget.request_context.value = {**(self.run_identity or {}),
                'request_id': request['request_id'], 'role': request['role']}
            self.budget.request_context.reservation_id = None
        result = super().handle(request)
        if self.budget is not None:
            with self.lock:
                row = next(row for row in self.calls if row['request_id'] == request['request_id'])
                row['reservation_id'] = self.budget.request_context.reservation_id if row['charged_request'] else None
            self.budget.request_context.value = None
        response = result.get('response', {})
        try:
            payload = json.loads(request['messages'][-1]['data']['content'])
            stage = payload.get('stage')
        except (ValueError, TypeError, KeyError, IndexError):
            payload, stage = {}, None
        if stage == 'investigate':
            raw = response.get('content')
            try:
                decision = json.loads(raw) if isinstance(raw, str) else None
            except (ValueError, TypeError):
                decision = None
            data = payload.get('input', {})
            feedback = data.get('review_feedback')
            row = {'request_id': request['request_id'], 'role': request['role'], 'stage': stage,
                'is_collection_decision': True,
                'phase': 'review_rework' if feedback is not None else 'collect',
                'prompt_sha256': fingerprint(request['messages'][0]), 'step_json': decision,
                'response_content': raw, 'tool_calls': response.get('tool_calls', []),
                'preceding_feedback': deepcopy(data.get('feedback')),
                'error': deepcopy(result.get('error'))}
            with self.lock:
                self.decision_trace.append(row)
        return result


def install_budget_adapter(engine, arm, events):
    """Exactly the same wrapper in all arms; only one declared value differs."""
    original = engine.collect
    audit = {'policy': deepcopy(ADAPTER_POLICY), 'arm': arm,
             'max_new_reads': ARM_CONFIGS[arm]['max_new_reads'], 'calls': []}

    def adapted(*args, **kwargs):
        phase = kwargs.get('phase', 'collect')
        if phase == 'review_rework':
            previous = kwargs.get('max_new_reads')
            kwargs = {**kwargs, 'max_new_reads': audit['max_new_reads']}
            entry = {'phase': phase, 'native_max_new_reads': previous,
                     'effective_max_new_reads': audit['max_new_reads']}
            audit['calls'].append(entry)
            events.append({'type': 'evaluation_budget_adapter', **entry, 'seq': len(events) + 1})
        return original(*args, **kwargs)

    engine.collect = adapted
    return audit


def worker_case(spec):
    from scripts.evaluate_frozen_versions import worker_case as public_case
    public = public_case(spec)
    if spec.get('fixed_draft') is not None:
        public['fixed_draft'] = deepcopy(spec['fixed_draft'])
    return public


def resolve_fixed_draft(template, executor):
    result = {'claims': [], 'next_steps': [], 'gaps': deepcopy(template.get('gaps', []))}
    for kind in ('claims', 'next_steps'):
        for item in template.get(kind, []):
            body = {key: deepcopy(value) for key, value in item.items()
                    if key not in {'selectors', 'counter_selectors'}}
            body['refs'] = _refs(executor, item.get('selectors', []), short=True)
            if kind == 'claims':
                body['counter_refs'] = _refs(executor, item.get('counter_selectors', []), short=True)
            result[kind].append(body)
    return result


def normalize_controls(batch, controls):
    by_id = {row['target_id']: row for row in batch.get('items', [])}
    rows = []
    for item in controls:
        native = by_id.get(item['target_id'], {})
        valid = native.get('protocol_status') == 'valid'
        verdict = native.get('submitted_decision') if item['kind'] == 'check_appropriateness' else native.get('relation')
        text = item.get('original_text', item.get('statement', item.get('action', '')))
        rows.append({'target_id': item['target_id'], 'kind': item['kind'],
            'original_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'native_verdict': verdict, 'accept': verdict in {'supported', 'appropriate'} if valid else None,
            'protocol_status': 'valid' if valid else 'not_assessed', 'native_fields': deepcopy(native)})
    return rows


def fixed_rework(spec, provider, principal, models, context, events):
    from tools.executor import ToolExecutor
    from workflow.v2.contracts import parse_draft
    from workflow.v2.review import review_draft
    from workflow.v2.engine import execute_review_rework, run_challenge, publish, collection_view
    executor = ToolExecutor(provider, principal, context.scope, context, role='single')
    provider._evaluation_executor = executor
    for check in spec['initial_checks']:
        executor.execute(check['tool'], check['args'])
    raw = resolve_fixed_draft(spec['fixed_draft'], executor)
    draft, errors = parse_draft(raw, executor)
    if errors:
        raise ValueError('Fixed draft does not validate')
    repairs = {'repairs': 0, 'reviewer_repairs': 0}
    drafts = [{'revision': 1, 'draft': draft.model_dump(mode='json')}]
    prefix = {'fixed_prefix': {'draft': raw, 'source_checks': deepcopy(spec['initial_checks']),
                              'is_fully_autonomous_e2e': False}, 'drafts': drafts}
    provider._evaluation_native = prefix
    review = review_draft(models['reviewer'], draft, executor, context, repairs=repairs,
                          collection=collection_view(executor))
    primary = review.model_dump(mode='json')
    review, challenge = run_challenge(models, draft, review, executor, context)
    first = {'draft': draft.model_dump(mode='json'), 'review': review.model_dump(mode='json'),
             'primary_review': primary, 'collection': deepcopy(collection_view(executor)),
             'evidence': [row.model_dump(mode='json') for row in executor.evidence.values()],
             'run_summary': context.summary()}
    prefix.update(first_pass=first, primary_review=primary, challenges=[challenge])
    draft, review, audit = execute_review_rework(models, draft, review, executor, context,
                                                repairs=repairs, enable_challenge=True)
    if audit.get('performed'):
        drafts.append({'revision': 2, 'draft': draft.model_dump(mode='json')})
    report = publish(draft, review, executor, context, audit.get('failures', []), 'FINISHED',
                     events, provider.ticket(), audit.get('decisions', 0), repairs)
    report.update(prefix, reworks=[audit], rework_rounds=int(bool(audit.get('performed'))),
        final_draft=draft.model_dump(mode='json'), final_review=review.model_dump(mode='json'),
        reviews=[{'revision': len(drafts), 'review': review.model_dump(mode='json')}],
        challenges=[challenge, *audit.get('challenges', [])],
        collection=collection_view(executor),
        revised_claim_accuracy='pending_independent_audit; initial_target_labels_not_reused')
    return report


def worker_main(source_root):
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    app = (Path(source_root).resolve() / 'app')
    sys.path[:] = [str(app), str(ROOT), *[path for path in sys.path
        if Path(path or '.').resolve() not in {(ROOT / 'app').resolve(), app, ROOT}]]
    initial = json.loads(sys.stdin.readline())
    if application_freeze(source_root) != initial['application']:
        raise ValueError('Frozen application differs')
    from runtime.context import Limits, RunContext
    from workflow.v2 import engine
    provider, principal = _worker_provider(initial['case'])
    events = []
    context = RunContext(scope=provider.ticket().scope, limits=Limits(**initial['limits']),
                         emit=lambda event: events.append({**event, 'seq': len(events) + 1}))
    adapter = install_budget_adapter(engine, initial['arm'], events)
    transport = WorkerTransport(sys.stdin, sys.stdout)
    models = {role: ProxyModel(transport, role) for role in ROLES}
    # Capture even an exceptional E2E run's actual acquired evidence.
    original_executor = engine.ToolExecutor
    def tracked_executor(*args, **kwargs):
        value = original_executor(*args, **kwargs)
        provider._evaluation_executor = value
        return value
    engine.ToolExecutor = tracked_executor
    try:
        if initial['stage'] == 'fixed_rework':
            native = fixed_rework(initial['case'], provider, principal, models, context, events)
        else:
            native = engine.investigate(models, provider, principal, context=context, event_log=events,
                                        enable_rework=True, enable_challenge=True)
    except Exception as exc:
        acquired = getattr(provider, '_evaluation_executor', None)
        native = {**deepcopy(getattr(provider, '_evaluation_native', {})), 'status': 'failed',
            'execution_error': safe_error(exc), 'events': events,
            'evidence': [row.model_dump(mode='json') for row in acquired.evidence.values()] if acquired else [],
            'collection': list(acquired.check_results.values()) if acquired else [], 'run_summary': context.summary()}
    normalized = normalize_report(native, arm='current', stage=initial['stage'])
    controls = initial['case'].get('review_controls', []) if initial['stage'] == 'fixed_rework' else []
    primary = native.get('primary_review', native.get('first_pass', {}).get('review', {}))
    first = native.get('first_pass', {}).get('review', {})
    normalized['review_controls_primary'] = normalize_controls(primary, controls)
    normalized['review_controls_first_pass'] = normalize_controls(first, controls)
    loaded = _loaded_application_paths(app)
    if application_freeze(source_root) != initial['application']:
        raise ValueError('Application changed during worker')
    result = {'type': 'worker_result', 'native_report': native, 'normalized': normalized,
        'run_summary': native.get('run_summary', context.summary()), 'loaded_application_modules': loaded,
        'observed_world_sha256': fingerprint(provider._data),
        'observed_capability_sha256': fingerprint(provider.binding.observations if hasattr(provider, 'binding') else {}),
        'budget_adapter_audit': adapter, 'arm_limits': deepcopy(initial['limits']),
        'initial_observation_ids': [row.get('locator', '').rsplit('/', 1)[-1]
            for row in native.get('first_pass', {}).get('evidence', []) if row.get('kind') == 'observation']
            if initial['stage'] == 'fixed_rework' else []}
    with transport.lock:
        sys.stdout.write(RPC_PREFIX + json.dumps(result, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    return 0


def execute_worker(spec, application, arm, stage, broker, *, timeout=300):
    command = [sys.executable, '-u', str(Path(__file__).resolve()), '--worker', application['source_root']]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding='utf-8', bufsize=1, cwd=ROOT)
    incoming, write_lock = queue.Queue(), Lock()
    initial = {'application': application, 'arm': arm, 'stage': stage,
               'limits': ARM_CONFIGS[arm]['limits'], 'case': worker_case(spec)}
    process.stdin.write(json.dumps(initial, ensure_ascii=False) + '\n')
    process.stdin.flush()
    def receive():
        for line in process.stdout:
            if line.startswith(RPC_PREFIX):
                try:
                    incoming.put(json.loads(line[len(RPC_PREFIX):]))
                except ValueError:
                    pass
        incoming.put({'type': 'worker_eof'})
    def answer(request):
        response = broker.handle(request)
        with write_lock:
            try:
                process.stdin.write(json.dumps(response, ensure_ascii=False) + '\n')
                process.stdin.flush()
            except (OSError, BrokenPipeError):
                pass
    started, result = time.monotonic(), None
    Thread(target=receive, daemon=True).start()
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            while True:
                left = timeout - (time.monotonic() - started)
                if left <= 0:
                    raise TimeoutError('Worker deadline')
                message = incoming.get(timeout=left)
                kind = message.get('type')
                if kind == 'model_request':
                    pool.submit(answer, message)
                elif kind == 'worker_result':
                    result = message
                    break
                elif kind in {'worker_eof', 'worker_error'}:
                    raise RuntimeError('Worker failed before report')
        finally:
            if result is None and process.poll() is None:
                process.kill()
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
    result.pop('type', None)
    result['worker_wall_seconds'] = time.monotonic() - started
    return result


def make_plan(args, config, specs):
    from evals.budget_matrix_cases import VERSION, freeze_case_spec
    declaration = json.loads(args.prespecified_plan.read_text(encoding='utf-8-sig')) if args.prespecified_plan else None
    stage = declaration['stages'][args.stage] if declaration else {}
    cases = args.cases or stage.get('cases') or [key for key, spec in specs.items() if args.stage in spec['stages']]
    repeats = args.repeats or stage.get('repeats', 3)
    if not cases or len(set(cases)) != len(cases) or any(key not in specs or args.stage not in specs[key]['stages'] for key in cases):
        raise ValueError('Cases do not match stage')
    if not 1 <= repeats <= 5:
        raise ValueError('Repetitions must be 1..5')
    freezes = [freeze_case_spec(specs[key]) for key in cases]
    plan = {'version': 'frozen-flash-budget-matrix-v1', 'stage': args.stage,
        'execution_mode': 'live' if args.live else 'plan_only', 'arms': list(ARMS),
        'arm_configs': deepcopy(ARM_CONFIGS), 'application': application_freeze(args.application_root),
        'model': args.model, 'model_settings': dict(SETTINGS),
        'provider_configuration_sha256': fingerprint({'base_url': config.base_url, 'extra_body': config.extra_body}),
        'cases': list(cases), 'repeats': repeats, 'planned_runs': len(cases) * len(ARMS) * repeats,
        'maximum_model_calls': len(cases) * repeats * sum(ARM_CONFIGS[arm]['limits']['model_calls'] for arm in ARMS),
        'schedule': list(schedule(cases, repeats)), 'jobs': args.jobs,
        'concurrency_policy': 'same_case_repeat_four_arm_block; blocks_sequential; rotated_submission_order',
        'world_freezes': freezes, 'case_freezes': {row['case_id']: row for row in freezes},
        'review_targets': {key: deepcopy(specs[key].get('review_controls', []))
                           if args.stage == 'fixed_rework' else [] for key in cases},
        'dataset_version': VERSION, 'evaluation_source_sha256': evaluation_freeze(),
        'adapter_policy': deepcopy(ADAPTER_POLICY), 'default_budget_modified': False,
        'accounting_policy': deepcopy(ACCOUNTING_POLICY),
        'paid_ledger_owner': 'one_parent_memory_lock_durable_reservation_and_settlement',
        'production_accuracy': 'not_established', 'rollout_recommendation': 'hold',
        'reference_kind': 'source_constructed_provisional_not_human_gold',
        'human_semantic_gate': 'pending', 'read_only': True,
        'completion_warning': 'Completion is not diagnosis accuracy.'}
    if declaration:
        for key in ('model', 'model_settings', 'provider_configuration_sha256', 'arm_configs',
                    'application', 'jobs', 'adapter_policy'):
            if declaration.get(key) != plan[key]:
                raise ValueError('Run differs from predeclared ' + key)
        if cases != stage['cases'] or repeats != stage['repeats']:
            raise ValueError('Run schedule differs from predeclaration')
        declared = declaration.get('case_freezes', {})
        if any(declared.get(row['case_id']) != row for row in freezes):
            raise ValueError('Case differs from predeclaration')
        if declaration.get('evaluation_source_sha256') != plan['evaluation_source_sha256']:
            raise ValueError('Execution sources differ from predeclaration')
        plan['prespecified_plan'] = {'path': str(args.prespecified_plan.resolve()),
            'sha256': hashlib.sha256(args.prespecified_plan.read_bytes()).hexdigest()}
    return plan


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--application-root', type=Path)
    parser.add_argument('--stage', choices=('e2e', 'fixed_rework'), default='e2e')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--repeats', type=int)
    parser.add_argument('--jobs', type=int, choices=(4,), default=4)
    parser.add_argument('--model', default='qwen3.7-flash', choices=('qwen3.7-flash',))
    parser.add_argument('--prespecified-plan', type=Path)
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'budget-matrix-study' / 'runs')
    args = parser.parse_args(argv)
    if args.worker:
        return parser, args
    if args.application_root is None:
        parser.error('Frozen --application-root is required')
    if args.live and any(value is None for value in (args.prespecified_plan, args.budget_ledger, args.max_model_calls)):
        parser.error('Live requires immutable plan, single-owner ledger and call cap')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 4000:
        parser.error('Invalid paid request cap')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    if args.worker:
        try:
            return worker_main(args.worker)
        except Exception as exc:
            sys.stdout.write(RPC_PREFIX + json.dumps({'type': 'worker_error', 'error': safe_error(exc)}) + '\n')
            sys.stdout.flush()
            return 2
    sys.path.insert(0, str(ROOT / 'app'))
    from config.incident import IncidentConfig
    from evals.budget_matrix_cases import build_case_specs, freeze_case_spec
    try:
        specs, config = build_case_specs(ROOT), IncidentConfig.from_file()
        plan = make_plan(args, config, specs)
        if not args.live:
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, **plan}, ensure_ascii=False, indent=2))
            return 0
        if not config.api_key or config.api_key == 'test-only':
            parser.error('Configured credentials required; no request sent')
        from agents.models import build_model
        model = build_model(config.api_key, args.model, base_url=config.base_url, extra_body=config.extra_body,
            max_output_tokens=SETTINGS['max_output_tokens'], request_timeout=SETTINGS['request_timeout'])
        budget = MemoryPaidBudget(args.budget_ledger, args.max_model_calls)
        directory = args.output / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'plan.json', plan)
        write_json(directory / 'frozen-case-specs.json', {key: specs[key] for key in plan['cases']})
        runs = []
        def save():
            write_json(directory / 'bundle.json', {'plan': plan, 'runs': runs,
                'paid_budget': budget.summary(), 'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'})
        save()
        def execute(entry):
            broker = MatrixBroker(model, budget, args.model, run_identity={**entry, 'stage': args.stage})
            try:
                report = execute_worker(specs[entry['case_id']], plan['application'], entry['arm'], args.stage, broker)
            except Exception as exc:
                report = {'native_report': {'status': 'harness_failed', 'execution_error': safe_error(exc)},
                    'run_summary': {}, 'normalized': {}, 'harness_failure': safe_error(exc),
                    'worker_wall_seconds': None, 'arm_limits': deepcopy(ARM_CONFIGS[entry['arm']]['limits'])}
            report.update(entry, stage=args.stage, execution_mode='live', application=plan['application'],
                world_freeze=plan['case_freezes'][entry['case_id']], model_settings=plan['model_settings'],
                provider_configuration_sha256=plan['provider_configuration_sha256'],
                model_call_log=sorted(broker.calls, key=lambda row: row['request_id']),
                decision_trace=sorted(broker.decision_trace, key=lambda row: row['request_id']),
                audit_ticket_question={key: deepcopy(specs[entry['case_id']]['world']['ticket'][key])
                    for key in ('title', 'symptoms', 'purpose', 'scope')
                    if key in specs[entry['case_id']]['world']['ticket']})
            return report
        for start in range(0, len(plan['schedule']), len(ARMS)):
            if evaluation_freeze() != plan['evaluation_source_sha256'] or application_freeze(args.application_root) != plan['application']:
                raise ValueError('Frozen execution sources changed')
            block = plan['schedule'][start:start + len(ARMS)]
            for entry in block:
                if freeze_case_spec(specs[entry['case_id']]) != plan['case_freezes'][entry['case_id']]:
                    raise ValueError('Case changed')
            # Declining admission is itself recorded by workers; never silently
            # omit later samples or rerun a failed arm to improve its result.
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                reports = list(pool.map(execute, block))
            for report in reports:
                name = f"{report['case_id']}-{report['arm']}-r{report['repeat']}.json"
                write_json(directory / name, report)
                entry = {key: report[key] for key in ('case_id', 'arm', 'repeat', 'stage')}
                runs.append({**entry, 'report_file': name,
                    'report_sha256': hashlib.sha256((directory / name).read_bytes()).hexdigest()})
                print(json.dumps({**entry, 'directory': str(directory),
                    'status': report['native_report'].get('status'), 'paid_requests': sum(row['charged_request'] for row in report['model_call_log'])}, ensure_ascii=False), flush=True)
            save()
        save()
        print(json.dumps({'directory': str(directory), 'runs': len(runs), 'expected_runs': plan['planned_runs'],
            'paid_budget': {key: value for key, value in budget.summary().items() if key not in {'reservation_events', 'initial', 'final'}}}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'evaluation_error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
