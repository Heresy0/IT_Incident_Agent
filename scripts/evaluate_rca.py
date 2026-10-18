"""Read-only RCA evaluation. Planning and scripted wiring never send API calls.

The parent owns the paid ledger; isolated workers receive only public sources
and, for diagnostic stages, separately published diagnostic inputs. Authored
private references never become model context or automatic correctness labels.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
from threading import Lock, Thread
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evals.rca_contracts import (DEFAULT_MANIFEST, canonical_sha, load_dataset,
    read_json, sha_file, validate_public, validate_public_diagnostics)
from scripts.evaluate_frozen_versions import (LIMITS, SETTINGS, ProxyModel,
    WorkerTransport, RPC_PREFIX, application_freeze, normalize_report, safe_error,
    write_json, _loaded_application_paths, _worker_provider, fixed_review)
from scripts.evaluate_budget_matrix import MatrixBroker, MemoryPaidBudget

STAGES = ('e2e', 'sufficient_evidence', 'fixed_review')
RUN_LIMITS = {**LIMITS, 'model_calls': 16}
MODEL_SETTINGS = dict(SETTINGS)
ROLES = ('investigation', 'diagnosis', 'reviewer', 'challenger')
FATAL_PROVIDER_CODES = {'QUOTA_EXCEEDED', 'AUTH_ERROR'}
REUSED_FILES = ('scripts/evaluate_frozen_versions.py', 'scripts/evaluate_autonomous.py',
    'scripts/evaluate_budget_matrix.py', 'evals/closed_loop_cases.py',
    'evals/frozen_version_cases.py')


def evaluation_freeze():
    paths = {ROOT / name for name in REUSED_FILES}
    for folder, pattern in (('scripts', '*rca*.py'), ('evals', '*rca*.py'),
                            ('tests/evals', 'test_rca*.py')):
        paths.update((ROOT / folder).glob(pattern))
    return {path.relative_to(ROOT).as_posix(): sha_file(path) for path in sorted(paths)}


def dataset_freeze(dataset):
    """Bind every registered file, including private material never sent to workers."""
    manifest = Path(dataset['manifest_path'])
    files = {'manifest.json': sha_file(manifest)}
    for case in dataset['cases']:
        for kind in ('public', 'truth', 'diagnostics'):
            relative = case[kind + '_path']
            files[relative] = sha_file(manifest.parent / relative)
    for relative, digest in dataset.get('bound_artifacts', {}).items():
        if sha_file(manifest.parent / relative) != digest:
            raise ValueError('Bound machine evidence changed')
        if relative in files and files[relative] != digest:
            raise ValueError('Machine evidence conflicts with a registered case file')
        files[relative] = digest
    return {'manifest_path': str(manifest), 'fingerprint': dataset['fingerprint'],
            'files_sha256': files}


def case_freeze(case):
    return {key: deepcopy(case[key]) for key in ('case_id', 'split', 'independence_group',
        'public_sha256', 'truth_sha256', 'diagnostics_sha256')}


def worker_case(case, stage, control_id=None):
    """An allowlist boundary, rather than deleting known private answer fields."""
    if stage not in STAGES:
        raise ValueError('Unknown evaluation stage')
    public = case['public']
    validate_public(public)
    result = {key: deepcopy(public[key]) for key in ('world', 'capability_metadata')}
    if stage == 'e2e':
        if control_id is not None:
            raise ValueError('E2E does not accept a diagnostic target')
        return result
    diagnostic = case['public_diagnostics']
    validate_public_diagnostics(diagnostic)
    result['initial_checks'] = deepcopy(diagnostic['source_queries'])
    if stage == 'fixed_review':
        controls = [row for row in diagnostic['fixed_controls'] if row['control_id'] == control_id]
        if len(controls) != 1:
            raise ValueError('A fixed review runs exactly one registered original sentence')
        row = controls[0]
        result['review_controls'] = [{'target_id': 'C1', 'kind': 'factual_relation',
            'statement': row['statement'], 'original_text': row['statement'],
            'level': row['level'], 'selectors': deepcopy(row['selectors'])}]
    return result


def schedule(cases, stages, repeats, system_id):
    for repeat in range(1, repeats + 1):
        for case in cases:
            for stage in stages:
                targets = [row['control_id'] for row in case['public_diagnostics']['fixed_controls']] \
                    if stage == 'fixed_review' else [None]
                for control_id in targets:
                    yield {'case_id': case['case_id'], 'split': case['split'],
                        'repeat': repeat, 'system_id': system_id, 'stage': stage,
                        'control_id': control_id}


def make_plan(args, dataset, provider_sha):
    split = {'candidate': 'candidate_acceptance'}.get(args.split, args.split)
    cases = [case for case in dataset['cases'] if split == 'all' or case['split'] == split]
    if args.cases:
        by_id = {case['case_id']: case for case in cases}
        if len(set(args.cases)) != len(args.cases) or not set(args.cases) <= by_id.keys():
            raise ValueError('Unknown, duplicate or out-of-split case')
        cases = [by_id[key] for key in args.cases]
    if not cases:
        raise ValueError('No cases selected')
    rows = list(schedule(cases, args.stages, args.repeats, args.system_id))
    plan = {'schema_version': 'rca-run-plan-v1', 'application': application_freeze(args.application_root),
        'native_arm': 'current', 'system_id': args.system_id, 'split': args.split,
        'cases': [case['case_id'] for case in cases], 'stages': list(args.stages),
        'repeats': args.repeats, 'schedule': rows, 'planned_runs': len(rows),
        'limits': deepcopy(RUN_LIMITS), 'model': args.model, 'model_settings': deepcopy(MODEL_SETTINGS),
        'maximum_model_calls': len(rows) * RUN_LIMITS['model_calls'],
        'jobs': args.jobs, 'concurrency_policy': 'bounded_workers; schedule_order; no_selective_reruns',
        'provider_configuration_sha256': provider_sha, 'dataset_freeze': dataset_freeze(dataset),
        'case_freezes': {case['case_id']: case_freeze(case) for case in cases},
        'evaluation_source_sha256': evaluation_freeze(), 'business_sources_modified': False,
        'runtime_budget_adapter': None, 'default_budget_modified': False,
        'diagnostic_source': 'separate_public_diagnostic_tasks; never_private_truth',
        'fixed_review_unit': 'one_original_sentence_per_run; native_target_C1',
        'sufficient_evidence_policy': 'actual_prefix_reads; native_draft_review_challenge; no_collection_or_rework',
        'accounting_policy': {'single_parent_ledger': True, 'known_usage_settled': True,
            'unknown_usage_retains_reservation': True, 'timeout': 30, 'reservation_margin_seconds': 5},
        'fatal_provider_codes': sorted(FATAL_PROVIDER_CODES),
        'human_truth_status': 'pending_human_review', 'human_semantic_gate': 'pending',
        'formal_acceptance_eligible': False, 'semantic_accuracy': None,
        'completion_warning': 'Completion and authored reference matches are not RCA accuracy.'}
    if args.prespecified_plan:
        declaration = read_json(args.prespecified_plan)
        # No provider request, output directory, or paid reservation precedes this check.
        if declaration != plan:
            changed = sorted(key for key in set(declaration) | set(plan)
                             if declaration.get(key) != plan.get(key))
            raise ValueError('Execution differs from immutable plan: ' + ','.join(changed))
    return plan


def verify_inputs(plan, manifest_path, prespecified_plan=None):
    if application_freeze(plan['application']['source_root']) != plan['application']:
        raise ValueError('Frozen application changed')
    if evaluation_freeze() != plan['evaluation_source_sha256']:
        raise ValueError('Evaluation source changed')
    dataset = load_dataset(manifest_path)
    if dataset_freeze(dataset) != plan['dataset_freeze']:
        raise ValueError('Dataset changed')
    if prespecified_plan is not None and read_json(prespecified_plan) != plan:
        raise ValueError('Immutable declaration changed')
    return dataset


class RcaBroker(MatrixBroker):
    """Keep classified provider errors and close further admission on quota/auth."""
    def handle(self, request):
        result = super().handle(request)
        code = result.get('error', {}).get('code')
        if code in FATAL_PROVIDER_CODES | {'BUDGET_EXCEEDED'} and self.budget is not None:
            with self.budget.lock:
                reason = code if code in FATAL_PROVIDER_CODES else 'GLOBAL_BUDGET_ADMISSION_CLOSED'
                self.budget.state.update(admission_closed=True, admission_close_reason=reason)
                try:
                    self.budget._persist()
                except OSError as exc:
                    self.budget.persistence_errors.append({'phase': 'provider_circuit_breaker', **safe_error(exc)})
        return result


def sufficient_evidence(spec, provider, principal, models, context, events):
    """Actual tool reads followed by the native diagnostic path; no answer injection."""
    from tools.executor import ToolExecutor
    from workflow.v2.engine import (make_draft, collection_view, model_for, publish, run_challenge)
    from workflow.v2.review import review_draft
    executor = ToolExecutor(provider, principal, context.scope, context, role='single')
    provider._evaluation_executor = executor
    for check in spec['initial_checks']:
        executor.execute(check['tool'], check['args'])
    prefix_ids = [row.evidence_id for row in executor.evidence.values()]
    failures, repairs = [], {'repairs': 0, 'reviewer_repairs': 0}
    draft = make_draft(models, executor, context, failures, repairs,
                       'FIXED_PUBLIC_PREFIX_COMPLETE', reserve_after=2)
    primary = review_draft(model_for(models, 'reviewer'), draft, executor, context,
        repairs=repairs, collection=collection_view(executor))
    final, challenge = run_challenge(models, draft, primary, executor, context)
    report = publish(draft, final, executor, context, failures,
        'FIXED_PUBLIC_PREFIX_COMPLETE', events, provider.ticket(), 0, repairs)
    report.update(drafts=[{'revision': 1, 'draft': draft.model_dump(mode='json')}],
        primary_review=primary.model_dump(mode='json'), final_review=final.model_dump(mode='json'),
        challenges=[challenge], fixed_prefix={'source_checks': deepcopy(spec['initial_checks']),
            'evidence_ids': prefix_ids, 'is_fully_autonomous_e2e': False},
        execution_variant='fixed_public_evidence_diagnostic_only')
    return report


def worker_main(source_root):
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    app = Path(source_root).resolve() / 'app'
    sys.path[:] = [str(app), str(ROOT), *[path for path in sys.path
        if Path(path or '.').resolve() not in {(ROOT / 'app').resolve(), app, ROOT}]]
    initial = json.loads(sys.stdin.readline())
    if application_freeze(source_root) != initial['application']:
        raise ValueError('Worker application differs from plan')
    from runtime.context import Limits, RunContext
    from workflow.v2 import engine
    provider, principal = _worker_provider(initial['case'])
    events = []
    context = RunContext(scope=provider.ticket().scope, limits=Limits(**initial['limits']),
        emit=lambda event: events.append({**event, 'seq': len(events) + 1}))
    transport = WorkerTransport(sys.stdin, sys.stdout)
    models = {role: ProxyModel(transport, role) for role in ROLES}
    # Observation-only instrumentation retains evidence when native execution raises.
    # It changes neither tool arguments, return values, nor execution policy.
    native_executor = engine.ToolExecutor
    def track_executor(*args, **kwargs):
        result = native_executor(*args, **kwargs)
        provider._evaluation_executor = result
        return result
    engine.ToolExecutor = track_executor
    try:
        if initial['stage'] == 'e2e':
            native = engine.investigate(models, provider, principal, context=context,
                event_log=events, enable_rework=True, enable_challenge=True)
        elif initial['stage'] == 'sufficient_evidence':
            native = sufficient_evidence(initial['case'], provider, principal, models, context, events)
        elif initial['stage'] == 'fixed_review':
            native = fixed_review(initial['case'], provider, principal, models, context, events, 'current')
        else:
            raise ValueError('Unknown worker stage')
    except Exception as exc:
        acquired = getattr(provider, '_evaluation_executor', None)
        native = {**deepcopy(getattr(provider, '_evaluation_native', {})), 'status': 'failed',
            'execution_error': safe_error(exc), 'events': events,
            'evidence': [row.model_dump(mode='json') for row in acquired.evidence.values()] if acquired else [],
            'collection': list(acquired.check_results.values()) if acquired else [],
            'run_summary': context.summary()}
    if application_freeze(source_root) != initial['application']:
        raise ValueError('Application changed during worker')
    result = {'type': 'worker_result', 'native_report': native,
        'normalized': normalize_report(native, arm='current', stage=initial['stage'],
            controls=initial['case'].get('review_controls', [])),
        'run_summary': native.get('run_summary', context.summary()),
        'loaded_application_modules': _loaded_application_paths(app),
        'observed_world_sha256': canonical_sha(provider._data),
        'observed_capability_sha256': canonical_sha(provider.binding.observations if hasattr(provider, 'binding') else {}),
        'native_arm': 'current', 'runtime_budget_adapter': None}
    with transport.lock:
        sys.stdout.write(RPC_PREFIX + json.dumps(result, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    return 0


def validate_worker_world(result, spec):
    """Do not label a worker result with a world it did not actually observe."""
    for observed, expected in (('observed_world_sha256', 'world'),
                               ('observed_capability_sha256', 'capability_metadata')):
        if result.get(observed) != canonical_sha(spec.get(expected, {})):
            raise ValueError('Worker observed public input mismatch: ' + observed)
    return True


def execute_worker(spec, application, stage, broker, *, timeout=300):
    command = [sys.executable, '-u', str(Path(__file__).resolve()), '--worker', application['source_root']]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1, cwd=ROOT)
    incoming, write_lock = queue.Queue(), Lock()
    initial = {'application': application, 'native_arm': 'current', 'stage': stage,
               'limits': deepcopy(RUN_LIMITS), 'case': spec}
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
                if message.get('type') == 'model_request':
                    pool.submit(answer, message)
                elif message.get('type') == 'worker_result':
                    result = message
                    break
                elif message.get('type') in {'worker_eof', 'worker_error'}:
                    raise RuntimeError('Worker exited without report')
        finally:
            if result is None and process.poll() is None:
                process.kill()
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
    result.pop('type', None)
    result['worker_wall_seconds'] = time.monotonic() - started
    validate_worker_world(result, spec)
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--scripted', action='store_true')
    modes.add_argument('--live', action='store_true')
    parser.add_argument('--application-root', type=Path)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--split', choices=('calibration', 'candidate', 'candidate_acceptance', 'all'), default='calibration')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--stages', nargs='+', choices=STAGES, default=['e2e'])
    parser.add_argument('--repeats', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--jobs', type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument('--system-id', default='baseline')
    parser.add_argument('--model', choices=('qwen3.7-flash',), default='qwen3.7-flash')
    parser.add_argument('--prespecified-plan', type=Path)
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--collect-pending', action='store_true',
        help='Collect observations with provisional truth; never claim formal acceptance')
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'rca-study')
    args = parser.parse_args(argv)
    if args.worker:
        return parser, args
    if args.application_root is None:
        parser.error('--application-root required')
    if len(set(args.stages)) != len(args.stages):
        parser.error('Duplicate stage')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.system_id):
        parser.error('Invalid system identity')
    if args.live and any(value is None for value in
                         (args.prespecified_plan, args.budget_ledger, args.max_model_calls)):
        parser.error('Live requires immutable plan, single-owner ledger and request cap')
    if args.live and not args.collect_pending:
        parser.error('Human truth is pending; explicitly use --collect-pending for observation-only collection')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 20000:
        parser.error('Invalid request cap')
    return parser, args


def run_collection(args, plan, dataset, model=None, budget=None):
    directory = args.output / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / 'plan.json', plan)
    cases = {row['case_id']: row for row in dataset['cases']}
    mode = 'live_observation_only' if args.live else 'scripted_control_only'
    declaration_sha = sha_file(args.prespecified_plan) if args.prespecified_plan else None
    runs, pending = [], []
    def save():
        write_json(directory / 'bundle.json', {'schema_version': 'rca-run-bundle-v1',
            'plan': plan, 'runs': runs, 'scheduled_pending': pending,
            'prespecified_plan_sha256': declaration_sha,
            'paid_budget': budget.summary() if budget else None, 'execution_mode': mode,
            'human_semantic_gate': 'pending', 'human_verified': False, 'is_gold': False,
            'semantic_accuracy': None, 'formal_acceptance_eligible': False,
            'planned_runs': plan['planned_runs'], 'recorded_runs': len(runs)})
    def execute(entry):
        chat = model
        if not args.live:
            from workflow.v2.scripted import scripted_model
            chat = scripted_model(None)
        broker = RcaBroker(chat, budget, args.model if args.live else 'scripted-wiring', run_identity=entry)
        case = cases[entry['case_id']]
        try:
            report = execute_worker(worker_case(case, entry['stage'], entry['control_id']),
                plan['application'], entry['stage'], broker)
        except Exception as exc:
            report = {'native_report': {'status': 'harness_failed', 'execution_error': safe_error(exc)},
                'normalized': {}, 'run_summary': {}, 'worker_wall_seconds': None,
                'harness_failure': safe_error(exc)}
        identifier = 'RCA_' + canonical_sha({'bundle': directory.name, **entry})[:24]
        report.update(entry, report_id=identifier, condition=entry['system_id'],
            execution_mode=mode, application=plan['application'], world_freeze=case_freeze(case),
            dataset_fingerprint=dataset['fingerprint'], model_settings=plan['model_settings'],
            provider_configuration_sha256=plan['provider_configuration_sha256'],
            model_call_log=sorted(broker.calls, key=lambda row: row['request_id']),
            decision_trace=sorted(broker.decision_trace, key=lambda row: row['request_id']),
            human_semantic_gate='pending', human_verified=False, is_gold=False, semantic_accuracy=None,
            audit_ticket_question={key: deepcopy(case['public']['world']['ticket'][key])
                for key in ('title', 'symptoms', 'purpose', 'scope')
                if key in case['public']['world']['ticket']})
        return report
    save()
    for offset in range(0, len(plan['schedule']), args.jobs):
        try:
            verify_inputs(plan, args.dataset, args.prespecified_plan)
            if args.prespecified_plan and sha_file(args.prespecified_plan) != declaration_sha:
                raise ValueError('Immutable declaration bytes changed')
        except Exception:
            pending.extend({**entry, 'reason': 'FROZEN_INPUT_CHANGED'} for entry in plan['schedule'][offset:])
            save()
            raise
        if budget and (budget.snapshot().get('admission_closed') or budget.session_requests >= budget.max_calls
                       or any(value <= 0 for value in budget.remaining().values())):
            reason = budget.snapshot().get('admission_close_reason', 'GLOBAL_BUDGET_ADMISSION_CLOSED')
            pending.extend({**entry, 'reason': reason} for entry in plan['schedule'][offset:])
            break
        block = plan['schedule'][offset:offset + args.jobs]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            reports = list(pool.map(execute, block))
        for report in reports:
            name = report['report_id'] + '.json'
            write_json(directory / name, report)
            entry = {key: report[key] for key in ('report_id', 'case_id', 'split', 'repeat', 'system_id', 'stage', 'control_id')}
            runs.append({**entry, 'report_file': name, 'report_sha256': sha_file(directory / name)})
            print(json.dumps({**entry, 'status': report['native_report'].get('status'),
                'paid_requests': sum(row['charged_request'] for row in report['model_call_log']),
                'directory': str(directory)}, ensure_ascii=False), flush=True)
        save()
    save()
    # A final input check also covers the last worker block.
    verify_inputs(plan, args.dataset, args.prespecified_plan)
    if args.prespecified_plan and sha_file(args.prespecified_plan) != declaration_sha:
        raise ValueError('Immutable declaration bytes changed')
    return directory


def main(argv=None):
    parser, args = parse_args(argv)
    if args.worker:
        try:
            return worker_main(args.worker)
        except Exception as exc:
            sys.stdout.write(RPC_PREFIX + json.dumps({'type': 'worker_error', 'error': safe_error(exc)}) + '\n')
            sys.stdout.flush()
            return 2
    try:
        sys.path.insert(0, str(ROOT / 'app'))
        from config.incident import IncidentConfig
        config = IncidentConfig.from_file()
        dataset = load_dataset(args.dataset)
        provider_sha = canonical_sha({'base_url': config.base_url, 'extra_body': config.extra_body})
        plan = make_plan(args, dataset, provider_sha)
        if not args.live and not args.scripted:
            args.output.mkdir(parents=True, exist_ok=True)
            write_json(args.output / 'plan.json', plan)
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0,
                'plan_file': str(args.output / 'plan.json'), 'planned_runs': plan['planned_runs'],
                'maximum_model_calls': plan['maximum_model_calls'],
                'human_truth_status': 'pending_human_review'}, ensure_ascii=False))
            return 0
        budget, model = None, None
        if args.live:
            if not config.api_key or config.api_key == 'test-only':
                parser.error('Configured credentials required; no request sent')
            if args.max_model_calls > plan['maximum_model_calls']:
                parser.error('Request cap exceeds declared schedule maximum')
            from agents.models import build_model
            model = build_model(config.api_key, args.model, base_url=config.base_url,
                extra_body=config.extra_body, max_output_tokens=MODEL_SETTINGS['max_output_tokens'],
                request_timeout=MODEL_SETTINGS['request_timeout'])
            budget = MemoryPaidBudget(args.budget_ledger, args.max_model_calls)
        directory = run_collection(args, plan, dataset, model, budget)
        print(json.dumps({'directory': str(directory), 'semantic_accuracy': None,
            'human_semantic_gate': 'pending', 'paid_requests': budget.session_requests if budget else 0}))
        return 0
    except Exception as exc:
        print(json.dumps({'evaluation_error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
