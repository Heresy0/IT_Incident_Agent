"""Isolated real-Flash Vue acceptance using the existing v2 closed loop.

The explicit operator admission is scoped to the local demo tenant and service.
It does not assert production semantic acceptance or change application defaults.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Lock
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from scripts.demo_frontend_acceptance import TOKEN, validate_dsn
from workflow.service import WorkflowService

MODEL = 'qwen3.7-flash'
TENANT = 'frontend_acceptance'
USER = 'operator'
SERVICE = 'checkout-api'
MODEL_CAP = 32


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'))
    temporary.replace(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode()).hexdigest()


def validate_execution(payload):
    if (not isinstance(payload, dict) or payload.get('execution_mode') != 'live'
            or payload.get('model_budget') != 16 or payload.get('tool_budget') != 8):
        raise ValueError('本闭环演示固定使用真实模型、16次模型调用和8次工具调用；请按此设置，其他配置不会回退运行旧版。')


class ClosedLoopDemoService(WorkflowService):
    """Reject other envelopes after normal API authentication and body parsing."""
    def start_diagnosis(self, iid, execution, principal):
        from incidents.support import IncidentError
        try:
            validate_execution(execution)
        except ValueError as exc:
            raise IncidentError(str(exc), status=400) from None
        return super().start_diagnosis(iid, execution, principal)


class LiveLedger:
    """One persistent admission cap, including failed or unknown-usage calls."""
    def __init__(self, folder, maximum=MODEL_CAP):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'model-ledger.json'
        self.lock = Lock()
        self.maximum = maximum
        if self.path.exists():
            saved = read(self.path)
            if saved.get('maximum_calls') != maximum or saved.get('model') != MODEL:
                raise ValueError('Existing paid admission envelope changed; refusing reset.')
        else:
            write(self.path, {'schema_version': 'frontend-live-model-ledger-v1',
                'model': MODEL, 'maximum_calls': maximum, 'calls': []})

    def admit(self, run_id, role, messages):
        from runtime.context import ExecutionError
        with self.lock:
            saved = read(self.path)
            if len(saved['calls']) >= self.maximum:
                raise ExecutionError('DEMO_MODEL_CALL_LIMIT', 'model')
            row = {'request_id': len(saved['calls']) + 1, 'run_id': run_id,
                   'role': role, 'requested_model': MODEL, 'status': 'reserved',
                   'started_at_utc': datetime.now(timezone.utc).isoformat(),
                   'request_sha256': digest([{'type': getattr(m, 'type', 'unknown'),
                                             'content': m.content} for m in messages]),
                   'usage': None}
            saved['calls'].append(row)
            write(self.path, saved)
            return row

    def settle(self, row):
        with self.lock:
            saved = read(self.path)
            saved['calls'][row['request_id'] - 1] = row
            write(self.folder / 'calls' / f"{row['request_id']:04d}.json", row)
            write(self.path, saved)

    def summary(self):
        with self.lock:
            saved = read(self.path)
        calls = saved['calls']
        measured = [row for row in calls if row.get('usage') is not None]
        return {'admitted_calls': len(calls), 'maximum_calls': self.maximum,
                'remaining_calls': self.maximum - len(calls),
                'known_usage_calls': len(measured), 'unknown_usage_calls': len(calls) - len(measured),
                'input_tokens_known': sum(row['usage']['input_tokens'] for row in measured),
                'output_tokens_known': sum(row['usage']['output_tokens'] for row in measured),
                'model': MODEL, 'is_vendor_bill': False}


class RecordedFlash:
    def __init__(self, model, ledger, role):
        self.model, self.ledger, self.role = model, ledger, role

    def invoke(self, messages):
        from runtime.context import current, ExecutionError
        context = current()
        if context is None:
            raise ExecutionError('DEMO_RUN_CONTEXT_REQUIRED', 'model')
        row = self.ledger.admit(context.run_id, self.role, messages)
        started = time.monotonic()
        try:
            response = self.model.invoke(messages)
            usage = getattr(response, 'usage_metadata', None)
            if usage and 'input_tokens' in usage and 'output_tokens' in usage:
                row['usage'] = {key: int(usage[key]) for key in ('input_tokens', 'output_tokens')}
            metadata = getattr(response, 'response_metadata', {}) or {}
            row.update(status='responded', response_model=metadata.get('model_name') or metadata.get('model'),
                       finish_reason=metadata.get('finish_reason'), response_content=response.content)
            return response
        except Exception as exc:
            row.update(status='failed', error_code=getattr(exc, 'code', type(exc).__name__))
            raise
        finally:
            row['duration_ms'] = round((time.monotonic() - started) * 1000)
            self.ledger.settle(row)


def configure_demo(service, artifact):
    """Bind the operator-requested demo admission to exact source and settings."""
    from runtime.version import implementation_version
    from workflow.rollout import RolloutPolicy, AcceptanceGate, configuration_fingerprint
    from workflow.v2.review import REVIEW_POLICY_VERSION
    config = service._base_config
    limits = service._incident.limits({'execution': {'model_budget': 16, 'tool_budget': 8}}).__dict__
    settings = {'model': config.model, 'base_url': config.base_url, 'extra_body': config.extra_body,
                'temperature': 0, 'max_tokens': 2200}
    model_hash = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    configuration_hash = configuration_fingerprint(model_hash, 'closed_loop', True, limits,
                                                  execution_mode='live')
    plan = {'schema_version': 'frontend-live-demo-admission-v1',
            'authority': 'explicit_user_requested_isolated_demo_execution',
            'meaning': 'Execution admission for this local demo only; not production quality acceptance.',
            'source_version': implementation_version(), 'review_policy_version': REVIEW_POLICY_VERSION,
            'configuration_sha256': configuration_hash, 'model_settings_sha256': model_hash,
            'model': MODEL, 'provider_host': urlsplit(config.base_url).hostname,
            'execution_mode': 'live', 'execution_variant': 'closed_loop',
            'enable_rework': True, 'enable_challenge': True, 'limits': limits,
            'tenant': TENANT, 'service': SERVICE, 'maximum_live_model_calls': MODEL_CAP,
            'planned_ui_cases': ['case_001', 'case_003'], 'fixture_observations_are_synthetic': True,
            'human_semantic_gold': False, 'production_quality_gate_changed': False,
            'repair_eligible': False}
    write(artifact, plan)
    gate = AcceptanceGate(source_version=plan['source_version'], review_policy_version=REVIEW_POLICY_VERSION,
        evaluation_hash=hashlib.sha256(Path(artifact).read_bytes()).hexdigest(), verdict='pass',
        configuration_sha256=configuration_hash)
    service.configure_incident_rollout(RolloutPolicy(mode='full', tenants=(TENANT,), services=(SERVICE,),
        variant='closed_loop', enable_challenge=True, shadow=False, gate=gate))
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8176)
    parser.add_argument('--dsn-env', default='FRONTEND_ACCEPTANCE_DSN')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/frontend-live-acceptance-20261018/server')
    args = parser.parse_args(argv)
    if not args.serve:
        print(json.dumps({'plan_only': True, 'model': MODEL, 'loopback_only': True,
            'execution_variant': 'closed_loop', 'maximum_live_model_calls': MODEL_CAP,
            'synthetic_observations': True, 'real_provider_calls': 0}))
        return 0
    validate_dsn(os.getenv(args.dsn_env, ''))
    if not 1024 <= args.port <= 65535:
        parser.error('A nonprivileged loopback port is required.')
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT / 'output'):
        parser.error('Runtime artifacts must remain in workspace output/.')
    static = ROOT / 'front/agent_front/dist'
    if not (static / 'index.html').is_file():
        parser.error('Build the existing Vue frontend first.')

    from config.incident import IncidentConfig
    from api.auth import Principal, get_current_principal
    from agents.models import build_model
    from app_main import create_app
    from config import AppSettings
    from fastapi import Depends, HTTPException
    from fastapi.staticfiles import StaticFiles
    from providers.registry import ServiceRegistry
    from storage.runs import PostgresRunStore
    from workflow.dependencies import get_workflow_service
    from workflow.rollout import RolloutPolicy
    import uvicorn

    base_config = IncidentConfig.from_file()
    if not base_config.api_key or base_config.api_key == 'test-only':
        parser.error('An existing real model credential is required; credential values are never printed.')
    if urlsplit(base_config.base_url).hostname != 'dashscope.aliyuncs.com':
        parser.error('This Flash acceptance is limited to the configured official DashScope endpoint.')
    dsn = os.environ[args.dsn_env]
    config = replace(base_config, model=MODEL, postgres_dsn=dsn)
    ledger = LiveLedger(args.output)
    principal = Principal(TENANT, USER, 'operator')
    auth = args.output / 'auth.local.json'
    write(auth, {'principals': [{**principal.__dict__, 'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest()}]})

    def factory(api_key, model):
        if model != MODEL:
            raise ValueError('The demo model is frozen to Flash.')
        chat = build_model(api_key, MODEL, base_url=config.base_url, extra_body=config.extra_body)
        return {role: RecordedFlash(chat, ledger, role) for role in
                ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer', 'challenger')}

    environment = {'INCIDENT_AUTH_FILE': str(auth), 'POSTGRES_DSN': dsn,
                   'INCIDENT_MAX_MODEL_CALLS': '16', 'INCIDENT_MAX_TOOL_CALLS': '8',
                   'INCIDENT_MAX_SECONDS': '180'}
    with patch.dict(os.environ, environment):
        store = PostgresRunStore(dsn)
        service = ClosedLoopDemoService('unused', store=store, config=config,
            incident_model_factory=factory, service_registry=ServiceRegistry([]),
            incident_engine='autonomous_v2', incident_rollout=RolloutPolicy(mode='off'), max_concurrency=1)
        plan = configure_demo(service, args.output / 'admission.json')
        try:
            service.start()
            baseline = service.list_incidents(principal, 0, 100)
            session = {'schema_version': 'frontend-live-server-v1',
                'started_at_utc': datetime.now(timezone.utc).isoformat(),
                'base_url': f'http://127.0.0.1:{args.port}', 'model': MODEL,
                'admission': plan, 'baseline_incidents': len(baseline),
                'baseline_run_ids': [row['latest_run_id'] for row in baseline if row.get('latest_run_id')],
                'frontend_assets_sha256': {p.relative_to(static).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in static.rglob('*') if p.is_file()},
                'daily_database_used': False, 'live_repair_executors': False}
            with patch('app_main.get_workflow_service', return_value=service):
                app = create_app(AppSettings(_env_file=None))
                app.dependency_overrides[get_workflow_service] = lambda: service

                @app.get('/_frontend_live_acceptance/status', include_in_schema=False)
                def status(identity=Depends(get_current_principal)):
                    if identity != principal:
                        raise HTTPException(403, 'Acceptance identity required.')
                    return {**session, 'model_ledger': ledger.summary()}

                app.mount('/', StaticFiles(directory=str(static), html=True), name='actual_vue_frontend')
                write(args.output / 'session.json', session)
                print(json.dumps({'url': session['base_url'], 'model': MODEL,
                    'variant': 'closed_loop', 'max_model_calls_total': MODEL_CAP,
                    'preserved_incidents': len(baseline), 'credential_values_logged': False}), flush=True)
                uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning')
        finally:
            service.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
