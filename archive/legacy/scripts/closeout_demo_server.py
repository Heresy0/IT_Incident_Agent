"""Opt-in local UI acceptance with an isolated *_test DB and simulated providers."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
TOKEN = 'closeout-demo-operator'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', help='启动仅本机可访问的免费验收服务。')
    parser.add_argument('--dsn-env', default='INCIDENT_TEST_POSTGRES_DSN')
    parser.add_argument('--port', type=int, default=8175)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/closeout-ui')
    args = parser.parse_args(argv)
    if not args.serve:
        print(json.dumps({'plan_only': True, 'requires_isolated_test_database': True,
            'paid_model_calls': 0, 'live_repairs': 0, 'port': args.port}, ensure_ascii=False))
        return 0
    from psycopg.conninfo import conninfo_to_dict
    dsn = os.getenv(args.dsn_env, '')
    try:
        isolated = conninfo_to_dict(dsn).get('dbname', '').endswith('_test')
    except Exception:
        isolated = False
    if not isolated or not 1024 <= args.port <= 65535:
        parser.error('仅允许显式 *_test 数据库和 1024～65535 端口；连接信息不输出。')
    static = ROOT / 'front/agent_front/dist'
    if not (static / 'index.html').is_file():
        parser.error('请先构建前端：在 front/agent_front 运行 npm run build。')
    args.output.mkdir(parents=True, exist_ok=True)
    from api.auth import Principal, get_current_principal
    from app_main import create_app
    from config import AppSettings
    from config.incident import IncidentConfig
    from fastapi import Depends
    from fastapi.staticfiles import StaticFiles
    import httpx
    from knowledge.configuration import RunbookRAGConfig
    from knowledge.database import RunbookStore, scope_id
    from knowledge.retrieval import embed
    from providers.bound import catalog
    from providers.registry import ServiceBinding, ServiceRegistry
    from runtime.version import implementation_version
    from scripts.demo_remediation import DemonstrationExecutor
    from storage.runs import PostgresRunStore
    from workflow.dependencies import get_workflow_service
    from workflow.service import WorkflowService
    import uvicorn

    principal = Principal('closeout_' + uuid4().hex, 'operator', 'operator')
    fields = catalog(principal)[0]['fields']
    config = RunbookRAGConfig(database_dsn_env=args.dsn_env, embedding_dimension=2,
        embedding_api_key_env='CLOSEOUT_EMBEDDING_KEY')
    executor = DemonstrationExecutor()
    binding = ServiceBinding(principal.tenant_id, (principal.user_id,), fields['service'],
        fields['environment'], fields['service_version'], observations={'runbook_rag': config.model_dump()},
        repairs={'api': {'executor': executor.name, 'actions': ['restart_service']}})
    auth = args.output / 'auth.local.json'
    auth.write_text(json.dumps({'principals': [{**principal.__dict__,
        'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest()}]}), encoding='utf-8')
    embedding_requests = []

    def embeddings(request):
        body = json.loads(request.content)
        embedding_requests.append(len(body['input']['texts']))
        return httpx.Response(200, json={'output': {'embeddings': [
            {'text_index': i, 'embedding': [1., 0.]} for i in range(len(body['input']['texts']))]},
            'usage': {'total_tokens': len(body['input']['texts']) * 8}})

    transport = httpx.MockTransport(embeddings)
    report = {'source_version': implementation_version(), 'mode': 'isolated_ui_control_only',
              'paid_model_calls': 0, 'live_repairs': 0}
    store = PostgresRunStore(dsn)
    service = WorkflowService('unused', store=store, config=IncidentConfig(api_key='test-only', postgres_dsn=dsn),
        service_registry=ServiceRegistry([binding]), repair_executors={executor.name: executor})
    app = create_app(AppSettings(_env_file=None))
    app.dependency_overrides[get_workflow_service] = lambda: service

    @app.get('/_closeout/status', include_in_schema=False)
    def status(identity=Depends(get_current_principal)):
        if identity != principal:
            from fastapi import HTTPException
            raise HTTPException(403, '仅供本次合成验收身份使用。')
        return {**report, 'simulated_writes': executor.writes,
                'embedding_http_requests': len(embedding_requests), 'tenant_id': principal.tenant_id}

    app.mount('/', StaticFiles(directory=str(static), html=True), name='closeout_frontend')
    try:
        # The workflow receives only explicit test settings. Live mode rejects
        # the dummy key; all embedding HTTP traffic uses the local transport.
        with patch.dict(os.environ, {'INCIDENT_AUTH_FILE': str(auth.resolve()), 'CLOSEOUT_EMBEDDING_KEY': 'mock-only',
                'POSTGRES_DSN': dsn, 'MODEL_API_KEY': 'test-only', 'DASHSCOPE_API_KEY': 'test-only',
                'INCIDENT_SERVICES_FILE': ''}), \
                patch('app_main.get_workflow_service', return_value=service), \
                patch('knowledge.ingest.embed', side_effect=lambda texts, config, **kw: embed(texts, config, transport=transport)), \
                patch('knowledge.retrieval.embed', side_effect=lambda texts, config, **kw: embed(texts, config, transport=transport)):
            print(f'免费隔离验收：http://127.0.0.1:{args.port}；合成令牌：{TOKEN}', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
    finally:
        # Clean only this process's random scope/tenant, never clear tables.
        report.update(simulated_writes=executor.writes, embedding_http_requests=len(embedding_requests),
                      finished_at=datetime.now(timezone.utc).isoformat())
        try:
            import psycopg
            with psycopg.connect(dsn) as connection:
                if connection.execute("SELECT to_regclass('public.incident_runbook_documents')").fetchone()[0]:
                    connection.execute('DELETE FROM incident_runbook_documents WHERE scope_id=%s', (scope_id(binding, config),))
                for table in ('confirmed_incident_cases', 'incidents', 'research_runs'):
                    connection.execute(f'DELETE FROM {table} WHERE tenant_id=%s', (principal.tenant_id,))
            report['cleanup'] = 'completed'
        except Exception as exc:
            report['cleanup'] = 'failed'
            report['cleanup_exception_type'] = type(exc).__name__
        finally:
            service.close()
            (args.output / 'server-summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['cleanup'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
