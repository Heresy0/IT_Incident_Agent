"""Real pgvector acceptance only in an explicitly configured *_test database."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4
import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from api.auth import Principal
from knowledge.configuration import RunbookRAGConfig
from knowledge.database import RunbookStore, scope_id
from knowledge.ingest import build_index, prepare, upload_document
from knowledge.retrieval import search_runbooks
from evidence.contracts import RunbooksArgs
from providers.registry import ServiceBinding, ServiceRegistry
from providers.fixtures import ProviderError


@unittest.skipUnless(os.getenv('INCIDENT_TEST_RUNBOOK_DSN'),'requires isolated pgvector *_test database')
class RunbookPgvectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn=os.environ['INCIDENT_TEST_RUNBOOK_DSN']
        if not conninfo_to_dict(cls.dsn).get('dbname','').endswith('_test'):
            raise RuntimeError('Runbook tests require an isolated *_test database')

    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'RUNBOOK_TEST_KEY':'mock-only'}))
        self.config=RunbookRAGConfig(database_dsn_env='INCIDENT_TEST_RUNBOOK_DSN',
            embedding_dimension=2,embedding_api_key_env='RUNBOOK_TEST_KEY')
        self.binding=ServiceBinding('rag_test_'+uuid4().hex,('reader',),'api','staging','v1',
            observations={'runbook_rag':self.config.model_dump()})
        self.scopes={scope_id(self.binding,self.config)}
        self.store=RunbookStore(self.binding,self.config)
        self.store.initialize()
        self.requests=[]
        def handle(request):
            self.requests.append(request)
            body=json.loads(request.content)
            vectors=[]
            for text in body['input']['texts']:
                vector=[1.,0.] if body['parameters']['text_type']=='query' or text.startswith('match') else (
                    [-1.,0.] if text.startswith('opposite') else [0.,1.])
                vectors.append(vector)
            return httpx.Response(200,json={'output':{'embeddings':[
                {'text_index':i,'embedding':v} for i,v in enumerate(vectors)]},'usage':{'total_tokens':8}})
        self.transport=httpx.MockTransport(handle)
        self.docs=[{'document_id':'guide','category':'resource','text':'match 连接等待排查',
                    'updated_at':'2026-10-13T00:00:00Z'}]
        self.scope=self.make_scope(self.binding)

    def tearDown(self):
        # Remove only this test's generated scopes, never tables or unrelated data.
        with self.store.connection() as connection:
            for identity in self.scopes:
                connection.execute('DELETE FROM incident_runbook_documents WHERE scope_id=%s',(identity,))

    def make_scope(self,binding):
        stamp=datetime.now(timezone.utc)
        return ServiceRegistry([binding]).ticket({'id':'test','title':'timeout','symptoms':'等待',
            'service':binding.service,'environment':binding.environment,'service_version':binding.version,
            'start':stamp.isoformat(),'end':(stamp+timedelta(minutes=10)).isoformat()},
            Principal(binding.tenant_id,'reader')).scope

    def ingest(self,documents=None,binding=None):
        binding=binding or self.binding
        self.scopes.add(scope_id(binding,self.config))
        return build_index(binding,documents or self.docs,transport=self.transport)

    def search(self,category=None):
        return search_runbooks(self.binding,self.scope,RunbooksArgs(query='连接等待',category=category),transport=self.transport)

    def test_real_cosine_threshold_category_and_top_k(self):
        documents=[{**self.docs[0],'document_id':f'match-{i}'} for i in range(4)]
        documents += [{**self.docs[0],'document_id':'east','text':'east 不相关'},
                      {**self.docs[0],'document_id':'opposite','text':'opposite 相反'},
                      {**self.docs[0],'document_id':'other-category','category':'dependency'}]
        self.ingest(documents)
        rows,truncated=self.search('resource')
        self.assertEqual(len(rows),3)
        self.assertTrue(truncated)
        self.assertTrue(all(row['document_id'].startswith('match-') for row in rows))
        self.assertTrue(all(abs(row['retrieval_score']-1)<1e-6 for row in rows))
        self.assertEqual(self.search('dependency')[0][0]['document_id'],'other-category')
        before=len(self.requests)
        self.assertEqual(self.search('configuration'),([],False))
        self.assertEqual(len(self.requests),before)

    def test_sql_filters_tenant_service_environment_and_version(self):
        self.ingest()
        for binding in (replace(self.binding,tenant_id='other_'+uuid4().hex),replace(self.binding,service='other'),
                        replace(self.binding,environment='production'),replace(self.binding,version='v2')):
            self.ingest([{**self.docs[0],'document_id':'foreign'}],binding)
        # Variable-dimension vectors share the table but must never enter this distance calculation.
        config3=self.config.model_copy(update={'embedding_dimension':3})
        binding3=replace(self.binding,observations={'runbook_rag':config3.model_dump()})
        self.scopes.add(scope_id(binding3,config3))
        _,chunks3=prepare(binding3,self.docs)
        RunbookStore(binding3,config3).replace(chunks3,[[1.,0.,0.]],())
        rows,_=self.search()
        self.assertEqual([row['document_id'] for row in rows],['guide'])

    def test_replace_removes_old_document_and_updates_revision(self):
        self.ingest()
        original=self.store.signature()
        self.ingest([{**self.docs[0],'document_id':'new','text':'match 新手册'}])
        self.assertNotEqual(self.store.signature(),original)
        self.assertEqual([row['document_id'] for row in self.search()[0]],['new'])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM incident_runbook_chunks WHERE scope_id=%s',
                (scope_id(self.binding,self.config),)).fetchone()['count'],1)

    def test_failed_chunk_write_rolls_back_document_delete_and_insert(self):
        self.ingest()
        original=self.store.signature()
        with patch.object(RunbookStore,'_insert_chunks',side_effect=ProviderError('RAG_UNAVAILABLE')):
            with self.assertRaisesRegex(ProviderError,'RAG_UNAVAILABLE'):
                self.ingest([{**self.docs[0],'document_id':'new'}])
        self.assertEqual(self.store.signature(),original)
        self.assertEqual(self.search()[0][0]['document_id'],'guide')

    def test_stale_import_signature_cannot_overwrite_new_documents(self):
        self.ingest()
        expected=self.store.signature()
        _,rows=prepare(self.binding,self.docs)
        self.ingest([{**self.docs[0],'document_id':'new'}])
        with self.assertRaisesRegex(ProviderError,'RAG_INDEX_CHANGED'):
            self.store.replace(rows,[[1.,0.]],expected)
        self.assertEqual(self.search()[0][0]['document_id'],'new')

    def test_repeatable_read_keeps_catalog_and_fragments_from_same_snapshot(self):
        self.ingest()
        with self.store.snapshot() as snapshot:
            catalog=snapshot.catalog()
            self.ingest([{**self.docs[0],'document_id':'new'}])
            records=snapshot.search([1.,0.],None)
            self.assertEqual(records[0]['document_id'],'guide')
            self.assertEqual(records[0]['revision'],catalog[0]['revision'])
        self.assertEqual(self.search()[0][0]['document_id'],'new')

    def test_database_constraints_reject_zero_vector_and_dimension_mismatch(self):
        self.ingest()
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            for statement in ("UPDATE incident_runbook_chunks SET embedding='[0,0]'::vector WHERE scope_id=%s",
                              "UPDATE incident_runbook_chunks SET embedding_dimension=3 WHERE scope_id=%s"):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    connection.execute(statement,(scope_id(self.binding,self.config),))
        self.assertEqual(self.search()[0][0]['document_id'],'guide')

    def test_tampered_catalog_scope_rejected_before_query_embedding(self):
        self.ingest()
        with self.store.connection() as connection:
            connection.execute("UPDATE incident_runbook_documents SET tenant_id='foreign' WHERE scope_id=%s",
                               (scope_id(self.binding,self.config),))
        before=len(self.requests)
        with self.assertRaisesRegex(ProviderError,'RAG_SCOPE_MISMATCH'):
            self.search()
        self.assertEqual(len(self.requests),before)

    def test_single_upload_preserves_other_document_and_supports_chinese_source_name(self):
        self.ingest()
        guide_revision = dict(self.store.signature())['guide']
        upload = {**self.docs[0], 'document_id': 'upload-demo', 'source_name': '内存排查.md',
                  'source_format': 'markdown', 'text': '# 资源\n检查内存。'}
        result = upload_document(self.binding, upload, None, transport=self.transport)
        self.assertEqual(dict(self.store.signature())['guide'], guide_revision)
        with self.store.snapshot() as snapshot:
            self.assertEqual(next(row for row in snapshot.catalog() if row['document_id']=='upload-demo')['source_name'], '内存排查.md')
        second = upload_document(self.binding, {**upload, 'text': '# 资源\n更新内存排查。'}, result['revision'], transport=self.transport)
        self.assertNotEqual(second['revision'], result['revision'])
        self.assertEqual(dict(self.store.signature())['guide'], guide_revision)

    def test_single_upload_stale_revision_rejected_before_embedding(self):
        self.ingest()
        spent = len(self.requests)
        with self.assertRaisesRegex(ProviderError, 'RAG_INDEX_CHANGED'):
            upload_document(self.binding, self.docs[0], None, transport=self.transport)
        self.assertEqual(len(self.requests), spent)
        self.assertEqual(len(self.store.signature()), 1)

    def test_single_upload_failed_write_rolls_back_old_document_and_other_documents(self):
        self.ingest([*self.docs, {**self.docs[0], 'document_id':'other'}])
        before = self.store.signature()
        with patch.object(RunbookStore, '_insert_chunks', side_effect=ProviderError('RAG_UNAVAILABLE')):
            with self.assertRaisesRegex(ProviderError, 'RAG_UNAVAILABLE'):
                upload_document(self.binding, {**self.docs[0], 'text':'match 新资料'}, dict(before)['guide'], transport=self.transport)
        self.assertEqual(self.store.signature(), before)

    def test_single_upload_commit_rechecks_revision_and_scope_capacity(self):
        self.ingest()
        expected = dict(self.store.signature())['guide']
        _, rows = prepare(self.binding, self.docs)
        self.ingest([{**self.docs[0], 'text':'match 最新资料'}])
        current = self.store.signature()
        with self.assertRaisesRegex(ProviderError, 'RAG_INDEX_CHANGED'):
            self.store.upsert_document(rows, [[1.,0.]], expected)
        self.assertEqual(self.store.signature(), current)
        self.ingest([{**self.docs[0], 'document_id':f'guide-{i}'} for i in range(200)])
        spent = len(self.requests)
        with self.assertRaisesRegex(ProviderError, 'RAG_CHUNK_LIMIT'):
            upload_document(self.binding, self.docs[0], None, transport=self.transport)
        self.assertEqual(len(self.requests), spent)

    def test_upload_http_to_database_to_retrieval_without_paid_model(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from urllib.parse import urlencode
        from types import SimpleNamespace
        from api.runbooks import router
        from api.auth import get_current_principal
        from workflow.dependencies import get_workflow_service
        from knowledge.retrieval import embed
        self.ingest()
        guide_revision = dict(self.store.signature())['guide']
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_current_principal] = lambda: Principal(self.binding.tenant_id, 'reader', 'operator')
        app.dependency_overrides[get_workflow_service] = lambda: SimpleNamespace(services=ServiceRegistry([self.binding]))
        params = {'service':'api', 'environment':'staging', 'service_version':'v1', 'filename':'内存手册.txt', 'category':'resource'}
        body = 'match 内存排查。\n\ntoken=must-be-redacted\n\n核对指标和日志。'.encode()
        self.requests.clear()
        with TestClient(app) as client, patch('knowledge.ingest.embed',
                side_effect=lambda texts, config, **kwargs: embed(texts, config, transport=self.transport)):
            preview = client.post('/api/v1/runbooks/preview?' + urlencode(params), content=body,
                                 headers={'Content-Type':'application/octet-stream'})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(self.requests, [])
            plan = preview.json()
            self.assertNotIn('must-be-redacted', preview.text)
            response = client.post('/api/v1/runbooks/upload?' + urlencode({**params,
                'preview_digest':plan['preview_digest'], 'expected_revision':'', 'embedding_authorized':'true'}),
                content=body, headers={'Content-Type':'application/octet-stream'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['usage']['model_calls'], 1)
            self.assertEqual(dict(self.store.signature())['guide'], guide_revision)
            rows, _ = self.search()
            self.assertIn(plan['document_id'], [row['document_id'] for row in rows])
            self.assertNotIn('must-be-redacted', str(rows))
