"""Frozen legacy/v2 experiments. Planning is free; scripted runs are not semantic evidence."""
import argparse
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from threading import RLock
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from api.auth import Principal
from config.incident import IncidentConfig
from evals.common import write_json
from providers.fixtures import FixtureProvider
from runtime.context import ExecutionError, Limits, RunContext
from runtime.version import implementation_version

DEFAULT_CASES = ('case_001', 'case_003', 'case_004', 'case_006')
MODEL_SETTINGS = {'temperature': 0, 'max_output_tokens': 2200, 'request_timeout': 30,
                  'max_retries': 0, 'response_format': 'json_object'}
PRINCIPAL = Principal('synthetic_demo', 'cli_reader')
LIMIT_KEYS = {'requests': 'max_requests', 'input_tokens': 'max_input_tokens',
              'output_tokens': 'max_output_tokens', 'model_seconds': 'max_model_seconds'}

# Evaluation-input metadata only. These definitions describe measured quantities;
# they contain no thresholds, expected outcomes, causes or query selection rules.
METRIC_MEANINGS = {
    'db_cpu': ('Processor utilization observed at the database host.', 'database host processor'),
    'dependency_error_rate': ('Fraction of outgoing dependency requests that failed.', 'outgoing dependency requests'),
    'pool_usage': ('Occupied connections as a fraction of application connection-pool capacity.', 'application connection pool'),
    'pool_wait': ('Time spent waiting to acquire an application connection-pool slot.', 'connection acquisition queue'),
    'request_error_rate': ('Fraction of inbound requests that ended with an error.', 'service inbound requests'),
    'request_latency': ('Observed duration of inbound request processing.', 'service inbound request duration'),
    'request_rate': ('Inbound requests processed per unit time.', 'service inbound request volume'),
}
LOG_MEANINGS = {
    'dependency': 'Events at the outgoing request boundary, including response outcomes and health controls.',
    'configuration': 'Events from local configuration loading and validation.',
    'resource': 'Events concerning local process capacity, queues and resource acquisition.',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def jsonable(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


class FrozenWorld:
    """Queryable observation data, captured once; never an old tool-trace lookup."""
    def __init__(self, cases, *, anonymize=False):
        folder = ROOT / 'fixtures' / 'incident'
        self.catalog_bytes = (folder / 'catalog.json').read_bytes()
        catalog = json.loads(self.catalog_bytes)
        entries = {row['case_id']: row for row in catalog if row.get('visibility') == 'public_synthetic'}
        if not cases or len(cases) != len(set(cases)) or not set(cases) <= entries.keys():
            raise ValueError('Unknown, duplicate or non-public synthetic case')
        self.payloads, self.hashes, self.capability_metadata = {}, {}, {}
        self.anonymization = {'enabled': False}
        for case in cases:
            path = folder / entries[case]['file']
            if path.parent.resolve() != folder.resolve() or path.suffix != '.json':
                raise ValueError('Unsafe fixture registration')
            raw = path.read_bytes()
            data = json.loads(raw)
            if data.get('synthetic') is not True or {'gold', 'cause', 'correct_answer'}.intersection(data):
                raise ValueError('Observation-only synthetic data required')
            self.payloads[case], self.hashes[case] = data, digest(raw)
        if anonymize:
            self._anonymize()

    def _anonymize(self):
        """Rename identifiers in the data world, never select a fault or a path."""
        def opaque(prefix, original):
            return prefix + digest(original.encode('utf-8'))[:12]

        services, metrics, aliases = set(), set(), set()
        for data in self.payloads.values():
            scope = data['ticket']['scope']
            services.update([scope['service'], *scope.get('allowed_dependencies', [])])
            for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
                services.update(row['service'] for row in data[kind])
            metrics.update(row['metric'] for row in data['metrics'])
            aliases.update(alias for row in data['owners'] for alias in row.get('aliases', []))
        service_map = {name: opaque('service_', name) for name in sorted(services)}
        metric_map = {name: opaque('signal_', name) for name in sorted(metrics)}
        alias_map = {name: service_map.get(name, opaque('alias_', name)) for name in sorted(aliases)}
        replacements = {**alias_map, **service_map, **metric_map}

        def rename(value):
            if isinstance(value, dict):
                return {key: rename(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rename(item) for item in value]
            if not isinstance(value, str):
                return value
            if value in replacements:
                return replacements[value]
            # Longest first also maps text spellings such as "Checkout API".
            for original in sorted(replacements, key=len, reverse=True):
                pattern = re.escape(original).replace(r'\-', r'[- _]')
                value = re.sub(r'(?<![\w])' + pattern + r'(?![\w])',
                               replacements[original], value, flags=re.IGNORECASE)
            return value

        for case, original in list(self.payloads.items()):
            definitions = {}
            for row in original['metrics']:
                description, observed_object = METRIC_MEANINGS.get(row['metric'], ('unknown', 'unknown'))
                definitions[metric_map[row['metric']]] = {
                    'description': description, 'observed_object': observed_object,
                    'unit': row.get('unit', ''), 'aggregation': row.get('aggregation', 'unknown'),
                    'sampling': row.get('granularity', 'unknown'),
                    'source': 'evaluation input quantity description', 'version': 'anonymous-world-v1'}
            transformed = rename(original)
            transformed['ticket']['incident_id'] = opaque('incident_', original['ticket']['incident_id'])
            transformed['ticket']['title'] = 'Synthetic incident with renamed observation identifiers'
            for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
                for old_row, new_row in zip(original[kind], transformed[kind]):
                    new_row['id'] = opaque('row_', old_row['id'])
            self.payloads[case] = transformed
            self.capability_metadata[case] = {
                # Sentinels describe this in-memory adapter, not real endpoints.
                'prometheus': {'synthetic_world': True}, 'loki': {'synthetic_world': True},
                'metrics': definitions, 'log_queries': {name: 'synthetic_partition' for name in LOG_MEANINGS},
                'log_metadata': {name: {'description': description, 'version': 'anonymous-world-v1'}
                                 for name, description in LOG_MEANINGS.items()}}
        self.anonymization = {'enabled': True, 'version': 'anonymous-world-v1',
            'service_mapping': service_map, 'metric_mapping': metric_map, 'alias_mapping': alias_map,
            'metadata_source': 'evaluation-only quantity/channel descriptions; no fault rules or answer labels',
            'runtime_receives_original_mapping': False}

    def provider(self, case, principal=PRINCIPAL):
        world = self

        class FrozenProvider(FixtureProvider):
            name = 'autonomous_frozen_synthetic_world'

            def __init__(self):
                self._data, self.principal = deepcopy(world.payloads[case]), principal
                if case in world.capability_metadata:
                    from providers.registry import ServiceBinding
                    scope = self.ticket().scope
                    self.binding = ServiceBinding(principal.tenant_id, (principal.user_id,), scope.service,
                        scope.environment, scope.service_version, scope.allowed_dependencies,
                        observations=deepcopy(world.capability_metadata[case]))

            def query(self, name, args, scope, *, max_metric_points=60):
                rows, truncated = super().query(name, args, scope)
                if name == 'get_service_metrics' and max_metric_points < 60:
                    selected, counts = [], {}
                    for row in rows:
                        metric = row['metric']
                        if counts.get(metric, 0) < max_metric_points:
                            selected.append(row)
                            counts[metric] = counts.get(metric, 0) + 1
                    return selected, truncated or len(selected) < len(rows)
                return rows, truncated

        return FrozenProvider()

    def freeze(self):
        return {'kind': 'queryable_synthetic_world', 'catalog_sha256': digest(self.catalog_bytes),
                'fixture_sha256': self.hashes,
                'services': {cid: row['ticket']['scope']['service'] for cid, row in self.payloads.items()},
                'anonymization': self.anonymization,
                'runtime_world_sha256': {cid: digest(json.dumps({'observations': data,
                    'capability_metadata': self.capability_metadata.get(cid)}, sort_keys=True,
                    ensure_ascii=False).encode('utf-8')) for cid, data in self.payloads.items()},
                'profile_required': False, 'external_blind_evaluation': False}


class GlobalPaidBudget:
    """Single-process shared ledger, including bound models and legacy parallel calls.

    Before each request reserve conservative UTF-8 input bytes, maximum output and
    timeout. Known usage replaces reservations. Unknown usage stays conservatively
    charged and is explicitly labelled; the ledger must have one process owner.
    """
    def __init__(self, path, max_calls):
        self.path, self.max_calls = Path(path), max_calls
        self.lock, self.session_requests = RLock(), 0
        self.initial = self.snapshot()

    def snapshot(self):
        state = json.loads(self.path.read_text(encoding='utf-8'))
        for used, cap in LIMIT_KEYS.items():
            if not isinstance(state.get(used), (int, float)) or not isinstance(state.get(cap), (int, float)) \
                    or not math.isfinite(state[used]) or not math.isfinite(state[cap]) \
                    or state[used] < 0 or state[cap] <= 0:
                raise ValueError('Invalid paid budget ledger')
        if not isinstance(state.get('unknown_usage_calls'), int) or state['unknown_usage_calls'] < 0:
            raise ValueError('Invalid paid budget usage status')
        return state

    def remaining(self):
        state = self.snapshot()
        return {key: max(0, state[cap] - state[key]) for key, cap in LIMIT_KEYS.items()}

    def reserve(self, input_bound, output_bound, seconds_bound):
        reservations = {'requests': 1, 'input_tokens': input_bound,
                        'output_tokens': output_bound, 'model_seconds': seconds_bound}
        with self.lock:
            state = self.snapshot()
            if state.get('admission_closed') or self.session_requests >= self.max_calls or any(
                    state[key] + value > state[LIMIT_KEYS[key]] for key, value in reservations.items()):
                raise ExecutionError('BUDGET_EXCEEDED', 'evaluation')
            self.session_requests += 1
            for key, value in reservations.items():
                state[key] += value
            write_json(self.path, state)
        return reservations

    def settle(self, reservation, usage, seconds):
        with self.lock:
            state = self.snapshot()
            known = isinstance(usage, dict) and all(isinstance(usage.get(k), int)
                and usage[k] >= 0 for k in ('input_tokens', 'output_tokens'))
            if known:
                for key in ('input_tokens', 'output_tokens'):
                    state[key] += usage[key] - reservation[key]
                if any(usage[key] > reservation[key] for key in ('input_tokens', 'output_tokens')):
                    state['provider_usage_over_reservation'] = True
                    state['admission_closed'] = True
            else:
                state['unknown_usage_calls'] += 1
            # A provider violating its bound cannot be un-called. Keep the actual
            # measurement and close admission instead of hiding excess usage.
            state['model_seconds'] += seconds - reservation['model_seconds']
            if seconds > reservation['model_seconds']:
                state['timeout_contract_exceeded'] = True
                state['admission_closed'] = True
            write_json(self.path, state)

    def summary(self):
        final = self.snapshot()
        return {'session_requests': self.session_requests,
                'charged_delta': {key: final[key] - self.initial[key] for key in LIMIT_KEYS},
                'unknown_usage_calls_delta': final['unknown_usage_calls'] - self.initial['unknown_usage_calls'],
                'remaining': self.remaining(), 'token_accounting': 'known_or_conservative_upper_bound',
                'single_process_ledger': True, 'admission_closed': final.get('admission_closed', False)}


class BudgetedModel:
    def __init__(self, model, budget, tools=()):
        self.model, self.budget, self.tools = model, budget, tools
        self.json_output = getattr(model, 'json_output', True)
        self.blind_review = getattr(model, 'blind_review', False)

    def bind_tools(self, tools):
        return BudgetedModel(self.model.bind_tools(tools), self.budget, tools)

    def invoke(self, messages):
        serialized = json.dumps({'messages': [m.model_dump(mode='json') for m in messages],
                                 'tools': self.tools}, ensure_ascii=False, default=str)
        # UTF-8 byte count plus framing is intentionally conservative, not a
        # claim to have measured provider-specific tokenization.
        bound = len(serialized.encode('utf-8')) + 1024
        reservation = self.budget.reserve(bound, MODEL_SETTINGS['max_output_tokens'],
                                          MODEL_SETTINGS['request_timeout'])
        started, usage = time.monotonic(), None
        try:
            response = self.model.invoke(messages)
            usage = getattr(response, 'usage_metadata', None)
            if not usage:
                raw = getattr(response, 'response_metadata', {}).get('token_usage', {})
                if 'prompt_tokens' in raw and 'completion_tokens' in raw:
                    usage = {'input_tokens': raw['prompt_tokens'], 'output_tokens': raw['completion_tokens']}
            return response
        finally:
            self.budget.settle(reservation, usage, time.monotonic() - started)


def safe_exception(exc):
    # Never serialize exception text: HTTP/provider errors can contain credentials.
    return {'code': exc.code if isinstance(exc, ExecutionError) else 'INTERNAL_ERROR',
            'exception_type': type(exc).__name__}


def make_plan(args, config, world):
    settings = {**MODEL_SETTINGS, 'model': config.model}
    fingerprint = digest(json.dumps({'settings': settings, 'base_url': config.base_url,
                                    'extra_body': config.extra_body}, sort_keys=True).encode())
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True)
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True, check=True)
    app_files = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted((ROOT / 'app').rglob('*.py'))}
    budget = GlobalPaidBudget(args.budget_ledger, args.max_model_calls) if args.budget_ledger else None
    count = len(args.cases) * args.repeats * len(args.arms)
    if args.stage == 'review':
        count *= 5
    return {'schema_version': 'autonomous-evaluation-v1', 'stage': args.stage,
            'execution_mode': 'live' if args.live else 'scripted_control_only' if args.scripted else 'plan_only',
            'semantic_quality_status': 'pending_human_review' if args.live else 'not_evaluated',
            'cases': args.cases, 'arms': args.arms, 'repeats': args.repeats,
            'model_settings': settings, 'configuration_sha256': fingerprint,
            'limits': asdict(run_limits(args)), 'planned_runs': count,
            'maximum_model_calls': min(count * args.model_budget, args.max_model_calls)
                if args.max_model_calls is not None else count * args.model_budget,
            'paid_budget_remaining': budget.remaining() if budget else None,
            'source_version': implementation_version(), 'source_files_sha256': app_files,
            'evaluation_script_sha256': digest(Path(__file__).read_bytes()),
            'git_commit': git.stdout.strip(), 'working_tree_dirty': bool(dirty.stdout.strip()),
            'world': world.freeze(), 'human_evaluation': 'pending',
            'reference_policy': 'source_constructed_checks_are_provisional_not_human_labels',
            'read_only': True, 'estimated_cost': None}


def run_limits(args):
    return replace(Limits.from_env(), model_calls=args.model_budget, tool_calls=args.tool_budget,
                   seconds=args.seconds, reserve_model_calls=2, reserve_seconds=min(15, args.seconds / 4))


def schedule(cases, arms, repeats):
    for repeat in range(repeats):
        for index, case in enumerate(cases):
            order = arms if (repeat + index) % 2 == 0 else list(reversed(arms))
            for arm in order:
                yield case, arm, repeat + 1


def capture_e2e(case, arm, world, model, limits):
    provider, events = world.provider(case), []
    context = RunContext(limits=limits, scope=provider.ticket().scope,
                         emit=lambda e: events.append({**e, 'seq': len(events) + 1}))
    try:
        if arm == 'legacy':
            from workflow.coordinator import collaborate
            result = collaborate({role: model for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')},
                                 provider, PRINCIPAL, context=context, event_log=events,
                                 investigation_mode='programmatic', knowledge_mode='programmatic')
        else:
            from workflow.v2.engine import investigate
            result = investigate(model, provider, PRINCIPAL, context=context, limits=limits, event_log=events)
    except Exception as exc:
        result = {'status': 'failed', 'review_status': 'not_performed', 'execution_error': safe_exception(exc),
                  'evidence': [], 'events': events, 'run_summary': context.summary()}
    return result


def review_inputs(executor):
    """Simple source-entailment controls; they do not establish diagnosis quality."""
    item = next(e for e in executor.evidence.values() if 'value' in e.payload)
    value, metric = item.payload['value'], item.payload['metric']
    when = item.observed_from.isoformat()
    ref = next(r.reference_id for r in item.reference_options if r.field_path == 'value')
    observed = f'在{when}，{metric}的观测值为{value}{item.payload.get("unit", "")}。'
    wrong = value + 1 if isinstance(value, (int, float)) else 'different'
    controls = [('source_fact', observed, 'supported'),
                ('paraphrase', f'{metric}在{when}记录的值是{value}{item.payload.get("unit", "")}。', 'supported'),
                ('wrong_value', f'在{when}，{metric}的观测值为{wrong}{item.payload.get("unit", "")}。', 'contradicted'),
                ('mixed_scope', observed + '且这足以确认故障根因是数据库主机硬件损坏。', 'partial'),
                ('unobserved_cause', '故障根因已确认为数据库主机硬件损坏。', 'insufficient')]
    return [(name, {'claims': [{'statement': statement, 'refs': [{'reference_id': ref}],
                               'counter_refs': [], 'level': 'mechanism'}],
                    'next_steps': [], 'gaps': []}, relation) for name, statement, relation in controls]


def capture_review(case, world, model, limits):
    from tools.executor import ToolExecutor
    from workflow.v2.contracts import parse_draft
    from workflow.v2.review import review_draft
    provider = world.provider(case)
    rows = []
    # Each control is evaluated independently against exactly the same source world.
    for index in range(5):
        events = []
        context = RunContext(limits=limits, scope=provider.ticket().scope,
                             emit=lambda e: events.append({**e, 'seq': len(events) + 1}))
        executor = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
        metric = sorted({r['metric'] for r in provider._data['metrics']})[0]
        executor.execute('get_service_metrics', {**{k: v for k, v in context.scope.model_dump(mode='json').items()
                                                  if k in ('start', 'end')}, 'metrics': [metric]})
        name, raw, expected = review_inputs(executor)[index]
        try:
            draft, errors = parse_draft(raw, executor)
            if errors:
                raise ValueError('Source-constructed draft invalid')
            batch = review_draft(model, draft, executor, context, allow_correction=True)
            report = {'review': jsonable(batch), 'run_summary': context.summary(), 'events': events}
        except Exception as exc:
            report = {'execution_error': safe_exception(exc), 'run_summary': context.summary(), 'events': events}
        report.update(control=name, provisional_expected_relation=expected, draft_input=raw,
                      evidence=[e.model_dump(mode='json') for e in executor.evidence.values()])
        rows.append(report)
    return rows


def engineering_metrics(report):
    events = report.get('events', [])
    types = [e.get('type') for e in events]
    summary = report.get('run_summary', {})
    return {'status': report.get('status'), 'review_status': report.get('review_status'),
            'evidence_count': len(report.get('evidence', [])),
            'model_calls': summary.get('model_calls'), 'tool_calls': summary.get('tool_calls'),
            'token_usage': summary.get('token_usage'), 'duration_ms': summary.get('duration_ms'),
            'stop_reason': report.get('stop_reason') or summary.get('termination_reason'),
            'validation_failures': types.count('validation_failure'),
            'query_reuse_events': types.count('query_reused'),
            'execution_error': report.get('execution_error'),
            'semantic_metrics': {'critical_observation_omission': None, 'causal_overreach': None,
                                 'false_accept': None, 'false_reject': None},
            'semantic_quality_status': 'pending_human_review'}


def review_control_metrics(report):
    items = report.get('review', {}).get('items', [])
    item = items[0] if items else {}
    relation = item.get('relation')
    return {'reference_kind': 'provisional_source_entailment_control',
            'expected_relation': report['provisional_expected_relation'],
            'observed_relation': relation, 'protocol_status': item.get('protocol_status', 'not_run'),
            'agrees_with_provisional_reference': relation == report['provisional_expected_relation'],
            'human_verified': False}


def summarize_review_controls(runs):
    controls = [row for row in runs if 'review_control' in row]
    grouped = {}
    for row in controls:
        grouped.setdefault((row['case_id'], row['control']), []).append(row['review_control']['observed_relation'])
    return {'reference_kind': 'provisional_source_entailment_controls_not_diagnostic_accuracy',
            'controls': len(controls),
            'agreement_count': sum(row['review_control']['agrees_with_provisional_reference'] for row in controls),
            'repeated_groups': sum(len(values) > 1 for values in grouped.values()),
            'groups_with_relation_changes': sum(len(set(values)) > 1 for values in grouped.values()),
            'human_verified': False}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--scripted', action='store_true')
    modes.add_argument('--live', action='store_true')
    parser.add_argument('--stage', choices=('e2e', 'review'), default='e2e')
    parser.add_argument('--cases', nargs='+', default=list(DEFAULT_CASES))
    parser.add_argument('--arms', nargs='+', choices=('legacy', 'v2'))
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--model-budget', type=int, default=12)
    parser.add_argument('--tool-budget', type=int, default=8)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--anonymize', action='store_true',
                        help='Rename observation identifiers and supply evaluation-only public quantity metadata')
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'autonomous-evaluation')
    args = parser.parse_args(argv)
    args.arms = args.arms or (['v2'] if args.scripted or args.stage == 'review' else ['legacy', 'v2'])
    if not 1 <= args.repeats <= 5 or not 3 <= args.model_budget <= 32 or not 1 <= args.tool_budget <= 48 \
            or not 20 <= args.seconds <= 300 or len(args.arms) != len(set(args.arms)):
        parser.error('Invalid repeats, budgets or duplicate arms')
    if (args.scripted or args.stage == 'review') and args.arms != ['v2']:
        parser.error('Scripted controls and isolated review currently support --arms v2 only')
    if args.live and (args.max_model_calls is None or args.budget_ledger is None):
        parser.error('Live needs --max-model-calls and --budget-ledger; no request sent')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 180:
        parser.error('--max-model-calls must be 1..180 for the entire experiment')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        world = FrozenWorld(args.cases, anonymize=args.anonymize)
        config = IncidentConfig.from_file()
        if args.model:
            config = replace(config, model=args.model)
        plan = make_plan(args, config, world)
        if not args.live and not args.scripted:
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, **plan}, ensure_ascii=False, indent=2))
            return 0
        if args.live and (not config.api_key or config.api_key == 'test-only'):
            parser.error('Configured model credentials required; no request sent')
        directory = args.output / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'plan.json', plan)
        budget = GlobalPaidBudget(args.budget_ledger, args.max_model_calls) if args.live else None
        if args.live:
            from agents.models import build_model
            model = BudgetedModel(build_model(config.api_key, config.model, base_url=config.base_url,
                extra_body=config.extra_body, max_output_tokens=MODEL_SETTINGS['max_output_tokens'],
                request_timeout=MODEL_SETTINGS['request_timeout']), budget)
        bundle = {'plan': plan, 'runs': [], 'semantic_quality_status': plan['semantic_quality_status']}
        human = {'status': 'pending', 'reviewer': None, 'runs': []}
        for case, arm, repeat in schedule(args.cases, args.arms, args.repeats):
            if implementation_version() != plan['source_version']:
                raise ValueError('Source changed after experiment freeze')
            remaining = budget.remaining() if budget else None
            if budget and (budget.session_requests >= args.max_model_calls
                    or budget.snapshot().get('admission_closed') or remaining['requests'] < 1
                    or remaining['input_tokens'] < 1024
                    or remaining['output_tokens'] < MODEL_SETTINGS['max_output_tokens']
                    or remaining['model_seconds'] < MODEL_SETTINGS['request_timeout']):
                bundle['stopped'] = 'GLOBAL_PAID_BUDGET_EXHAUSTED'
                break
            if args.scripted:
                from workflow.v2.scripted import scripted_model
                model = scripted_model(world.provider(case))
            reports = (capture_review(case, world, model, run_limits(args)) if args.stage == 'review'
                       else [capture_e2e(case, arm, world, model, run_limits(args))])
            for index, report in enumerate(reports):
                name = f'{case}-{arm}-r{repeat}' + (f'-c{index + 1}' if args.stage == 'review' else '')
                report.update(experiment_arm=arm, repeat=repeat, case_id=case,
                              execution_mode=plan['execution_mode'], source_version=plan['source_version'],
                              semantic_quality_status=plan['semantic_quality_status'])
                path = directory / f'{name}.json'
                write_json(path, report)
                entry = {'case_id': case, 'arm': arm, 'repeat': repeat, 'report_file': path.name,
                         'report_sha256': digest(path.read_bytes()), 'engineering': engineering_metrics(report)}
                entry['engineering']['semantic_quality_status'] = plan['semantic_quality_status']
                if args.stage == 'review':
                    entry.update(control=report['control'], provisional_expected_relation=report['provisional_expected_relation'])
                    entry['review_control'] = review_control_metrics(report)
                bundle['runs'].append(entry)
                human['runs'].append({'report_file': path.name, 'report_sha256': entry['report_sha256'],
                    'reviewer': None, 'approved': None, 'critical_observation_omission': None,
                    'causal_overreach': None, 'false_accept': None, 'false_reject': None, 'notes': None})
            if budget:
                bundle['paid_budget'] = budget.summary()
            if args.stage == 'review':
                bundle['provisional_review_controls'] = summarize_review_controls(bundle['runs'])
            write_json(directory / 'bundle.json', bundle)
            write_json(directory / 'human-review.pending.json', human)
            print(json.dumps({'case_id': case, 'arm': arm, 'repeat': repeat, 'reports': len(reports),
                              'directory': str(directory), 'semantic_quality_status': plan['semantic_quality_status']},
                             ensure_ascii=False), flush=True)
        if budget:
            bundle['paid_budget'] = budget.summary()
        write_json(directory / 'bundle.json', bundle)
        write_json(directory / 'human-review.pending.json', human)
        print(json.dumps({'directory': str(directory), 'runs': len(bundle['runs']),
                          'semantic_quality_status': plan['semantic_quality_status'],
                          'paid_budget': budget.summary() if budget else {'requests': 0}}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Local config, malformed ledger and runtime text are deliberately omitted.
        print(json.dumps({'evaluation_error': safe_exception(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
