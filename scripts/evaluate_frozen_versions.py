"""Paired, isolated execution of frozen original/current incident applications.

The parent is the only model/paid-ledger owner. Workers import one application
snapshot and relay native model calls through an authenticated-by-parent pipe;
they never receive model credentials or access the paid ledger. Native legacy
parallel calls remain concurrent in the parent's thread pool.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('original', 'current')
LIMITS = {'model_calls': 12, 'tool_calls': 8, 'web_calls': 12, 'seconds': 180,
          'reserve_model_calls': 2, 'reserve_seconds': 15, 'input_chars': 48000,
          'snippet_chars': 1800, 'max_sources': 10}
SETTINGS = {'temperature': 0, 'max_output_tokens': 2200, 'request_timeout': 30,
            'max_retries': 0, 'response_format': 'json_object'}
RPC_PREFIX = 'FROZEN_VERSION_RPC '
EVALUATION_FILES = ('scripts/evaluate_frozen_versions.py', 'evals/frozen_version_cases.py',
                    'scripts/evaluate_autonomous.py', 'evals/closed_loop_cases.py')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def safe_error(exc):
    return {'code': getattr(exc, 'code', 'INTERNAL_ERROR'), 'exception_type': type(exc).__name__}


def application_freeze(source_root):
    root = Path(source_root).resolve()
    app = root / 'app'
    if not (app / 'runtime' / 'context.py').is_file():
        raise ValueError('Frozen application root is missing')
    files, digest = {}, hashlib.sha256()
    for path in sorted(app.rglob('*.py')):
        relative = path.relative_to(app).as_posix()
        raw = path.read_bytes()
        files['app/' + relative] = hashlib.sha256(raw).hexdigest()
        digest.update(relative.encode())
        digest.update(raw)
    return {'source_root': str(root), 'app_root': str(app), 'source_version': digest.hexdigest()[:16],
            'app_source_sha256': files}


def schedule(cases, repeats):
    for repeat in range(1, repeats + 1):
        for index, case in enumerate(cases):
            arms = ARMS if (repeat + index) % 2 else tuple(reversed(ARMS))
            for arm in arms:
                yield case, arm, repeat


class WorkerTransport:
    """One stdin reader dispatches responses to concurrent native model calls."""
    def __init__(self, input_stream, output_stream):
        self.input, self.output = input_stream, output_stream
        self.lock, self.pending, self.counter = Lock(), {}, 0
        Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        for line in self.input:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            with self.lock:
                target = self.pending.get(value.get('request_id'))
                if target:
                    target['value'] = value
                    target['event'].set()
        with self.lock:
            for target in self.pending.values():
                target['value'] = {'error': {'code': 'PARENT_PIPE_CLOSED'}}
                target['event'].set()

    def call(self, role, messages, tools):
        from runtime.context import ExecutionError
        from langchain_core.messages import AIMessage
        target = {'event': Event()}
        with self.lock:
            self.counter += 1
            request_id = self.counter
            self.pending[request_id] = target
            self.output.write(RPC_PREFIX + json.dumps({'type': 'model_request', 'request_id': request_id,
                'role': role, 'messages': [{'type': m.type, 'data': m.model_dump(mode='json')} for m in messages],
                'tools': tools}, ensure_ascii=False) + '\n')
            self.output.flush()
        try:
            if not target['event'].wait(90):
                raise ExecutionError('TIMEOUT', 'evaluation_pipe')
            value = target['value']
            if value.get('error'):
                raise ExecutionError(value['error']['code'], 'model')
            return AIMessage.model_validate(value['response'])
        finally:
            with self.lock:
                self.pending.pop(request_id, None)


class ProxyModel:
    json_output = True
    blind_review = False

    def __init__(self, transport, role, tools=()):
        self.transport, self.role, self.tools = transport, role, tools

    def bind_tools(self, tools):
        return ProxyModel(self.transport, self.role, tools)

    def invoke(self, messages):
        return self.transport.call(self.role, messages, self.tools)


class RequestBudget:
    """Record the request's own reservation even when other threads advance."""
    def __init__(self, owner, row):
        self.owner, self.row = owner, row

    def reserve(self, *args):
        reservation = self.owner.reserve(*args)
        self.row['charged_request'] = True
        return reservation

    def settle(self, *args):
        return self.owner.settle(*args)


class ModelBroker:
    def __init__(self, model, budget, model_name):
        self.model, self.budget, self.model_name = model, budget, model_name
        self.calls, self.lock = [], Lock()

    def handle(self, request):
        from langchain_core.messages import messages_from_dict
        from scripts.evaluate_autonomous import BudgetedModel
        messages = messages_from_dict(request['messages'])
        role, stage = request['role'], request['role']
        try:
            body = json.loads(messages[-1].content)
            stage = body.get('stage') or ('challenge' if 'challenge_policy_version' in body else role)
        except (TypeError, ValueError, AttributeError, IndexError):
            pass
        row = {'request_id': request['request_id'], 'role': role, 'stage': stage, 'model': self.model_name,
            'request_sha256': fingerprint({'messages': request['messages'], 'tools': request.get('tools', [])}),
            'output_sha256': None, 'response_model_name': None, 'usage': None, 'seconds': None,
            'error': None, 'charged_request': False}
        started = time.monotonic()
        try:
            model = self.model.bind_tools(request['tools']) if request.get('tools') else self.model
            if self.budget is not None:
                model = BudgetedModel(model, RequestBudget(self.budget, row))
            response = model.invoke(messages)
            usage = getattr(response, 'usage_metadata', None)
            if not usage:
                raw = getattr(response, 'response_metadata', {}).get('token_usage', {})
                usage = {'input_tokens': raw.get('prompt_tokens'), 'output_tokens': raw.get('completion_tokens')}
            if isinstance(usage, dict) and all(isinstance(usage.get(k), int) and usage[k] >= 0
                                             for k in ('input_tokens', 'output_tokens')):
                row['usage'] = {k: usage[k] for k in ('input_tokens', 'output_tokens')}
            resolved = getattr(response, 'response_metadata', {}).get('model_name')
            if isinstance(resolved, str) and len(resolved) <= 160:
                row['response_model_name'] = resolved
            row['output_sha256'] = fingerprint({'content': response.content,
                'tool_calls': getattr(response, 'tool_calls', []),
                'invalid_tool_calls': getattr(response, 'invalid_tool_calls', [])})
            return {'request_id': request['request_id'], 'response': response.model_dump(mode='json')}
        except Exception as exc:
            row['error'] = safe_error(exc)
            return {'request_id': request['request_id'], 'error': row['error']}
        finally:
            row['seconds'] = time.monotonic() - started
            with self.lock:
                self.calls.append(row)


def _refs(executor, selectors, *, short):
    result = []
    for row_id, field in selectors:
        evidence = next((item for item in executor.evidence.values() if item.locator.endswith('/' + row_id)), None)
        if evidence is None:
            raise ValueError('Fixed source was not acquired')
        option = next(option for option in evidence.reference_options if option.field_path == field)
        result.append({'reference_id': option.reference_id} if short else
                      executor.references[option.reference_id].model_dump(mode='json'))
    return result


def fixed_review(spec, provider, principal, models, context, events, arm):
    """Native contracts on both sides; only the accept decision is comparable."""
    from tools.executor import ToolExecutor
    executor = ToolExecutor(provider, principal, context.scope, context, role='single')
    provider._evaluation_executor = executor
    for check in spec['initial_checks']:
        result = executor.execute(check['tool'], check['args'])
        # The old reviewer reads the native tool_result event projection. These
        # events describe actual reads, exactly as CollectionRunner would emit.
        context.emit({'type': 'tool_result', 'role': 'investigation', 'name': check['tool'],
            'status': result.status, 'query_profile': dict(executor.last_query_profile),
            'evidence_ids': [e.evidence_id for e in result.evidence], 'sample_order': result.sample_order,
            'truncated': result.truncated, 'error': result.error.model_dump(mode='json') if result.error else None,
            'execution_source': 'frozen_fixed_review_actual_read'})
    controls = spec['review_controls']
    native_targets, mappings = {}, {}
    if arm == 'current':
        from workflow.v2.contracts import parse_draft
        from workflow.v2.review import review_draft
        from workflow.v2.engine import collection_view, run_challenge
        raw = {'claims': [], 'next_steps': [], 'gaps': []}
        for control in controls:
            native_id = control['target_id']
            mappings[native_id] = native_id
            if control['kind'] == 'factual_relation':
                value = {'statement': control['statement'], 'level': control.get('level', 'mechanism'),
                         'refs': _refs(executor, control['selectors'], short=True), 'counter_refs': []}
                raw['claims'].append(value)
            else:
                value = {'action': control['action'], 'condition': control['condition'], 'kind': 'manual_check',
                         'refs': _refs(executor, control['selectors'], short=True)}
                raw['next_steps'].append(value)
            native_targets[native_id] = value
        draft, errors = parse_draft(raw, executor)
        if errors:
            raise ValueError('Current fixed draft rejected')
        provider._evaluation_native = {'fixed_targets': native_targets, 'target_mapping': mappings,
            'drafts': [{'revision': 1, 'draft': draft.model_dump(mode='json')}]}
        batch = review_draft(models['reviewer'], draft, executor, context, collection=collection_view(executor))
        primary = batch.model_dump(mode='json')
        final, challenge = run_challenge(models, draft, batch, executor, context)
        report = {'status': 'fixed_review_completed', 'primary_review': primary,
                  'final_review': final.model_dump(mode='json'), 'challenge': challenge,
                  'fixed_targets': native_targets, 'drafts': [{'revision': 1, 'draft': draft.model_dump(mode='json')}],
                  'item_reviews': [row.model_dump(mode='json') for row in final.items]}
    else:
        from agents.contracts import DiagnosisDraft, Hypothesis, RecommendedAction
        from workflow.coordinator import Collaboration
        collaboration = Collaboration(models, provider, principal, context=context, event_log=events,
                                      investigation_mode='programmatic', knowledge_mode='programmatic')
        collaboration.executors['investigation'] = executor
        collaboration.evidence.update(executor.evidence)
        collaboration.references.update(executor.references)
        hypotheses, actions = [], []
        for control in controls:
            if control['kind'] == 'factual_relation':
                native_id = 'H' + control['target_id'][1:]
                value = {'hypothesis_id': native_id, 'cause': control['statement'],
                         'support_refs': _refs(executor, control['selectors'], short=False), 'counter_refs': [],
                         'pending_checks': [], 'level': control.get('level', 'mechanism'),
                         'evidence_explanation': ''}
                hypotheses.append(Hypothesis.model_validate_json(json.dumps(value)))
            else:
                native_id = control['target_id']
                value = {'action': control['action'], 'condition': control['condition'],
                         'expected_result': '取得当前未知信息，根据实际结果支持、收窄或保留判断，不预设结果。',
                         'risk': '人工只读核查，遵守既有授权范围。', 'requires_approval': False, 'kind': 'manual_check'}
                actions.append(RecommendedAction.model_validate(value))
            mappings[control['target_id']] = native_id
            native_targets[native_id] = value
        collaboration.draft = DiagnosisDraft(findings=[], hypotheses=hypotheses, recommended_actions=actions,
            missing_information=[], escalation_team=None)
        collaboration.drafts.append({'revision': 1, 'draft': collaboration.draft.model_dump(mode='json')})
        provider._evaluation_native = {'fixed_targets': native_targets, 'target_mapping': mappings,
                                       'drafts': collaboration.drafts}
        # Invoke the original reviewer contract, normalization and protection.
        # Calling review_draft would additionally schedule workflow follow-up;
        # this isolated control intentionally stops after its native verdict.
        from agents.contracts import ReviewDecision
        from runtime.context import activate
        with activate(context):
            result = collaboration.ask('reviewer', ReviewDecision, collaboration.reviewer_input(), terminal=True,
                                       validate=collaboration.validate_review)
        batch = result.model_dump(mode='json')
        report = {'status': 'fixed_review_completed', 'primary_review': batch, 'final_review': batch,
                  'fixed_targets': native_targets, 'drafts': collaboration.drafts,
                  'reviews': [{'revision': 1, 'review': batch}]}
    report.update(target_mapping=mappings, evidence=[e.model_dump(mode='json') for e in executor.evidence.values()],
                  collection=list(executor.check_results.values()), run_summary=context.summary(), events=events)
    return report


def normalize_report(native, *, arm, stage, controls=()):
    evidence = native.get('evidence', [])
    observations = [{'observation_id': row.get('locator', '').rsplit('/', 1)[-1],
        'evidence_id': row['evidence_id'], 'locator': row.get('locator', ''), 'kind': row.get('kind'),
        'payload': deepcopy(row.get('payload', {})),
        'source_fields': {key: deepcopy(value) for key, value in row.items()
                          if key not in {'evidence_id', 'locator', 'reference_options'}}}
        for row in evidence]
    queries = native.get('collection')
    if queries is None:
        queries = [{key: event.get(key) for key in ('role', 'name', 'status', 'query_profile', 'truncated', 'evidence_ids', 'error')}
                   for event in native.get('events', []) if event.get('type') == 'tool_result']
    final = native.get('final_review', {})
    if not final:
        history = native.get('reviews', [])
        final = history[-1].get('review', {}) if history else {}
    assessments = final.get('items', native.get('item_reviews', [])) if arm == 'current' else final.get('assessments', [])
    by_id = {row['target_id']: row for row in assessments}
    drafts = native.get('drafts', [])
    draft = drafts[-1].get('draft', {}) if drafts else {}
    claims = draft.get('claims', []) if arm == 'current' else draft.get('hypotheses', [])
    normalized_claims = []
    for index, claim in enumerate(claims, 1):
        target_id = claim.get('id') or claim.get('hypothesis_id') or ('C' if arm == 'current' else 'H') + str(index)
        assessment = by_id.get(target_id, {})
        valid = assessment.get('protocol_status') == 'valid' if arm == 'current' else bool(assessment)
        verdict = assessment.get('relation') if arm == 'current' else assessment.get('verdict')
        stance = ('accepted' if verdict == 'supported' else 'rejected' if verdict in {'contradicted', 'not_supported'} else 'pending') if valid else 'not_assessed'
        normalized_claims.append({'id': target_id, 'statement': claim.get('statement', claim.get('cause', '')),
            'refs': claim.get('refs', claim.get('support_refs', [])), 'counter_refs': claim.get('counter_refs', []),
            'level': claim.get('level'), 'published_stance': stance})
    review_controls = []
    for control in controls:
        target_id = control['target_id']
        native_id = native.get('target_mapping', {}).get(target_id, target_id if arm == 'current' else target_id.replace('C', 'H', 1))
        row = by_id.get(native_id, {})
        valid = row.get('protocol_status') == 'valid' if arm == 'current' else bool(row)
        verdict = row.get('submitted_decision') if control['kind'] == 'check_appropriateness' and arm == 'current' else None
        verdict = verdict or row.get('relation' if arm == 'current' else 'verdict')
        text = control.get('original_text', control.get('statement', control.get('action', '')))
        review_controls.append({'target_id': target_id, 'native_target_id': native_id, 'kind': control['kind'],
            'original_text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'native_verdict': verdict,
            'accept': verdict in {'supported', 'appropriate'} if valid else None,
            'protocol_status': 'valid' if valid else 'not_assessed', 'native_fields': deepcopy(row)})
    failures = []
    history = deepcopy(native.get('protocol_failure_history', native.get('protocol_failures', [])))
    final_errors = {(row.get('stage'), row.get('code'), row.get('path'), row.get('target_id')): row
                    for row in native.get('protocol_failures', [])}
    for row in history:
        terminal = final_errors.get((row.get('stage'), row.get('code'), row.get('path'), row.get('target_id')))
        if terminal and 'recovered' in terminal:
            row['recovered'] = terminal['recovered']
    if not history:
        for index, event in enumerate(native.get('events', [])):
            if event.get('type') != 'validation_failure':
                continue
            outcome = next((row for row in native.get('evaluation_ask_outcomes', [])
                            if row['event_start'] <= index < row['event_end']
                            and row['role'] == event.get('role')), None)
            history.append({'stage': event.get('role', 'unknown'),
                'category': 'model_protocol' if outcome else 'unclassified',
                'code': event.get('code', event.get('reason', 'VALIDATION_FAILURE')),
                'recovered': bool(outcome and outcome['validated_return']), 'recovery_known': bool(outcome)})
    for row in history:
        code = row.get('code', 'UNKNOWN')
        category = ('budget' if 'BUDGET' in code else 'model_protocol' if row.get('category') in
                    {'model_protocol', 'syntax', 'shape', 'relation', 'reference', 'length'} or code in
                    {'MODEL_OUTPUT_INVALID', 'OUTPUT_TRUNCATED', 'STEP_INVALID'} else
                    'unclassified' if row.get('category') == 'unclassified' else 'other')
        recovered = bool(row.get('recovered'))
        terminal = not recovered and native.get('run_summary', {}).get('termination_reason') in {'MODEL_OUTPUT_INVALID', 'OUTPUT_TRUNCATED'}
        failures.append({'stage': row.get('stage', 'review'), 'category': category, 'code': code,
                         'recovered': recovered, 'terminal': terminal,
                         'recovery_known': row.get('recovery_known', 'recovered' in row)})
    for row in queries:
        if row.get('status') == 'error':
            error = row.get('error') or {}
            code = error.get('code', 'PROVIDER_ERROR') if isinstance(error, dict) else str(error)
            failures.append({'stage': 'tool', 'category': 'provider', 'code': code, 'recovered': False, 'terminal': False})
    if native.get('execution_error'):
        error = native['execution_error']
        code = error.get('code', 'INTERNAL_ERROR')
        category = ('model_protocol' if code in {'MODEL_OUTPUT_INVALID', 'OUTPUT_TRUNCATED'}
                    else 'budget' if 'BUDGET' in code else 'other')
        failures.append({'stage': 'workflow', 'category': category, 'code': code, 'recovered': False, 'terminal': True})
    audit_items = []
    for index, item in enumerate(draft.get('facts', draft.get('findings', [])), 1):
        target_id = item.get('id') or 'F' + str(index)
        audit_items.append({'id': target_id, 'kind': 'finding', 'text': item.get('statement', ''),
            'refs': item.get('refs', []), 'counter_refs': [], 'native_approval': by_id.get(target_id)})
    for claim in normalized_claims:
        audit_items.append({'id': claim['id'], 'kind': 'hypothesis', 'text': claim['statement'],
            'level': claim.get('level'), 'refs': claim['refs'], 'counter_refs': claim['counter_refs'],
            'native_approval': by_id.get(claim['id']), 'published_stance': claim['published_stance']})
    for index, item in enumerate(draft.get('next_steps', draft.get('recommended_actions', [])), 1):
        target_id = item.get('id') or 'A' + str(index)
        audit_items.append({'id': target_id, 'kind': 'action', 'text': item.get('action', ''),
            'condition': item.get('condition', ''), 'expected_result': item.get('expected_result'),
            'refs': item.get('refs', []), 'counter_refs': [], 'native_approval': by_id.get(target_id)})
    covered_actions = {item['id'] for item in audit_items if item['kind'] == 'action'}
    for index, item in enumerate(native.get('pending_actions', []), 1):
        target_id = item.get('target_id') or 'P' + str(index)
        if target_id in covered_actions:
            continue
        audit_items.append({'id': target_id, 'kind': 'pending_action', 'text': item.get('action', ''),
            'condition': item.get('condition', ''), 'expected_result': item.get('expected_result'),
            'refs': item.get('refs', []), 'counter_refs': [], 'native_approval': None})
    return {'observations': observations, 'queries': queries, 'final_claims': normalized_claims,
            'review_controls': review_controls, 'failures': failures, 'audit_items': audit_items,
            'coverage': {'normalization_complete': not bool(native.get('execution_error')),
                         'unsupported_fields': ['cross_version_four_class_relation_mapping', 'human_causal_accuracy']}}


def _loaded_application_paths(app_root):
    problems, loaded = [], {}
    prefixes = {'agents', 'api', 'config', 'evidence', 'providers', 'runtime', 'tools', 'workflow',
                'storage', 'observability', 'repairs', 'knowledge', 'incidents'}
    app_root = Path(app_root).resolve()
    for name, module in list(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if filename and name.split('.', 1)[0] in prefixes:
            path = Path(filename).resolve()
            loaded[name] = str(path)
            if not path.is_relative_to(app_root):
                problems.append(name)
    if problems:
        raise ValueError('Application import escaped frozen root')
    return loaded


def _worker_provider(spec):
    from api.auth import Principal
    from providers.fixtures import FixtureProvider, ProviderError
    from providers.registry import ServiceBinding
    principal = Principal('frozen_comparison_synthetic', 'evaluation_reader')

    class FrozenProvider(FixtureProvider):
        name = 'frozen_cross_version_world'

        def __init__(self):
            self._data, self.principal = deepcopy(spec['world']), principal
            scope = self.ticket().scope
            if spec.get('capability_metadata'):
                self.binding = ServiceBinding(principal.tenant_id, (principal.user_id,), scope.service,
                    scope.environment, scope.service_version, scope.allowed_dependencies,
                    observations=deepcopy(spec['capability_metadata']))

        def query(self, name, args, scope, *, max_metric_points=60):
            self.authorize(self.principal, scope)
            if name in self._data.get('source_errors', {}):
                raise ProviderError(self._data['source_errors'][name])
            rows, truncated = super().query(name, args, scope)
            if name == 'get_service_metrics' and max_metric_points < 60:
                counts, selected = {}, []
                for row in rows:
                    metric = row['metric']
                    if counts.get(metric, 0) < max_metric_points:
                        selected.append(row)
                        counts[metric] = counts.get(metric, 0) + 1
                return selected, truncated or len(selected) < len(rows)
            return rows, truncated

    return FrozenProvider(), principal


def worker_main(source_root):
    # This process has imported only stdlib before selecting its application.
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    frozen = Path(source_root).resolve()
    app = frozen / 'app'
    current_app = (ROOT / 'app').resolve()
    sys.path[:] = [str(app), str(ROOT), *[p for p in sys.path if Path(p or '.').resolve() not in {current_app, app, ROOT}]]
    initial = json.loads(sys.stdin.readline())
    if application_freeze(frozen) != initial['application']:
        raise ValueError('Worker source does not match frozen plan')
    from runtime.context import Limits, RunContext
    provider, principal = _worker_provider(initial['case'])
    events = []
    context = RunContext(scope=provider.ticket().scope, limits=Limits(**initial['limits']),
                         emit=lambda event: events.append({**event, 'seq': len(events) + 1}))
    transport = WorkerTransport(sys.stdin, sys.stdout)
    roles = ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer', 'challenger')
    models = {role: ProxyModel(transport, role) for role in roles}
    ask_outcomes = []
    if initial['arm'] == 'original':
        from workflow.coordinator import Collaboration
        original_ask = Collaboration.ask

        def observed_ask(self, role, schema, *args, **kwargs):
            begin, validated = len(events), False
            try:
                value = original_ask(self, role, schema, *args, **kwargs)
                quarantined = any(row.get('type') == 'diagnosis_actions_quarantined' for row in events[begin:])
                validated = not quarantined
                return value
            finally:
                ask_outcomes.append({'role': role, 'schema_name': schema.__name__, 'event_start': begin,
                                     'event_end': len(events), 'validated_return': validated})

        Collaboration.ask = observed_ask
    try:
        if initial['stage'] == 'review':
            native = fixed_review(initial['case'], provider, principal, models if initial['arm'] == 'current'
                                  else {k: v for k, v in models.items() if k != 'challenger'},
                                  context, events, initial['arm'])
        elif initial['arm'] == 'original':
            from workflow.coordinator import collaborate
            native = collaborate({k: v for k, v in models.items() if k != 'challenger'}, provider, principal,
                context=context, event_log=events, investigation_mode='programmatic', knowledge_mode='programmatic',
                parallel_collection=True)
        else:
            from workflow.v2.engine import investigate
            native = investigate(models, provider, principal, context=context, event_log=events,
                                 enable_rework=True, enable_challenge=True)
    except Exception as exc:
        acquired = getattr(provider, '_evaluation_executor', None)
        native = {**deepcopy(getattr(provider, '_evaluation_native', {})),
                  'status': 'failed', 'execution_error': safe_error(exc), 'events': events,
                  'evidence': [e.model_dump(mode='json') for e in acquired.evidence.values()] if acquired else [],
                  'collection': list(acquired.check_results.values()) if acquired else [],
                  'run_summary': context.summary()}
    native['evaluation_ask_outcomes'] = ask_outcomes
    loaded = _loaded_application_paths(app)
    if application_freeze(frozen) != initial['application']:
        raise ValueError('Application changed during run')
    output = {'type': 'worker_result', 'native_report': native,
              'normalized': normalize_report(native, arm=initial['arm'], stage=initial['stage'],
                                              controls=initial['case'].get('review_controls', [])),
              'run_summary': native.get('run_summary', context.summary()),
              'loaded_application_modules': loaded,
              'observed_world_sha256': fingerprint(provider._data),
              'observed_capability_sha256': fingerprint(provider.binding.observations if hasattr(provider, 'binding') else {})}
    with transport.lock:
        sys.stdout.write(RPC_PREFIX + json.dumps(output, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    return 0


def worker_case(spec):
    """Only observations, fixed questions and source selectors cross the pipe."""
    controls = [{key: deepcopy(value) for key, value in control.items() if key not in
                {'expected_accept', 'reference_relation', 'reference_kind'}}
                for control in spec['review_controls']]
    return {'world': deepcopy(spec['world']), 'capability_metadata': deepcopy(spec['capability_metadata']),
            'initial_checks': deepcopy(spec['initial_checks']), 'review_controls': controls}


def execute_worker(spec, application, arm, stage, broker, *, timeout=300):
    """Workers never share a ledger. Only this parent admits real requests."""
    command = [sys.executable, '-u', str(Path(__file__).resolve()), '--worker', application['source_root']]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', bufsize=1, cwd=ROOT)
    incoming, write_lock = queue.Queue(), Lock()
    initial = {'application': application, 'limits': LIMITS, 'case': worker_case(spec), 'arm': arm, 'stage': stage}
    process.stdin.write(json.dumps(initial, ensure_ascii=False) + '\n')
    process.stdin.flush()

    def receive():
        for line in process.stdout:
            if not line.startswith(RPC_PREFIX):
                continue  # Native diagnostics are not a trusted protocol or saved output.
            try:
                incoming.put(json.loads(line[len(RPC_PREFIX):]))
            except ValueError:
                continue
        incoming.put({'type': 'worker_eof'})

    def answer(request):
        response = broker.handle(request)
        with write_lock:
            try:
                process.stdin.write(json.dumps(response, ensure_ascii=False) + '\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    started, result = time.monotonic(), None
    Thread(target=receive, daemon=True).start()
    with ThreadPoolExecutor(max_workers=6) as executor:
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('Worker deadline')
                message = incoming.get(timeout=remaining)
                if message.get('type') == 'model_request':
                    executor.submit(answer, message)
                elif message.get('type') == 'worker_result':
                    result = message
                    break
                elif message.get('type') == 'worker_eof':
                    raise RuntimeError('Frozen worker exited before its report')
        finally:
            if result is None and process.poll() is None:
                process.kill()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
    result.pop('type', None)
    result['worker_wall_seconds'] = time.monotonic() - started
    return result


def evaluation_freeze():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in EVALUATION_FILES}


def plan_for(args, config, specs):
    from evals.frozen_version_cases import VERSION, freeze_case_spec, audit_rubric
    cases = args.cases or [key for key, spec in specs.items() if args.stage in spec['stages']]
    if not cases or len(set(cases)) != len(cases) or any(key not in specs or args.stage not in specs[key]['stages'] for key in cases):
        raise ValueError('Cases do not match the requested stage')
    applications = {'original': application_freeze(args.original_root), 'current': application_freeze(args.current_root)}
    if applications['original']['app_root'] == applications['current']['app_root']:
        raise ValueError('Distinct frozen application roots are required')
    planned = len(cases) * 2 * args.repeats
    plan = {'version': 'frozen-native-version-comparison-v1', 'stage': args.stage,
            'execution_mode': 'live' if args.live else 'plan_only', 'arms': list(ARMS),
            'applications': applications, 'cases': cases, 'repeats': args.repeats, 'planned_runs': planned,
            'world_freezes': [freeze_case_spec(specs[key]) for key in cases],
            'review_targets': {key: [{field: deepcopy(control[field]) for field in
                ('target_id', 'kind', 'original_text_sha256', 'expected_accept', 'reference_relation')}
                for control in specs[key]['review_controls']] for key in cases},
            'review_positive_verdicts': {'original': ['supported'], 'current': ['supported', 'appropriate']},
            'review_uncertain_policy': 'Valid uncertain/partial/insufficient means not accepted; protocol failure stays unassessed.',
            'review_stage_policy': {'original': 'native ReviewDecision and validation; no blind challenger in original configuration',
                                   'current': 'native primary review followed by native Flash blind challenge'},
            'limits': dict(LIMITS), 'model': args.model, 'model_settings': dict(SETTINGS),
            'provider_configuration_sha256': fingerprint({'base_url': config.base_url, 'extra_body': config.extra_body}),
            'evaluation_source_sha256': evaluation_freeze(), 'dataset_version': VERSION,
            'maximum_model_calls': min(planned * LIMITS['model_calls'], args.max_model_calls)
                if args.max_model_calls else planned * LIMITS['model_calls'],
            'native_parallel_collection': True, 'paid_ledger_owner': 'one_parent_process_with_thread_safe_admission',
            'schedule': [{'case_id': case, 'arm': arm, 'repeat': repeat}
                         for case, arm, repeat in schedule(cases, args.repeats)],
            'audit_rubric': audit_rubric(), 'reference_kind': 'source_constructed_provisional_not_human_gold',
            'human_semantic_gate': 'pending', 'production_accuracy': 'not_established',
            'rollout_recommendation': 'hold', 'read_only': True,
            'completion_warning': 'Native completion contracts differ; completion rates are not diagnosis accuracy.',
            'estimated_cost': None}
    if args.prespecified_plan:
        declaration = json.loads(args.prespecified_plan.read_text(encoding='utf-8-sig'))
        expected = declaration['stages'][args.stage]
        if plan['cases'] != expected['cases'] or args.repeats != expected['repeats'] \
                or plan['limits'] != declaration['limits'] or args.model != declaration['model'] \
                or SETTINGS != declaration.get('model_settings', declaration.get('settings')) \
                or plan['provider_configuration_sha256'] != declaration['provider_configuration_sha256']:
            raise ValueError('Run configuration differs from predeclared comparison')
        freezes = declaration.get('case_freezes', {})
        if freezes and any(freezes.get(row['case_id']) != row for row in plan['world_freezes']):
            raise ValueError('Observation world differs from predeclaration')
        if declaration.get('applications') and plan['applications'] != declaration['applications']:
            raise ValueError('Application differs from predeclaration')
        plan['prespecified_plan'] = {'path': str(args.prespecified_plan.resolve()),
            'sha256': hashlib.sha256(args.prespecified_plan.read_bytes()).hexdigest()}
        plan['pricing_scope'] = declaration.get('pricing_scope')
    return plan


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--original-root', type=Path)
    parser.add_argument('--current-root', type=Path)
    parser.add_argument('--stage', choices=('e2e', 'review'), default='e2e')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--model', default='qwen3.7-flash')
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--prespecified-plan', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'frozen-version-study' / 'runs')
    args = parser.parse_args(argv)
    if args.worker:
        return parser, args
    if not args.original_root or not args.current_root:
        parser.error('Both frozen --original-root and --current-root are required')
    if not 1 <= args.repeats <= 5 or not args.model:
        parser.error('Invalid repetition count or model')
    if args.live and (args.budget_ledger is None or args.max_model_calls is None):
        parser.error('Live requires existing single-owner --budget-ledger and --max-model-calls')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 1200:
        parser.error('Maximum paid requests must be 1..1200')
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
    sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
    from config.incident import IncidentConfig
    from evals.frozen_version_cases import build_case_specs, freeze_case_spec
    from scripts.evaluate_autonomous import GlobalPaidBudget
    try:
        specs, config = build_case_specs(ROOT), IncidentConfig.from_file()
        plan = plan_for(args, config, specs)
        if not args.live:
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, **plan}, ensure_ascii=False, indent=2))
            return 0
        if not config.api_key or config.api_key == 'test-only':
            parser.error('Configured model credentials required; no request sent')
        budget = GlobalPaidBudget(args.budget_ledger, args.max_model_calls)
        from agents.models import build_model
        base_model = build_model(config.api_key, args.model, base_url=config.base_url, extra_body=config.extra_body,
            max_output_tokens=SETTINGS['max_output_tokens'], request_timeout=SETTINGS['request_timeout'])
        directory = args.output / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'plan.json', plan)
        write_json(directory / 'frozen-case-specs.json', {case: specs[case] for case in plan['cases']})
        runs = []
        human = {'status': 'pending', 'reviewer': None, 'runs': []}

        def save():
            write_json(directory / 'bundle.json', {'plan': plan, 'runs': runs, 'paid_budget': budget.summary(),
                'human_semantic_gate': 'pending', 'rollout_recommendation': 'hold'})
            write_json(directory / 'human-review.pending.json', human)

        save()
        frozen_worlds = {row['case_id']: row for row in plan['world_freezes']}
        for case_id, arm, repeat in schedule(plan['cases'], args.repeats):
            if evaluation_freeze() != plan['evaluation_source_sha256'] or any(
                    application_freeze(application['source_root']) != application for application in plan['applications'].values()):
                raise ValueError('Frozen application or evaluator changed')
            if freeze_case_spec(specs[case_id]) != frozen_worlds[case_id]:
                raise ValueError('Frozen observation world changed')
            if budget.session_requests >= args.max_model_calls or budget.snapshot().get('admission_closed') \
                    or budget.remaining()['requests'] < 1:
                break
            broker = ModelBroker(base_model, budget, args.model)
            report = execute_worker(specs[case_id], plan['applications'][arm], arm, args.stage, broker)
            if report['observed_world_sha256'] != frozen_worlds[case_id]['world_sha256'] \
                    or report['observed_capability_sha256'] != frozen_worlds[case_id]['metadata_sha256']:
                raise ValueError('Worker observation input differs from frozen plan')
            report.update(case_id=case_id, arm=arm, repeat=repeat, stage=args.stage, execution_mode='live',
                          application=plan['applications'][arm], world_freeze=frozen_worlds[case_id],
                          model_call_log=sorted(broker.calls, key=lambda row: row['request_id']),
                          audit_ticket_question={key: deepcopy(specs[case_id]['world']['ticket'][key])
                              for key in ('title', 'symptoms', 'purpose', 'scope')
                              if key in specs[case_id]['world']['ticket']})
            name = f'{case_id}-{arm}-r{repeat}.json'
            write_json(directory / name, report)
            entry = {'case_id': case_id, 'arm': arm, 'repeat': repeat, 'report_file': name,
                     'report_sha256': hashlib.sha256((directory / name).read_bytes()).hexdigest()}
            runs.append(entry)
            human['runs'].append({**entry, 'reviewer': None, 'approved': None, 'notes': None})
            save()
            print(json.dumps({'case_id': case_id, 'arm': arm, 'repeat': repeat, 'directory': str(directory),
                'status': report['native_report'].get('status'), 'paid_requests': len([r for r in broker.calls if r['charged_request']]),
                'tool_calls': report['run_summary'].get('tool_calls')}, ensure_ascii=False), flush=True)
        save()
        print(json.dumps({'directory': str(directory), 'runs': len(runs), 'expected_runs': plan['planned_runs'],
                          'paid_budget': budget.summary(), 'rollout_recommendation': 'hold'}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'evaluation_error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
