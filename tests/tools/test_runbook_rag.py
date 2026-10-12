"""Real local index IO, executor integration and mocked paid embedding transport."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import httpx
from api.auth import Principal
from knowledge.configuration import RunbookRAGConfig
from knowledge.retrieval import index_path, cosine
from knowledge.ingest import build_index, prepare
from providers.registry import ServiceBinding, ServiceRegistry
from providers.http import HTTPObservationProvider
from providers.fixtures import ProviderError
from runtime.context import RunContext, Limits, activate, ExecutionError
from tools.executor import ToolExecutor


class RunbookRAGTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'TEST_RAG_KEY':'secret-canary'}))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config = RunbookRAGConfig(index_dir=self.directory, embedding_dimension=2,
                                     embedding_api_key_env='TEST_RAG_KEY')
        self.binding = ServiceBinding('tenant', ('alice',), 'example-api', 'staging', 'v1',
            observations={'runbook_rag':self.config.model_dump()})
        self.principal = Principal('tenant','alice')
        stamp = datetime(2026,10,13,tzinfo=timezone.utc)
        snapshot = {'id':'ticket','title':'timeout','symptoms':'排查超时','service':'example-api',
                    'environment':'staging','service_version':'v1','start':stamp.isoformat(),
                    'end':(stamp+timedelta(minutes=30)).isoformat()}
        self.ticket = ServiceRegistry([self.binding]).ticket(snapshot,self.principal)
        self.docs = [{'document_id':'guide','category':'resource',
                      'text':'排查连接等待 token=hidden-value','updated_at':stamp.isoformat()}]
        self.requests = []
        def handle(request):
            self.requests.append(request)
            texts = json.loads(request.content)['input']['texts']
            return httpx.Response(200,json={'output':{'embeddings':[
                {'text_index':i,'embedding':[0.2,0.8]} for i,_ in enumerate(texts)]},
                'usage':{'total_tokens':8}})
        self.transport = httpx.MockTransport(handle)
        build_index(self.binding,self.docs,transport=self.transport)
        self.requests.clear()
        self.path = index_path(self.binding,self.config)

    def executor(self, limits=None):
        context = RunContext(scope=self.ticket.scope,limits=limits or Limits())
        provider = HTTPObservationProvider(self.binding,self.ticket,self.principal,transport=self.transport)
        return ToolExecutor(provider,self.principal,self.ticket.scope,context,role='knowledge')

    def change(self, edit):
        data = json.loads(self.path.read_text(encoding='utf-8'))
        edit(data)
        self.path.write_text(json.dumps(data),encoding='utf-8')

    def test_executor_registers_runbook_and_counts_embedding_usage(self):
        ex = self.executor()
        result = ex.execute('search_runbooks',{'query':'连接卡住','category':'resource'})
        self.assertEqual(result.status,'ok')
        self.assertEqual(result.evidence[0].kind,'runbook')
        self.assertNotIn('hidden-value',result.model_dump_json())
        self.assertEqual(ex.context.counts['model_calls'],1)
        self.assertEqual(ex.context.counts['tool_calls'],1)
        self.assertEqual(ex.context.summary()['token_usage']['input_tokens'],8)
        self.assertEqual(self.requests[0].headers['Authorization'],'Bearer secret-canary')
        self.assertTrue(any(o.field_path=='text' for o in result.evidence[0].reference_options))

    def test_index_identity_isolated_by_scope_and_embedding(self):
        for binding in (replace(self.binding,tenant_id='other'),replace(self.binding,service='other'),
                        replace(self.binding,environment='production'),replace(self.binding,version='v2')):
            self.assertNotEqual(index_path(binding,self.config),self.path)
        self.assertNotEqual(index_path(self.binding,self.config.model_copy(update={'embedding_model':'other'})),self.path)

    def test_foreign_metadata_is_rejected_before_embedding(self):
        original = self.path.read_bytes()
        for key,value in (('tenant_id','foreign'),('service','other'),('service_version','v2'),
                          ('schema_version','old')):
            with self.subTest(key=key):
                self.path.write_bytes(original)
                self.change(lambda data:data['rows'][0].update({key:value}))
                result = self.executor().execute('search_runbooks',{'query':'连接等待'})
                self.assertEqual(result.error.code,'RAG_SCOPE_MISMATCH')
                self.assertEqual(result.evidence,[])
        self.assertEqual(self.requests,[])

    def test_missing_or_corrupt_index_does_not_spend_embedding(self):
        self.path.unlink()
        result = self.executor().execute('search_runbooks',{'query':'连接等待'})
        self.assertEqual(result.error.code,'RAG_INDEX_NOT_READY')
        self.path.write_text('{',encoding='utf-8')
        result = self.executor().execute('search_runbooks',{'query':'连接等待'})
        self.assertEqual(result.error.code,'RAG_RESULT_INVALID')
        self.assertEqual(self.requests,[])

    def test_budget_and_missing_credentials_prevent_embedding(self):
        ex = self.executor(Limits(model_calls=2,reserve_model_calls=2))
        self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'BUDGET_EXCEEDED')
        with patch.dict(os.environ,{},clear=True):
            ex = self.executor()
            self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'PROVIDER_AUTH_MISSING')
            self.assertEqual(ex.context.counts['model_calls'],0)
        self.assertEqual(self.requests,[])

    def test_low_score_empty_and_duplicate_query_not_repeated(self):
        self.change(lambda data:data['rows'][0].update(vector=[-0.2,-0.8]))
        ex = self.executor()
        self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).status,'empty')
        self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'DUPLICATE_TOOL')
        self.assertEqual(len(self.requests),1)

    def test_category_without_documents_does_not_call_embedding(self):
        self.assertEqual(self.executor().execute('search_runbooks',{'query':'等待','category':'dependency'}).status,'empty')
        self.assertEqual(self.requests,[])

    def test_rebuild_replaces_removed_documents_and_changes_snapshot_version(self):
        first = self.executor().execute('search_runbooks',{'query':'等待'})
        docs = [{**self.docs[0],'document_id':'replacement','text':'核对日志。'}]
        build_index(self.binding,docs,transport=self.transport)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual([r['document_id'] for r in data['rows']],['replacement'])
        second = self.executor().execute('search_runbooks',{'query':'等待'})
        self.assertNotEqual(first.evidence[0].data_version,second.evidence[0].data_version)

    def test_failed_publish_preserves_previous_index_and_removes_temporary_file(self):
        original = self.path.read_bytes()
        with patch('knowledge.ingest.os.replace',side_effect=OSError('private details')):
            with self.assertRaisesRegex(ProviderError,'RAG_UNAVAILABLE'):
                build_index(self.binding,self.docs,transport=self.transport)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(list(Path(self.directory).glob('*.tmp')),[])

    def test_failed_embedding_preserves_previous_index(self):
        original = self.path.read_bytes()
        bad = httpx.MockTransport(lambda _:httpx.Response(200,json={}))
        with self.assertRaisesRegex(ProviderError,'EMBEDDING_FORMAT_INVALID'):
            build_index(self.binding,self.docs,transport=bad)
        self.assertEqual(self.path.read_bytes(),original)

    def test_result_limit_preserves_truncation_and_stable_order(self):
        docs = [{**self.docs[0],'document_id':f'guide-{i}'} for i in range(5)]
        build_index(self.binding,docs,transport=self.transport)
        first = self.executor().execute('search_runbooks',{'query':'等待'})
        second = self.executor().execute('search_runbooks',{'query':'等待'})
        self.assertEqual(len(first.evidence),3)
        self.assertTrue(first.truncated)
        self.assertEqual([e.payload['id'] for e in first.evidence],
                         [e.payload['id'] for e in second.evidence])

    def test_configuration_rejects_unsafe_endpoints_without_values(self):
        from providers.configuration import validate_configuration
        base = {'services':[{'tenant_id':'tenant','users':['alice'],'service':'example-api','environment':'staging',
                          'version':'v1','observations':{'runbook_rag':self.config.model_dump()}}]}
        base['services'][0]['observations']['runbook_rag']['embedding_url']='http://user:secret@remote.example'
        issues = validate_configuration(base)
        self.assertTrue(any(i['level']=='error' for i in issues))
        self.assertNotIn('secret',str(issues))

    def test_invalid_vectors_are_rejected_with_usage_retained(self):
        for vector in ([0.0,0.0],[0.1]):
            self.transport = httpx.MockTransport(lambda _:httpx.Response(200,json={
                'output':{'embeddings':[{'text_index':0,'embedding':vector}]},'usage':{'total_tokens':8}}))
            ex = self.executor()
            result = ex.execute('search_runbooks',{'query':'等待'})
            self.assertEqual(result.error.code,'EMBEDDING_FORMAT_INVALID')
            self.assertEqual(ex.context.summary()['token_usage']['input_tokens'],8)

    def test_indexing_redacts_secrets_before_embedding(self):
        _,rows = prepare(self.binding,self.docs)
        self.assertNotIn('hidden-value',str(rows))

    def test_redaction_before_chunking_keeps_long_manual_and_removes_split_secret(self):
        text = '前文。' * 200 + ' token=' + 'private-value' * 80 + ' 尾部必须保留。' * 300
        _,rows = prepare(self.binding,[{**self.docs[0],'text':text}])
        self.assertGreater(len(rows),3)
        self.assertNotIn('private-value',str(rows))
        self.assertIn('尾部必须保留',rows[-1]['text'])

    def test_tampered_index_vectors_rejected_before_paid_query(self):
        self.change(lambda data:data['rows'][0].update(vector=[0.0,0.0]))
        result = self.executor().execute('search_runbooks',{'query':'等待'})
        self.assertEqual(result.error.code,'RAG_RESULT_INVALID')
        self.assertEqual(self.requests,[])

    def test_free_ingestion_plan_does_not_write_index(self):
        from scripts.ingest_runbooks import main,ROOT
        with patch('scripts.ingest_runbooks.build_index',side_effect=AssertionError('No write')),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--services-file',str(ROOT/'examples/services.rag.example.json'),
                '--tenant','synthetic_demo','--service','example-api','--environment','staging',
                '--documents',str(ROOT/'examples/runbooks.example.json')]),0)
        self.assertTrue(json.loads(out.getvalue())['plan_only'])

    def test_ingestion_budget_failure_keeps_snapshot(self):
        original = self.path.read_bytes()
        context = RunContext(limits=Limits(model_calls=2,reserve_model_calls=2))
        with activate(context),self.assertRaises(ExecutionError):
            build_index(self.binding,self.docs,transport=self.transport)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(context.stop_reason,'BUDGET_EXCEEDED')

    def test_cosine_ranks_similarity_rather_than_vector_magnitude(self):
        self.assertAlmostEqual(cosine([1.,0.],[0.01,0.]),1.)
        self.assertEqual(cosine([1.,0.],[0.,100.]),0.)
        self.assertAlmostEqual(cosine([1.,0.],[-1.,0.]),-1.)


if __name__ == '__main__':
    unittest.main()
