"""Executor/budget tests with a fake store; real SQL is covered in storage tests."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
from api.auth import Principal
from knowledge.configuration import RunbookRAGConfig
from knowledge.database import scope_id, scope_fields, validate_catalog, RunbookStore
from knowledge.ingest import build_index, prepare
from providers.registry import ServiceBinding, ServiceRegistry
from providers.http import HTTPObservationProvider
from providers.fixtures import ProviderError
from runtime.context import RunContext, Limits, activate, ExecutionError
from tools.executor import ToolExecutor


class FakeStore:
    """Return canned similarity scores; never substitutes for pgvector acceptance."""
    def __init__(self,binding,config,data):
        self.binding,self.config,self.data=binding,config,data
        self.identity=scope_id(binding,config)

    def initialize(self):
        pass

    def catalog(self):
        rows=deepcopy(self.data.get(self.identity,{}).get('catalog',[]))
        validate_catalog(rows,self.binding,self.config)
        return rows

    @contextmanager
    def snapshot(self):
        yield self

    def signature(self):
        return tuple(sorted((r['document_id'],r['revision']) for r in self.catalog()))

    def replace(self,rows,vectors,expected):
        if self.signature()!=expected:
            raise ProviderError('RAG_INDEX_CHANGED')
        generation=self.data.get('generation',0)+1
        self.data['generation']=generation
        revision=hashlib.sha256(str(generation).encode()).hexdigest()
        catalog=[]
        for document in sorted(set(r['document_id'] for r in rows)):
            group=[r for r in rows if r['document_id']==document]
            catalog.append({**scope_fields(self.binding),'scope_id':self.identity,
                'document_id':document,'revision':revision,'updated_at':datetime.fromisoformat(group[0]['updated_at']),
                'category':group[0]['category'],'embedding_model':self.config.embedding_model,
                'embedding_dimension':self.config.embedding_dimension,'chunk_count':len(group),'actual_chunks':len(group)})
        results=[]
        for row in rows:
            doc=next(d for d in catalog if d['document_id']==row['document_id'])
            results.append({**row,**doc,'retrieval_score':0.9})
        self.data[self.identity]={'catalog':catalog,'results':results}

    def search(self,vector,category):
        rows=self.data[self.identity]['results']
        return deepcopy([r for r in rows if not category or r['category']==category][:self.config.top_k+1])


class RunbookRAGTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'TEST_RAG_KEY':'secret-canary'}))
        self.config=RunbookRAGConfig(embedding_dimension=2,embedding_api_key_env='TEST_RAG_KEY')
        self.binding=ServiceBinding('tenant',('alice',),'example-api','staging','v1',
            observations={'runbook_rag':self.config.model_dump()})
        self.principal=Principal('tenant','alice')
        stamp=datetime(2026,10,13,tzinfo=timezone.utc)
        snapshot={'id':'ticket','title':'timeout','symptoms':'排查超时','service':'example-api',
                  'environment':'staging','service_version':'v1','start':stamp.isoformat(),
                  'end':(stamp+timedelta(minutes=30)).isoformat()}
        self.ticket=ServiceRegistry([self.binding]).ticket(snapshot,self.principal)
        self.docs=[{'document_id':'guide','category':'resource','text':'排查连接等待 token=hidden-value',
                   'updated_at':stamp.isoformat()}]
        self.requests=[]
        def handle(request):
            self.requests.append(request)
            texts=json.loads(request.content)['input']['texts']
            return httpx.Response(200,json={'output':{'embeddings':[
                {'text_index':i,'embedding':[0.2,0.8]} for i,_ in enumerate(texts)]},'usage':{'total_tokens':8}})
        self.transport=httpx.MockTransport(handle)
        self.data={}
        self.store=FakeStore(self.binding,self.config,self.data)
        factory=lambda binding,config:FakeStore(binding,config,self.data)
        self.enterContext(patch('knowledge.ingest.RunbookStore',side_effect=factory))
        self.enterContext(patch('knowledge.retrieval.RunbookStore',side_effect=factory))
        build_index(self.binding,self.docs,transport=self.transport)
        self.requests.clear()
        self.identity=scope_id(self.binding,self.config)

    def executor(self,limits=None):
        context=RunContext(scope=self.ticket.scope,limits=limits or Limits())
        provider=HTTPObservationProvider(self.binding,self.ticket,self.principal,transport=self.transport)
        return ToolExecutor(provider,self.principal,self.ticket.scope,context,role='knowledge')

    def test_executor_registers_runbook_and_counts_embedding_usage(self):
        ex=self.executor()
        result=ex.execute('search_runbooks',{'query':'连接卡住','category':'resource'})
        self.assertEqual(result.status,'ok')
        self.assertEqual(result.evidence[0].kind,'runbook')
        self.assertNotIn('hidden-value',result.model_dump_json())
        self.assertEqual(ex.context.counts['model_calls'],1)
        self.assertEqual(ex.context.counts['tool_calls'],1)
        self.assertEqual(ex.context.summary()['token_usage']['input_tokens'],8)
        self.assertEqual(self.requests[0].headers['Authorization'],'Bearer secret-canary')
        self.assertTrue(any(o.field_path=='text' for o in result.evidence[0].reference_options))

    def test_identity_isolated_by_scope_and_embedding(self):
        for binding in (replace(self.binding,tenant_id='other'),replace(self.binding,service='other'),
                        replace(self.binding,environment='production'),replace(self.binding,version='v2')):
            self.assertNotEqual(scope_id(binding,self.config),self.identity)
        self.assertNotEqual(scope_id(self.binding,self.config.model_copy(update={'embedding_model':'other'})),self.identity)

    def test_foreign_metadata_rejected_before_embedding(self):
        original=deepcopy(self.data[self.identity])
        for key,value in (('tenant_id','foreign'),('service','other'),('service_version','v2'),('schema_version','old')):
            self.data[self.identity]=deepcopy(original)
            self.data[self.identity]['catalog'][0][key]=value
            self.assertEqual(self.executor().execute('search_runbooks',{'query':'等待'}).error.code,'RAG_SCOPE_MISMATCH')
        self.assertEqual(self.requests,[])

    def test_foreign_search_response_rejected(self):
        self.data[self.identity]['results'][0]['tenant_id']='other'
        self.assertEqual(self.executor().execute('search_runbooks',{'query':'等待'}).error.code,'RAG_SCOPE_MISMATCH')

    def test_empty_or_incomplete_catalog_does_not_spend_embedding(self):
        self.data[self.identity]['catalog'][0]['actual_chunks']=0
        self.assertEqual(self.executor().execute('search_runbooks',{'query':'等待'}).error.code,'RAG_RESULT_INVALID')
        self.data[self.identity]['catalog']=[]
        self.assertEqual(self.executor().execute('search_runbooks',{'query':'等待'}).error.code,'RAG_INDEX_NOT_READY')
        self.assertEqual(self.requests,[])

    def test_budget_and_missing_credentials_prevent_embedding(self):
        ex=self.executor(Limits(model_calls=2,reserve_model_calls=2))
        self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'BUDGET_EXCEEDED')
        with patch.dict(os.environ,{},clear=True):
            ex=self.executor()
            self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'PROVIDER_AUTH_MISSING')
            self.assertEqual(ex.context.counts['model_calls'],0)
        self.assertEqual(self.requests,[])

    def test_low_score_empty_and_duplicate_query_not_repeated(self):
        self.data[self.identity]['results'][0]['retrieval_score']=0.1
        ex=self.executor()
        self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).status,'empty')
        self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'DUPLICATE_TOOL')
        self.assertEqual(len(self.requests),1)

    def test_category_without_documents_does_not_call_embedding(self):
        self.assertEqual(self.executor().execute('search_runbooks',{'query':'等待','category':'dependency'}).status,'empty')
        self.assertEqual(self.requests,[])

    def test_replace_removes_documents_and_changes_evidence_revision(self):
        first=self.executor().execute('search_runbooks',{'query':'等待'})
        build_index(self.binding,[{**self.docs[0],'document_id':'new-guide'}],transport=self.transport)
        self.assertEqual([r['document_id'] for r in self.store.catalog()],['new-guide'])
        second=self.executor().execute('search_runbooks',{'query':'等待'})
        self.assertNotEqual(first.evidence[0].data_version,second.evidence[0].data_version)

    def test_failed_embedding_keeps_previous_documents(self):
        signature=self.store.signature()
        bad=httpx.MockTransport(lambda _:httpx.Response(200,json={}))
        with self.assertRaisesRegex(ProviderError,'EMBEDDING_FORMAT_INVALID'):
            build_index(self.binding,self.docs,transport=bad)
        self.assertEqual(self.store.signature(),signature)

    def test_result_limit_preserves_truncation_and_stable_order(self):
        build_index(self.binding,[{**self.docs[0],'document_id':f'guide-{i}'} for i in range(5)],transport=self.transport)
        first=self.executor().execute('search_runbooks',{'query':'等待'})
        second=self.executor().execute('search_runbooks',{'query':'等待'})
        self.assertEqual(len(first.evidence),3)
        self.assertTrue(first.truncated)
        self.assertEqual([e.payload['id'] for e in first.evidence],[e.payload['id'] for e in second.evidence])

    def test_configuration_rejects_unsafe_endpoint_and_old_file_backend(self):
        from providers.configuration import validate_configuration
        base={'services':[{'tenant_id':'tenant','users':['alice'],'service':'example-api','environment':'staging',
                          'version':'v1','observations':{'runbook_rag':self.config.model_dump()}}]}
        base['services'][0]['observations']['runbook_rag']['embedding_url']='http://user:secret@remote.example'
        issues=validate_configuration(base)
        self.assertTrue(any(i['level']=='error' for i in issues))
        self.assertNotIn('secret',str(issues))
        with self.assertRaises(ValueError):
            RunbookRAGConfig(index_dir='legacy')

    def test_invalid_vectors_rejected_with_usage_retained(self):
        for vector in ([0.,0.],[0.1]):
            self.transport=httpx.MockTransport(lambda _:httpx.Response(200,json={
                'output':{'embeddings':[{'text_index':0,'embedding':vector}]},'usage':{'total_tokens':8}}))
            ex=self.executor()
            self.assertEqual(ex.execute('search_runbooks',{'query':'等待'}).error.code,'EMBEDDING_FORMAT_INVALID')
            self.assertEqual(ex.context.summary()['token_usage']['input_tokens'],8)

    def test_redaction_before_chunking_keeps_long_manual_and_removes_secret(self):
        text='前文。'*200+' token='+'private-value'*80+' 尾部必须保留。'*300
        _,rows=prepare(self.binding,[{**self.docs[0],'text':text}])
        self.assertGreater(len(rows),3)
        self.assertNotIn('private-value',str(rows))
        self.assertIn('尾部必须保留',rows[-1]['text'])

    def test_free_ingestion_plan_does_not_open_database(self):
        from scripts.ingest_runbooks import main,ROOT
        with patch('scripts.ingest_runbooks.build_index',side_effect=AssertionError('No write')),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--services-file',str(ROOT/'examples/services.rag.example.json'),
                '--tenant','synthetic_demo','--service','example-api','--environment','staging',
                '--documents',str(ROOT/'examples/runbooks.example.json')]),0)
        self.assertTrue(json.loads(out.getvalue())['plan_only'])

    def test_ingestion_budget_failure_keeps_previous_documents(self):
        signature=self.store.signature()
        context=RunContext(limits=Limits(model_calls=2,reserve_model_calls=2))
        with activate(context),self.assertRaises(ExecutionError):
            build_index(self.binding,self.docs,transport=self.transport)
        self.assertEqual(self.store.signature(),signature)

    def test_missing_database_config_never_exposes_dsn_or_connects(self):
        with patch.dict(os.environ,{},clear=True),patch('knowledge.database.psycopg.connect') as connect:
            with self.assertRaisesRegex(ProviderError,'RAG_DATABASE_NOT_CONFIGURED'):
                RunbookStore(self.binding,self.config).signature()
            connect.assert_not_called()
