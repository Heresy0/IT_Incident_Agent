"""Free end-to-end local index smoke test; embedding HTTP is mocked."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'),str(ROOT)]
from api.auth import Principal
from knowledge.configuration import RunbookRAGConfig
from knowledge.ingest import build_index
from providers.registry import ServiceBinding,ServiceRegistry
from providers.http import HTTPObservationProvider
from tools.executor import ToolExecutor
from runtime.context import RunContext,activate
from runtime.version import implementation_version


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'output/runbook-rag-local-smoke')
    args = parser.parse_args(argv)
    calls = []
    def response(request):
        calls.append(request)
        texts = json.loads(request.content)['input']['texts']
        return httpx.Response(200,json={'output':{'embeddings':[{'text_index':i,'embedding':[1.0,0.0]}
            for i,_ in enumerate(texts)]},'usage':{'total_tokens':len(texts)*8}})
    transport = httpx.MockTransport(response)
    context = RunContext()
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'RAG_SMOKE_KEY':'mock-only'}):
        config = RunbookRAGConfig(index_dir=directory,embedding_dimension=2,embedding_api_key_env='RAG_SMOKE_KEY')
        binding = ServiceBinding('synthetic-rag-smoke',('cli-reader',),'example-api','staging','smoke-v1',
                                 observations={'runbook_rag':config.model_dump()})
        docs = [{'document_id':'smoke-guide','category':'resource',
                 'text':'连接等待需核对业务日志和连接占用；低处理器使用率不能排除所有下游等待。',
                 'updated_at':'2026-10-13T00:00:00Z'}]
        with activate(context):
            built = build_index(binding,docs,transport=transport)
        principal = Principal(binding.tenant_id,binding.users[0])
        start = datetime.now(timezone.utc)
        snapshot = {'id':'rag-smoke','title':'rag smoke','symptoms':'连接等待','service':binding.service,
            'environment':binding.environment,'service_version':binding.version,'start':start.isoformat(),
            'end':(start+timedelta(minutes=10)).isoformat()}
        ticket = ServiceRegistry([binding]).ticket(snapshot,principal)
        provider = HTTPObservationProvider(binding,ticket,principal,transport=transport)
        executor = ToolExecutor(provider,principal,ticket.scope,context,role='knowledge')
        result = executor.execute('search_runbooks',{'query':'连接卡住','category':'resource'})
        assert result.status == 'ok' and result.evidence[0].kind == 'runbook', result
        assert any(o.field_path == 'text' for o in result.evidence[0].reference_options)
        assert context.counts['model_calls'] == 2 and context.counts['tool_calls'] == 1
        report = {'source_version':implementation_version(),'method':'local_json_with_mock_embeddings_v1',
            'quality_measured':False,'paid_model_calls':0,'index':built,'temporary_index_removed_on_exit':True,
            'evidence':result.model_dump(mode='json'),'run_summary':context.summary(),
            'mock_embedding_requests':len(calls)}
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (args.output/'summary.md').write_text(
            '# 轻量 RAG 链路验证\n\n真实本地文件入库和检索通过；Embedding 使用测试桩，付费调用为零。'
            '本次不评估真实语义检索或诊断质量。\n\n'
            '| 检查 | 结果 |\n| --- | --- |\n| 文档切分与本地索引写入 | 通过 |\n'
            '| 向量相似度检索 | 通过（固定测试向量） |\n| 手册证据与字段引用 | 通过 |\n'
            '| 向量化预算与词元记录 | 通过 |\n| 临时索引清理 | 完成 |\n',encoding='utf-8')
    print(args.output/'summary.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
