"""Upload authorization, bounded files, preview integrity, and embedding failures."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.auth import Principal, get_current_principal
from api.runbooks import router
from knowledge.configuration import RunbookRAGConfig
from knowledge.database import RunbookStore
from knowledge.retrieval import embed
from providers.fixtures import ProviderError
from providers.registry import ServiceBinding, ServiceRegistry
from workflow.dependencies import get_workflow_service


class UploadAPITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'UPLOAD_TEST_KEY': 'mock-only'}))
        self.principal = Principal('tenant', 'alice', 'operator')
        config = RunbookRAGConfig(embedding_dimension=2, embedding_api_key_env='UPLOAD_TEST_KEY', chunk_size=100, chunk_overlap=20)
        binding = ServiceBinding('tenant', ('alice',), 'api', 'staging', 'v1', observations={'runbook_rag': config.model_dump()})
        workflow = SimpleNamespace(services=ServiceRegistry([binding, ServiceBinding('foreign', ('alice',), 'hidden', 'staging', 'v1')]))
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_current_principal] = lambda: self.principal
        app.dependency_overrides[get_workflow_service] = lambda: workflow
        self.client = self.enterContext(TestClient(app))
        self.rows, self.requests, self.writes = [], [], []
        outer = self
        original_check = RunbookStore.check_document
        class Store:
            check_document = staticmethod(original_check)
            def __init__(self, *args): pass
            def initialize(self): pass
            @contextmanager
            def snapshot(self):
                yield SimpleNamespace(catalog=lambda: deepcopy(outer.rows))
            def upsert_document(self, rows, vectors, expected):
                self.check_document(outer.rows, rows[0]['document_id'], len(rows), expected)
                outer.writes.append((deepcopy(rows), deepcopy(vectors)))
                revision = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
                outer.rows[:] = [row for row in outer.rows if row['document_id'] != rows[0]['document_id']]
                outer.rows.append({'document_id': rows[0]['document_id'], 'revision': revision,
                    'source_name': rows[0].get('source_name'), 'category': rows[0]['category'],
                    'updated_at': datetime.fromisoformat(rows[0]['updated_at']), 'chunk_count': len(rows)})
                return revision
        self.enterContext(patch('api.runbooks.RunbookStore', Store))
        self.enterContext(patch('knowledge.ingest.RunbookStore', Store))
        def handle(request):
            self.requests.append(request)
            texts = json.loads(request.content)['input']['texts']
            return httpx.Response(200, json={'output': {'embeddings': [
                {'text_index': i, 'embedding': [1., 0.]} for i in range(len(texts))]}, 'usage': {'total_tokens': 12}})
        self.transport = httpx.MockTransport(handle)
        self.enterContext(patch('knowledge.ingest.embed', side_effect=lambda texts, config, **kwargs: embed(texts, config, transport=self.transport)))
        self.body = '# 连接排查\n\n首先检查等待指标。\n\n## 日志\n\n检查超时日志。'.encode()
        self.params = {'service': 'api', 'environment': 'staging', 'service_version': 'v1', 'filename': '排障.md', 'category': 'resource'}

    def post(self, action, body=None, **params):
        query = {**self.params, **params}
        return self.client.post('/api/v1/runbooks/' + action + '?' + urlencode(query),
            content=self.body if body is None else body, headers={'Content-Type': 'application/octet-stream'})

    def plan(self, **params):
        response = self.post('preview', **params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upload(self, plan, **params):
        return self.post('upload', preview_digest=plan['preview_digest'],
            expected_revision=plan['expected_revision'] or '', embedding_authorized='true', **params)

    def test_preview_is_free_upload_tracks_usage_and_lists_document(self):
        plan = self.plan()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.writes, [])
        self.assertEqual(plan['strategy'], 'markdown')
        self.assertEqual(plan['chunks'], 2)
        response = self.upload(plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['usage']['model_calls'], 1)
        self.assertEqual(response.json()['usage']['token_usage']['input_tokens'], 12)
        listing = self.client.get('/api/v1/runbooks?' + urlencode({k: self.params[k] for k in ('service','environment','service_version')})).json()
        self.assertEqual(listing['items'][0]['source_name'], '排障.md')

    def test_role_tenant_membership_and_exact_version_enforced_before_embedding(self):
        self.principal = Principal('tenant', 'alice', 'user')
        self.assertEqual(self.post('preview').status_code, 403)
        self.assertEqual(self.client.get('/api/v1/runbooks/services').status_code, 403)
        for principal, params in [(Principal('other', 'alice', 'operator'), {}),
                                  (Principal('tenant', 'bob', 'operator'), {}),
                                  (Principal('tenant', 'alice', 'operator'), {'service_version': 'v2'}),
                                  (Principal('tenant', 'alice', 'operator'), {'environment': 'production'})]:
            self.principal = principal
            self.assertEqual(self.post('preview', **params).status_code, 403)
        self.assertEqual(self.requests, [])

    def test_registry_catalog_exposes_only_rag_bindings_without_configuration_secrets(self):
        response = self.client.get('/api/v1/runbooks/services')
        self.assertEqual([row['service'] for row in response.json()['items']], ['api'])
        self.assertNotIn('UPLOAD_TEST_KEY', response.text)
        self.assertNotIn('database_dsn_env', response.text)

    def test_rejects_extension_path_empty_invalid_utf8_binary_and_oversize(self):
        for body, filename, status in [(b'data', 'guide.pdf', 415), (b'data', '../guide.md', 422),
                (b'  ', 'guide.txt', 422), (b'\xff\xfe', 'guide.txt', 422),
                (b'abc\x00def', 'guide.md', 422), (b'x' * (256*1024+1), 'guide.txt', 413),
                (b'x' * 100001, 'guide.txt', 422)]:
            with self.subTest(filename=filename, status=status):
                self.assertEqual(self.post('preview', body=body, filename=filename).status_code, status)
        self.assertEqual(self.requests, [])

    def test_txt_bom_and_redaction_before_splitting(self):
        text = '\ufeff前文。\r\n\r\ntoken=' + 'secret-value'*15 + '\r\n\r\n后文必须保留。'*8
        self.body = text.encode()
        plan = self.plan(filename='guide.TXT')
        self.assertEqual(plan['strategy'], 'text')
        self.assertNotIn('secret-value', str(plan))
        self.assertNotIn('\ufeff', str(plan))
        response = self.upload(plan, filename='guide.TXT')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('secret-value', str(self.writes))
        self.assertIn('后文必须保留', str(self.writes))

    def test_paid_authorization_and_matching_preview_required(self):
        plan = self.plan()
        self.assertEqual(self.post('upload', preview_digest=plan['preview_digest']).status_code, 422)
        self.assertEqual(self.upload(plan, category='dependency').status_code, 409)
        self.assertEqual(self.upload(plan, body=self.body + b' changed').status_code, 409)
        self.assertEqual(self.requests, [])

    def test_stale_preview_refuses_second_embedding_and_update_gets_new_revision(self):
        plan = self.plan()
        self.assertEqual(self.upload(plan).status_code, 200)
        spent = len(self.requests)
        self.assertEqual(self.upload(plan).status_code, 409)
        self.assertEqual(len(self.requests), spent)
        new_plan = self.plan()
        self.assertTrue(new_plan['replaces_existing'])
        self.assertEqual(self.upload(new_plan).status_code, 200)

    def test_failed_embedding_preserves_old_document(self):
        self.assertEqual(self.upload(self.plan()).status_code, 200)
        before = deepcopy(self.rows)
        self.body += b'\nupdated'
        plan = self.plan()
        self.transport = httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        self.assertEqual(self.upload(plan).status_code, 503)
        self.assertEqual(self.rows, before)
        self.assertEqual(len(self.writes), 1)

    def test_scope_capacity_rejected_before_paid_embedding(self):
        self.rows.append({'document_id': 'other', 'revision': 'a'*64, 'chunk_count': 200})
        self.assertEqual(self.post('preview').status_code, 422)
        self.assertEqual(self.requests, [])

    def test_unauthenticated_requests_and_media_type(self):
        self.client.app.dependency_overrides.pop(get_current_principal)
        self.assertEqual(self.post('preview').status_code, 401)
        self.client.app.dependency_overrides[get_current_principal] = lambda: self.principal
        response = self.client.post('/api/v1/runbooks/preview?' + urlencode(self.params), content=self.body)
        self.assertEqual(response.status_code, 415)


class ChunkingTests(unittest.TestCase):
    def test_markdown_parent_context_retained_on_long_children_and_fenced_headers_ignored(self):
        from knowledge.chunking import split_text
        config = RunbookRAGConfig(chunk_size=100, chunk_overlap=20)
        chunks = split_text('# 资源\n## 内存\n' + '检查堆内存。'*45 + '\n```sh\n# 这是代码\necho ok\n```\n## CPU\n检查CPU使用率。', config, 'markdown')
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        memory = [chunk for chunk in chunks if '检查堆内存' in chunk]
        self.assertGreater(len(memory), 2)
        self.assertTrue(all(chunk.startswith('# 资源\n## 内存\n') for chunk in memory))
        code = next(chunk for chunk in chunks if '# 这是代码' in chunk)
        self.assertTrue(code.startswith('# 资源\n## 内存\n'))
        self.assertIn('# 资源\n## CPU\n', chunks[-1])

    def test_heading_only_and_long_heading_stay_bounded(self):
        from knowledge.chunking import split_text
        config = RunbookRAGConfig(chunk_size=100)
        chunks = split_text('# ' + '标题'*100 + '\n' + '内容'*200, config, 'markdown')
        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= 100 for chunk in chunks))
        self.assertEqual(split_text('# 标题', config, 'markdown'), ['# 标题'])
