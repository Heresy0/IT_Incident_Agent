"""Actual Vue/HTTP/SSE/PostgreSQL acceptance, with explicitly recorded Flash replays.

No model-quality scoring, model credentials, real providers or repair executors.
The free start path uses the existing scripted control and unchanged application.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
TOKEN = 'frontend-acceptance-local-operator'
DEFAULT_BUNDLE = ROOT / 'output/rca-cross-case-study-20261018/live/39811ae6-9874-45c2-a21f-5dbafe4bc140/bundle.json'
DEFAULT_SCORES = ROOT / 'evals/rca-cross-case-scorecard-20261018.json'
PARTIAL_IDS = ('RCA_f3419b816c1fee595a11cb0b', 'RCA_420b7fbfd6c5456f3a03e3b0', 'RCA_9d4a701949062fbdb3ee4e14')
PRESERVED_FIELDS = ('status', 'business_result', 'review_status', 'diagnosis_readiness', 'run_outcome',
    'output', 'final', 'evidence', 'sources', 'protocol_failures', 'item_reviews', 'run_summary')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8'))


def validate_dsn(dsn):
    from psycopg.conninfo import conninfo_to_dict
    try:
        fields = conninfo_to_dict(dsn)
        valid = (fields.get('dbname', '').endswith('_test')
            and fields.get('host') in {'127.0.0.1', 'localhost', '::1'})
    except Exception:
        valid = False
    if not valid:
        raise ValueError('An explicit loopback *_test database is required; connection details are never printed.')


def load_replays(bundle_path=DEFAULT_BUNDLE, scores_path=DEFAULT_SCORES):
    """Verify immutable report bindings before opening any store."""
    bundle_path, scores_path = Path(bundle_path).resolve(), Path(scores_path).resolve()
    if not bundle_path.is_relative_to(ROOT):
        raise ValueError('Replay observations must remain inside this workspace.')
    bundle, scores = read(bundle_path), read(scores_path)
    if (bundle.get('execution_mode') != 'live_observation_only'
            or scores.get('model') != 'qwen3.7-flash'
            or scores.get('source_bindings', {}).get('bundle', {}).get('sha256') != sha(bundle_path)):
        raise ValueError('Frozen actual Flash observations and a matching scorecard are required.')
    entries = {entry['report_id']: entry for entry in bundle['runs']}
    completed = next(row for row in scores['resource_run_metrics']
        if row['stage'] == 'e2e' and row['delivery_status'] == 'completed')
    selected = (completed['report_id'], *PARTIAL_IDS)
    result = []
    for rid in selected:
        entry = entries[rid]
        if Path(entry['report_file']).name != entry['report_file']:
            raise ValueError('Report paths must be local bundle filenames.')
        path = Path(bundle_path).parent / entry['report_file']
        if sha(path) != entry['report_sha256']:
            raise ValueError('Recorded report SHA256 mismatch.')
        report = read(path)
        native = report['native_report']
        expected = 'completed' if rid == selected[0] else 'partial'
        if (report['report_id'] != rid or report['stage'] != 'e2e' or entry['stage'] != 'e2e'
                or native['status'] != expected or not native.get('final') or not native.get('evidence')):
            raise ValueError('Recorded replay must preserve the expected real status and evidence.')
        if not report.get('model_call_log') or not all(c.get('model') == 'qwen3.7-flash'
                for c in report['model_call_log']):
            raise ValueError('Actual Flash call records are required.')
        result.append({'entry': entry, 'report': report, 'path': path, 'sha256': sha(path)})
    return result


def seed_replays(store, principal, records):
    """Project frozen native content into owner-bound replay incidents.

    Only application-envelope fields are added. The diagnosis/evidence/status
    fields and source report bytes remain unchanged; no worker/model is invoked.
    """
    from incidents.contracts import IncidentCreate
    summaries = []
    for index, record in enumerate(records):
        raw = record['report']
        native = deepcopy(raw['native_report'])
        existing = store.get(native['run_id'], principal)
        if existing is not None:
            old = existing.get('result') or {}
            provenance = old.get('frontend_acceptance_replay', {})
            if (provenance.get('report_sha256') != record['sha256']
                    or provenance.get('report_id') != raw['report_id']
                    or not all(old.get(k) == native.get(k) for k in PRESERVED_FIELDS)
                    or not existing['config'].get('run_spec', {}).get('replay_only')):
                raise ValueError('Existing replay does not match frozen content; refusing overwrite.')
            incident = store.get_incident(existing['config']['incident_id'], principal)
            if not incident or incident['latest_run_id'] != native['run_id']:
                raise ValueError('Existing replay incident binding changed.')
            summaries.append({**provenance, 'incident_id': incident['id'], 'run_id': native['run_id'],
                'title': incident['title'], 'status': native['status'], 'case_id': raw['case_id'], 'repeat': raw['repeat']})
            continue
        ticket = raw['audit_ticket_question']
        title = f"[历史Flash回放] {raw['case_id']} 第{raw['repeat']}次 {native['status']}"
        fields = IncidentCreate.model_validate({
            **{k: ticket['scope'][k] for k in ('service', 'environment', 'service_version', 'start', 'end')},
            'title': title, 'symptoms': ticket['symptoms'], 'occurred_at': ticket['scope']['end'],
            'purpose': 'diagnosis', 'impact': '已保存的真实Flash运行回放；本次查看不调用模型，不代表新诊断。',
            'demo_case_id': None}).model_dump(mode='json')
        incident = store.create_incident(fields, principal)
        execution = {'request_key': f'recorded-{index}', 'execution_mode': 'live',
            'model_budget': 16, 'tool_budget': 8}
        run_id, created, config = store.begin_diagnosis(incident['id'], native['run_id'], execution, principal,
            {'model': 'qwen3.7-flash', 'source_version': 'recorded_replay',
             'run_spec': {'engine_id': 'autonomous_v2', 'replay_only': True}})
        if not created:
            raise ValueError('Replay import must create a distinct new run.')
        for event in native.get('events', []):
            if event.get('type') != 'final':
                store.append(run_id, {**event, 'recorded_replay': True})
        binding = {'report_id': raw['report_id'], 'report_sha256': record['sha256'],
            'source_path': record['path'].relative_to(ROOT).as_posix(),
            'new_provider_calls': 0, 'new_diagnosis': False}
        native.update(execution_mode='recorded_live_replay', model='qwen3.7-flash',
            incident_snapshot=config['incident_snapshot'], frontend_acceptance_replay=binding)
        assert all(native.get(k) == raw['native_report'].get(k) for k in PRESERVED_FIELDS)
        if not store.finish(run_id, native):
            raise ValueError('Replay final commit failed.')
        summaries.append({**binding, 'incident_id': incident['id'], 'run_id': run_id,
            'title': title, 'status': native['status'], 'case_id': raw['case_id'], 'repeat': raw['repeat']})
    return summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--dsn-env', default='FRONTEND_ACCEPTANCE_DSN')
    parser.add_argument('--port', type=int, default=8176)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/frontend-acceptance-20261018/server')
    parser.add_argument('--bundle', type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument('--scorecard', type=Path, default=DEFAULT_SCORES)
    args = parser.parse_args(argv)
    if not args.serve:
        print(json.dumps({'plan_only': True, 'loopback_only': True, 'isolated_test_database_required': True,
            'port': args.port, 'new_provider_calls': 0,
            'startup': 'existing_scripted_control', 'reports': 'four_frozen_actual_flash_replays'}, ensure_ascii=False))
        return 0
    try:
        validate_dsn(os.getenv(args.dsn_env, ''))
    except ValueError as exc:
        parser.error(str(exc))
    if not 1024 <= args.port <= 65535:
        parser.error('A nonprivileged local port is required.')
    static = ROOT / 'front/agent_front/dist'
    if not (static / 'index.html').is_file():
        parser.error('Build the existing Vue frontend first.')
    records = load_replays(args.bundle, args.scorecard)
    args.output.mkdir(parents=True, exist_ok=True)
    from api.auth import Principal, get_current_principal
    from app_main import create_app
    from config import AppSettings
    from config.incident import IncidentConfig
    from fastapi import Depends, HTTPException
    from fastapi.staticfiles import StaticFiles
    from providers.registry import ServiceRegistry
    from storage.runs import PostgresRunStore
    from workflow.dependencies import get_workflow_service
    from workflow.service import WorkflowService
    import uvicorn

    dsn = os.environ[args.dsn_env]
    principal = Principal('frontend_acceptance', 'operator', 'operator')
    auth = args.output / 'auth.local.json'
    write(auth, {'principals': [{**principal.__dict__, 'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest()}]})
    environment = {'INCIDENT_AUTH_FILE': str(auth.resolve()), 'MODEL_API_KEY': 'test-only',
        'DASHSCOPE_API_KEY': 'test-only', 'MODEL': 'scripted-control', 'POSTGRES_DSN': dsn,
        'INCIDENT_SERVICES_FILE': '', 'INCIDENT_MAX_MODEL_CALLS': '16', 'INCIDENT_MAX_TOOL_CALLS': '8',
        'INCIDENT_MAX_SECONDS': '180'}
    session = {'schema_version': 'frontend-acceptance-server-v1',
        'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'actual_frontend_http_sse_postgres_with_recorded_flash_replay',
        'new_provider_calls': 0, 'model_quality_rescored': False,
        'startup_mode': 'scripted_control_only', 'daily_database_used': False,
        'bundle_sha256': sha(args.bundle), 'scorecard_sha256': sha(args.scorecard),
        'frontend_assets_sha256': {p.relative_to(static).as_posix(): sha(p)
            for p in static.rglob('*') if p.is_file()}, 'replays': []}
    with patch.dict(os.environ, environment), patch('workflow.rollout.RolloutPolicy.from_environment', return_value=None):
        store = PostgresRunStore(dsn)
        service = WorkflowService('unused', store=store, config=IncidentConfig(api_key='test-only',
            model='scripted-control', postgres_dsn=dsn), service_registry=ServiceRegistry([]),
            incident_engine='autonomous_v2', max_concurrency=1)
        try:
            service.start()
            session['replays'] = seed_replays(store, principal, records)
            with patch('app_main.get_workflow_service', return_value=service):
                app = create_app(AppSettings(_env_file=None))
                app.dependency_overrides[get_workflow_service] = lambda: service

                @app.get('/_frontend_acceptance/status', include_in_schema=False)
                def status(identity=Depends(get_current_principal)):
                    if identity != principal:
                        raise HTTPException(403, 'Acceptance identity required.')
                    unchanged = all(sha(r['path']) == r['sha256'] for r in records)
                    return {**session, 'frozen_reports_unchanged': unchanged}

                app.mount('/', StaticFiles(directory=str(static), html=True), name='actual_vue_frontend')
                write(args.output / 'session.json', session)
                print(json.dumps({'url': f'http://127.0.0.1:{args.port}', 'synthetic_acceptance_token': TOKEN,
                    'replayed_reports': len(session['replays']), 'new_provider_calls': 0}), flush=True)
                uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning')
        finally:
            service.close()
            session['stopped_at_utc'] = datetime.now(timezone.utc).isoformat()
            write(args.output / 'session.json', session)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
