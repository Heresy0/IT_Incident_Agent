"""Free pgvector smoke test; explicit execution requires an isolated *_test DB."""
import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from uuid import uuid4
import httpx
from psycopg.conninfo import conninfo_to_dict

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'app'),str(ROOT)]
from api.auth import Principal
from knowledge.configuration import RunbookRAGConfig
from knowledge.database import RunbookStore, scope_id
from knowledge.ingest import build_index
from providers.registry import ServiceBinding,ServiceRegistry
from providers.http import HTTPObservationProvider
from providers.fixtures import ProviderError
from tools.executor import ToolExecutor
from runtime.context import RunContext,activate
from runtime.version import implementation_version


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--dsn-env',default='INCIDENT_TEST_RUNBOOK_DSN')
    parser.add_argument('--output',type=Path,default=ROOT/'output/runbook-rag-pgvector-smoke')
    args=parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({'plan_only':True,'backend':'pgvector','database_env':args.dsn_env,
            'requires_isolated_test_database':True,'paid_model_calls':0},ensure_ascii=False))
        return 0
    try:
        if not conninfo_to_dict(os.getenv(args.dsn_env,'')).get('dbname','').endswith('_test'):
            raise ValueError()
        config=RunbookRAGConfig(database_dsn_env=args.dsn_env,embedding_dimension=2,embedding_api_key_env='RAG_SMOKE_KEY')
    except Exception:
        print(json.dumps({'status':'failed','code':'ISOLATED_TEST_DATABASE_REQUIRED'}))
        return 2
    calls=[]
    def response(request):
        calls.append(request)
        texts=json.loads(request.content)['input']['texts']
        return httpx.Response(200,json={'output':{'embeddings':[{'text_index':i,'embedding':[1.,0.]}
            for i,_ in enumerate(texts)]},'usage':{'total_tokens':len(texts)*8}})
    transport=httpx.MockTransport(response)
    context=RunContext()
    binding=ServiceBinding('rag_smoke_'+uuid4().hex,('reader',),'example-api','staging','smoke-v1',
        observations={'runbook_rag':config.model_dump()})
    store=RunbookStore(binding,config)
    report=None
    cleanup_ok=True
    try:
        with patch.dict(os.environ,{'RAG_SMOKE_KEY':'mock-only'}):
            docs=[{'document_id':'smoke-guide','category':'resource',
                'text':'连接等待需核对业务日志和连接占用；低处理器使用率不能排除所有下游等待。',
                'updated_at':'2026-10-13T00:00:00Z'}]
            with activate(context):
                built=build_index(binding,docs,transport=transport)
            with store.connection() as connection:
                versions=connection.execute("SELECT current_setting('server_version') AS postgres, "
                    "(SELECT extversion FROM pg_extension WHERE extname='vector') AS pgvector").fetchone()
            principal=Principal(binding.tenant_id,'reader')
            start=datetime.now(timezone.utc)
            ticket=ServiceRegistry([binding]).ticket({'id':'rag-smoke','title':'rag smoke','symptoms':'连接等待',
                'service':binding.service,'environment':binding.environment,'service_version':binding.version,
                'start':start.isoformat(),'end':(start+timedelta(minutes=10)).isoformat()},principal)
            provider=HTTPObservationProvider(binding,ticket,principal,transport=transport)
            executor=ToolExecutor(provider,principal,ticket.scope,context,role='knowledge')
            result=executor.execute('search_runbooks',{'query':'连接卡住','category':'resource'})
            assert result.status=='ok' and result.evidence[0].kind=='runbook'
            assert any(o.field_path=='text' for o in result.evidence[0].reference_options)
            assert context.counts['model_calls']==2 and context.counts['tool_calls']==1
            report={'source_version':implementation_version(),'method':'real_pgvector_with_mock_embeddings_v1',
                'quality_measured':False,'paid_model_calls':0,'database_versions':versions,'import':built,
                'evidence':result.model_dump(mode='json'),'run_summary':context.summary(),
                'mock_embedding_requests':len(calls)}
    except Exception as exc:
        print(json.dumps({'status':'failed','code':exc.code if isinstance(exc,ProviderError) else 'RAG_SMOKE_FAILED'}))
    finally:
        try:
            with store.connection() as connection:
                connection.execute('DELETE FROM incident_runbook_documents WHERE scope_id=%s',(scope_id(binding,config),))
                remaining=connection.execute('SELECT count(*) FROM incident_runbook_chunks WHERE scope_id=%s',
                    (scope_id(binding,config),)).fetchone()['count']
                cleanup_ok=remaining==0
        except Exception:
            cleanup_ok=False
    if report is None or not cleanup_ok:
        if not cleanup_ok:
            print(json.dumps({'status':'failed','code':'RAG_SMOKE_CLEANUP_FAILED'}))
        return 2
    report['temporary_scope_removed']=True
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.output/'summary.md').write_text(
        '# pgvector 链路验证\n\n真实 PostgreSQL / pgvector 写入和检索通过，Embedding 使用固定测试向量，付费调用为零。'
        '不评估真实语义检索或诊断质量。\n\n'
        '| 检查 | 结果 |\n| --- | --- |\n| 手册与片段事务写入 | 通过 |\n'
        '| SQL 范围过滤与余弦检索 | 通过 |\n| 工具执行、手册证据和字段引用 | 通过 |\n'
        '| 向量化预算与词元记录 | 通过 |\n| 本次临时范围清理 | 完成 |\n'
        '| 数据库版本 | PostgreSQL '+versions['postgres']+' / pgvector '+versions['pgvector']+' |\n',encoding='utf-8')
    print(args.output/'summary.md')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
