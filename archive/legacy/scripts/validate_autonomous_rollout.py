"""Local ASGI admission/SSE/rollback validation; never a production rollout approval."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

import httpx
from fastapi import FastAPI

from api.auth import Principal, get_current_principal
from api.incidents import router as incidents_router
from api.runs import router as runs_router
from config.incident import IncidentConfig
from evals.common import write_json
from providers.bound import catalog
from providers.registry import ServiceRegistry
from runtime.context import ExecutionError, Limits
from runtime.version import implementation_version
from scripts.evaluate_autonomous import BudgetedModel, FrozenWorld, GlobalPaidBudget, MODEL_SETTINGS
from storage.runs import MemoryRunStore
from workflow.dependencies import get_workflow_service
from workflow.rollout import AcceptanceGate, RolloutPolicy, configuration_fingerprint
from workflow.service import WorkflowService
from workflow.v2.review import REVIEW_POLICY_VERSION

PRINCIPAL = Principal('local_rollout_validation', 'reader')
GATE_KIND = 'local_ephemeral_gate_fixture_not_deployment_approval'
LOCAL_ENV = {'INCIDENT_MAX_MODEL_CALLS': '12', 'INCIDENT_MAX_TOOL_CALLS': '8',
             'INCIDENT_MAX_SECONDS': '180', 'INCIDENT_MAX_INPUT_CHARS': '48000',
             'INCIDENT_MAX_WEB_CALLS': '12'}


class ValidationFailure(Exception):
    def __init__(self, code):
        self.code = code


def error_code(exc):
    return {'code': exc.code if isinstance(exc, (ExecutionError, ValidationFailure)) else 'INTERNAL_ERROR'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def local_limits():
    return Limits(model_calls=12, tool_calls=8, seconds=180.0, reserve_model_calls=3,
                  reserve_seconds=20, input_chars=48000)


def plan_for(args, config):
    world = FrozenWorld([args.case])
    # This uses the same canonical settings as IncidentAdapter.metadata.
    settings = {'model': config.model, 'base_url': config.base_url, 'extra_body': config.extra_body,
                'temperature': 0, 'max_tokens': 2200}
    model_hash = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    limits = asdict(local_limits())
    return {'schema_version': 'rollout-asgi-validation-v1', 'case': args.case,
        'execution_mode': 'live' if args.live else 'plan_only', 'source_version': implementation_version(),
        'review_policy_version': REVIEW_POLICY_VERSION, 'world': world.freeze(),
        'model_settings': {'model': config.model, **MODEL_SETTINGS}, 'model_settings_sha256': model_hash,
        'configuration_sha256': configuration_fingerprint(model_hash, 'closed_loop', True, limits,
                                                           execution_mode='live'),
        'limits_per_run': limits, 'planned_runs': 2, 'planned_arms': ['closed_loop', 'legacy_after_rollback'],
        'max_model_calls': args.max_model_calls, 'gate_fixture': GATE_KIND,
        'gate_is_deployment_approval': False, 'production_rollout': False,
        'human_evaluation': 'pending', 'semantic_quality_status': 'not_established',
        'data_source': 'public_synthetic_demo', 'storage': 'MemoryRunStore',
        'provider_production_reads': False, 'production_dual_reads': False,
        'transport': 'in_process_ASGI', 'read_only': True}


def model_factory(config, budget):
    def factory(api_key, model):
        from agents.models import build_model
        wrapped = BudgetedModel(build_model(api_key, model, base_url=config.base_url,
            extra_body=deepcopy(config.extra_body), max_output_tokens=MODEL_SETTINGS['max_output_tokens'],
            request_timeout=MODEL_SETTINGS['request_timeout']), budget)
        # Legacy validates exactly five roles; v2 challenger intentionally falls
        # back to the same investigation model through its existing selector.
        return {role: wrapped for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}
    return factory


def sse_events(text):
    events = []
    for line in text.splitlines():
        if line.startswith('data: '):
            try:
                value = json.loads(line[6:])
            except (TypeError, ValueError):
                raise ValidationFailure('SSE_INVALID_JSON') from None
            if not isinstance(value, dict):
                raise ValidationFailure('SSE_INVALID_EVENT')
            events.append(value)
    return events


def ensure_response(response, expected):
    if response.status_code != expected:
        raise ValidationFailure('UNEXPECTED_HTTP_STATUS')
    try:
        return response.json()
    except (TypeError, ValueError):
        raise ValidationFailure('HTTP_INVALID_JSON') from None


def exhausted(result):
    summary = result.get('run_summary', {})
    failures = [*summary.get('errors', []), *result.get('protocol_failures', [])]
    return (summary.get('termination_reason') in {'BUDGET_EXCEEDED', 'TIME_BUDGET_EXCEEDED'}
            or any(row.get('code') in {'BUDGET_EXCEEDED', 'TIME_BUDGET_EXCEEDED'} for row in failures))


async def run_validation(config, budget, plan):
    """Exercise actual routes and worker admission; only model and storage are injected."""
    report = {'plan': plan, 'gate_fixture': GATE_KIND, 'production_rollout': False,
              'human_evaluation': 'pending', 'deployment_acceptance': 'hold',
              'checks': {}, 'errors': [], 'runs': {}, 'admissions': {}, 'sse': {}}
    release = Event()
    store = MemoryRunStore()
    service = WorkflowService('unused', store=store, config=config, service_registry=ServiceRegistry(),
        max_concurrency=2, incident_engine='legacy', incident_rollout={'mode': 'off'},
        incident_model_factory=model_factory(config, budget))
    original_execute = service._execute

    def gated_execute(run_id, request):
        # The existing worker pool owns these workers; no additional worker is created here.
        release.wait()
        original_execute(run_id, request)

    service._execute = gated_execute
    identity = {'principal': PRINCIPAL}
    app = FastAPI()
    app.include_router(incidents_router)
    app.include_router(runs_router)
    app.dependency_overrides[get_workflow_service] = lambda: service
    app.dependency_overrides[get_current_principal] = lambda: identity['principal']
    execution = {'request_key': 'stable-api-key', 'execution_mode': 'live', 'model_budget': 12, 'tool_budget': 8}
    runs = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local-validation') as client:
            fields = next(row['fields'] for row in catalog(PRINCIPAL) if row['case_id'] == plan['case'])
            first = ensure_response(await client.post('/api/v1/incidents', json=fields), 201)
            service.configure_incident_rollout(RolloutPolicy(mode='full', variant='closed_loop'))
            preview = service._incident.metadata(execution, incident=first, principal=PRINCIPAL)
            if (preview['run_spec']['configuration_sha256'] != plan['configuration_sha256']
                    or preview['run_spec']['limits'] != plan['limits_per_run']):
                raise ValidationFailure('PLAN_CONFIGURATION_CHANGED')
            # A local routing fixture is deliberately not a statement that semantic acceptance passed.
            gate = AcceptanceGate(source_version=plan['source_version'], review_policy_version=REVIEW_POLICY_VERSION,
                evaluation_hash=digest({'fixture_kind': GATE_KIND, 'configuration': plan['configuration_sha256']}),
                configuration_sha256=plan['configuration_sha256'], verdict='pass')
            service.configure_incident_rollout(RolloutPolicy(mode='full', variant='closed_loop', gate=gate))
            accepted = ensure_response(await client.post(f"/api/v1/incidents/{first['id']}/diagnoses", json=execution), 202)
            runs.append(accepted['run_id'])
            first_pending = ensure_response(await client.get(accepted['result_url']), 200)
            frozen = deepcopy(first_pending['config']['run_spec'])
            report['admissions']['first'] = {'response': accepted, 'run_spec': frozen}
            service.configure_incident_rollout({'mode': 'off'})
            second = ensure_response(await client.post('/api/v1/incidents', json=fields), 201)
            accepted_second = ensure_response(await client.post(f"/api/v1/incidents/{second['id']}/diagnoses", json={
                **execution, 'request_key': 'after-rollback'}), 202)
            runs.append(accepted_second['run_id'])
            second_pending = ensure_response(await client.get(accepted_second['result_url']), 200)
            second_spec = deepcopy(second_pending['config']['run_spec'])
            report['admissions']['second'] = {'response': accepted_second, 'run_spec': second_spec}
            replay = ensure_response(await client.post(f"/api/v1/incidents/{first['id']}/diagnoses", json=execution), 202)
            report['checks'].update(
                first_frozen_closed_loop=frozen['engine_id'] == 'autonomous_v2'
                    and frozen.get('execution_variant') == 'closed_loop' and frozen.get('enable_rework') is True,
                exact_configuration=frozen['configuration_sha256'] == plan['configuration_sha256'],
                exact_limits=frozen['limits'] == plan['limits_per_run'],
                second_frozen_legacy=second_spec['engine_id'] == 'legacy' and not second_spec.get('enable_rework'),
                replay_same_run=replay['run_id'] == accepted['run_id'] and replay['replayed'] is True,
                only_two_admitted=len(store.runs) == 2,
                both_pending_before_release=first_pending['status'] == second_pending['status'] == 'running')
            if not all(report['checks'].values()):
                raise ValidationFailure('ADMISSION_VALIDATION_FAILED')
            release.set()
            for name, admission in (('first', accepted), ('second', accepted_second)):
                result = await asyncio.wait_for(service.wait_result(admission['run_id'], PRINCIPAL), timeout=240)
                report['runs'][name] = result
                row = ensure_response(await client.get(admission['result_url']), 200)
                response = await client.get(admission['event_url'])
                if response.status_code != 200:
                    raise ValidationFailure('SSE_HTTP_STATUS')
                events = sse_events(response.text)
                finals = [event for event in events if event.get('type') == 'final']
                cursor = max(0, events[-1]['seq'] - 1) if events else 0
                resumed = await client.get(admission['event_url'], headers={'Last-Event-ID': str(cursor)})
                replayed_events = sse_events(resumed.text) if resumed.status_code == 200 else []
                report['sse'][name] = {'events': len(events), 'final_events': len(finals),
                    'last_event_id': events[-1]['seq'] if events else None,
                    'resume_final_events': sum(event.get('type') == 'final' for event in replayed_events)}
                report['checks'][name + '_single_final'] = len(finals) == 1
                report['checks'][name + '_sse_sequence'] = [event['seq'] for event in events] == list(range(1, len(events) + 1))
                report['checks'][name + '_sse_resume'] = report['sse'][name]['resume_final_events'] == 1
                report['checks'][name + '_terminal_persisted'] = row['status'] in {'completed', 'partial'}
                report['checks'][name + '_no_budget_failure'] = not exhausted(result)
            first_result, second_result = report['runs']['first'], report['runs']['second']
            report['checks']['first_kept_frozen_spec'] = first_result.get('run_spec') == frozen
            report['checks']['first_repair_ineligible'] = first_result.get('repair_eligible') is False
            report['checks']['rollback_second_engine'] = second_result.get('engine_id') == 'legacy'
            for label, other in (('other_owner', Principal(PRINCIPAL.tenant_id, 'other')),
                                 ('other_tenant', Principal('other-tenant', PRINCIPAL.user_id))):
                identity['principal'] = other
                statuses = [(await client.get(path)).status_code
                    for path in (accepted['result_url'], accepted['event_url'])]
                report['checks'][label + '_isolated'] = statuses == [404, 404]
            identity['principal'] = PRINCIPAL
    except Exception as exc:
        report['errors'].append(error_code(exc))
    finally:
        release.set()
        # Persist already accepted terminal results even when an API assertion or paid cap fails.
        service.close()
        for name, run_id in zip(('first', 'second'), runs):
            row = store.get(run_id, PRINCIPAL)
            if row and row.get('result') is not None:
                report['runs'].setdefault(name, row['result'])
        report['paid_budget'] = budget.summary()
    report['checks']['within_shared_paid_cap'] = budget.session_requests <= budget.max_calls
    report['validation_status'] = ('passed' if report['checks'] and all(report['checks'].values())
        and not report['errors'] and budget.session_requests > 0 else 'not_passed')
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--case', default='case_001')
    parser.add_argument('--model')
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'rollout-api-validation')
    args = parser.parse_args(argv)
    if args.live and (args.budget_ledger is None or args.max_model_calls is None):
        parser.error('LIVE_BUDGET_REQUIRED')
    if args.max_model_calls is not None and not 1 <= args.max_model_calls <= 180:
        parser.error('INVALID_TOTAL_MODEL_BUDGET')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    directory = None
    try:
        config = IncidentConfig.from_file()
        if args.model:
            config = replace(config, model=args.model)
        plan = plan_for(args, config)
        if not args.live:
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0, **plan}, ensure_ascii=False, indent=2))
            return 0
        if not config.api_key or config.api_key == 'test-only':
            parser.error('MODEL_CREDENTIALS_REQUIRED')
        budget = GlobalPaidBudget(args.budget_ledger, args.max_model_calls)
        directory = args.output / str(uuid4())
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / 'plan.json', plan)
        previous_logging_level = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            with patch.dict(os.environ, LOCAL_ENV):
                report = asyncio.run(run_validation(config, budget, plan))
        finally:
            logging.disable(previous_logging_level)
        write_json(directory / 'report.json', report)
        write_json(directory / 'human-review.pending.json', {'status': 'pending', 'reviewer': None,
            'approved': None, 'production_rollout': False, 'gate_fixture': GATE_KIND,
            'report_sha256': hashlib.sha256((directory / 'report.json').read_bytes()).hexdigest()})
        print(json.dumps({'directory': str(directory), 'validation_status': report['validation_status'],
            'human_evaluation': 'pending', 'production_rollout': False,
            'paid_requests_sent': budget.session_requests, 'errors': report['errors']}, ensure_ascii=False))
        return 0 if report['validation_status'] == 'passed' else 2
    except Exception as exc:
        result = {'validation_error': error_code(exc), 'production_rollout': False, 'human_evaluation': 'pending'}
        if directory is not None:
            write_json(directory / 'error.json', result)
        print(json.dumps(result), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
