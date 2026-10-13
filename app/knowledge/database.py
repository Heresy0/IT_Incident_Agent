"""Small psycopg store: exact scoped search and atomic, conflict-checked replacement."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import psycopg
from psycopg.rows import dict_row
from providers.fixtures import ProviderError

SCHEMA = 'incident_runbook_pgvector_v1'


def scope_fields(binding):
    return {'tenant_id':binding.tenant_id, 'service':binding.service, 'environment':binding.environment,
            'service_version':binding.version, 'schema_version':SCHEMA}


def scope_id(binding, config):
    return hashlib.sha256(json.dumps([*scope_fields(binding).values(), config.embedding_url,
        config.embedding_model, config.embedding_dimension], ensure_ascii=False).encode()).hexdigest()


def validate_catalog(rows, binding, config):
    seen = set()
    for row in rows:
        if row.get('scope_id') != scope_id(binding,config) or any(row.get(key) != value for key,value in scope_fields(binding).items()):
            raise ProviderError('RAG_SCOPE_MISMATCH')
        if row.get('embedding_model') != config.embedding_model or row.get('embedding_dimension') != config.embedding_dimension:
            raise ProviderError('RAG_INDEX_INCOMPATIBLE')
        try:
            if (row['document_id'] in seen or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',row['document_id'])
                    or not re.fullmatch(r'[a-f0-9]{64}',row['revision'])
                    or row['category'] not in {'resource','dependency','configuration'}
                    or not isinstance(row['updated_at'],datetime) or row['updated_at'].tzinfo is None
                    or type(row['chunk_count']) is not int or not 1 <= row['chunk_count'] <= 200
                    or row['actual_chunks'] != row['chunk_count']):
                raise ValueError()
            seen.add(row['document_id'])
        except (KeyError, TypeError, ValueError):
            raise ProviderError('RAG_RESULT_INVALID') from None
    if len(rows) > 200 or sum(row['chunk_count'] for row in rows) > 200:
        raise ProviderError('RAG_RESULT_INVALID')
    return tuple(sorted((row['document_id'],row['revision']) for row in rows))


class RunbookSnapshot:
    def __init__(self, connection, binding, config):
        self.connection, self.binding, self.config = connection, binding, config

    def catalog(self):
        rows = self.connection.execute("""
            SELECT d.*, (SELECT count(*) FROM incident_runbook_chunks c
                WHERE c.scope_id=d.scope_id AND c.document_id=d.document_id) AS actual_chunks
            FROM incident_runbook_documents d WHERE d.scope_id=%s ORDER BY d.document_id
        """, (scope_id(self.binding,self.config),)).fetchall()
        validate_catalog(rows,self.binding,self.config)
        return rows

    def search(self, vector, category):
        identity, fields = scope_id(self.binding,self.config), scope_fields(self.binding)
        encoded = json.dumps(vector,allow_nan=False)
        return self.connection.execute("""
            SELECT c.chunk_id AS id, c.text, d.scope_id,d.document_id, d.updated_at, d.revision,
                   d.tenant_id,d.service,d.environment,d.service_version,d.schema_version,d.category,
                   d.embedding_model,d.embedding_dimension,
                   1 - (c.embedding <=> %s::vector) AS retrieval_score
            FROM incident_runbook_chunks c JOIN incident_runbook_documents d
              ON c.scope_id=d.scope_id AND c.document_id=d.document_id
            WHERE d.scope_id=%s AND d.tenant_id=%s AND d.service=%s AND d.environment=%s
              AND d.service_version=%s AND d.schema_version=%s AND d.embedding_model=%s
              AND d.embedding_dimension=%s AND (%s::text IS NULL OR d.category=%s)
            ORDER BY c.embedding <=> %s::vector, c.chunk_id
            LIMIT %s
        """, (encoded,identity,*fields.values(),self.config.embedding_model,self.config.embedding_dimension,
              category,category,encoded,self.config.top_k+1)).fetchall()


class RunbookStore:
    def __init__(self, binding, config):
        self.binding, self.config = binding, config

    @contextmanager
    def connection(self):
        dsn = os.getenv(self.config.database_dsn_env)
        if not dsn:
            raise ProviderError('RAG_DATABASE_NOT_CONFIGURED')
        try:
            with psycopg.connect(dsn,autocommit=True,row_factory=dict_row,connect_timeout=5,
                    options='-c statement_timeout=10000 -c lock_timeout=5000 -c search_path=public,pg_catalog -c timezone=UTC') as connection:
                yield connection
        except (psycopg.errors.UndefinedTable,psycopg.errors.UndefinedObject,psycopg.errors.UndefinedFunction):
            raise ProviderError('RAG_INDEX_NOT_READY') from None
        except psycopg.Error:
            raise ProviderError('RAG_UNAVAILABLE') from None

    def initialize(self):
        # Explicit operator import only; retrieval uses no DDL.
        with self.connection() as connection, connection.transaction():
            connection.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
            connection.execute((Path(__file__).parent/'migrations/001_runbooks.sql').read_text(encoding='utf-8'))
            # Even ALTER TABLE ... IF NOT EXISTS acquires an exclusive lock.
            # Avoid it after migration so uploads can coexist with read snapshots.
            migrated = connection.execute("""SELECT EXISTS (
                SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('public.incident_runbook_documents')
                AND attname='source_name' AND NOT attisdropped) AS ready""").fetchone()['ready']
            if not migrated:
                connection.execute((Path(__file__).parent/'migrations/002_source_name.sql').read_text(encoding='utf-8'))

    @contextmanager
    def snapshot(self):
        with self.connection() as connection, connection.transaction():
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            yield RunbookSnapshot(connection,self.binding,self.config)

    def signature(self):
        with self.snapshot() as snapshot:
            rows = snapshot.catalog()
            return tuple(sorted((row['document_id'],row['revision']) for row in rows))

    def replace(self, rows, vectors, expected):
        identity = scope_id(self.binding,self.config)
        with self.connection() as connection, connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)',(int(identity[:15],16),))
            snapshot = RunbookSnapshot(connection,self.binding,self.config)
            current = tuple(sorted((row['document_id'],row['revision']) for row in snapshot.catalog()))
            if current != expected:
                raise ProviderError('RAG_INDEX_CHANGED')
            connection.execute('DELETE FROM incident_runbook_documents WHERE scope_id=%s',(identity,))
            groups = {}
            for row,vector in zip(rows,vectors,strict=True):
                groups.setdefault(row['document_id'],[]).append((row,vector))
            for document,group in groups.items():
                self._insert_document(connection, identity, document, group)
            self._insert_chunks(connection,identity,rows,vectors)

    @staticmethod
    def check_document(catalog, document, count, expected_revision):
        current = next((row for row in catalog if row['document_id'] == document), None)
        if (current['revision'] if current else None) != expected_revision:
            raise ProviderError('RAG_INDEX_CHANGED')
        if sum(row['chunk_count'] for row in catalog if row['document_id'] != document) + count > 200:
            raise ProviderError('RAG_CHUNK_LIMIT')

    def upsert_document(self, rows, vectors, expected_revision):
        document = rows[0]['document_id']
        if not 1 <= len(rows) <= 200 or any(row['document_id'] != document for row in rows):
            raise ValueError('SINGLE_DOCUMENT_REQUIRED')
        group = list(zip(rows, vectors, strict=True))
        identity = scope_id(self.binding, self.config)
        with self.connection() as connection, connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (int(identity[:15],16),))
            catalog = RunbookSnapshot(connection, self.binding, self.config).catalog()
            self.check_document(catalog, document, len(rows), expected_revision)
            connection.execute('DELETE FROM incident_runbook_documents WHERE scope_id=%s AND document_id=%s',
                               (identity, document))
            revision = self._insert_document(connection, identity, document, group)
            self._insert_chunks(connection, identity, rows, vectors)
        return revision

    def _insert_document(self, connection, identity, document, group):
        first = group[0][0]
        revision = hashlib.sha256(json.dumps(group,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
        connection.execute("""
            INSERT INTO incident_runbook_documents
            (scope_id,document_id,tenant_id,service,environment,service_version,schema_version,
             embedding_model,embedding_dimension,category,updated_at,revision,chunk_count,source_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (identity,document,*scope_fields(self.binding).values(),self.config.embedding_model,
              self.config.embedding_dimension,first['category'],first['updated_at'],revision,len(group),first.get('source_name')))
        return revision

    def _insert_chunks(self, connection, identity, rows, vectors):
        with connection.cursor() as cursor:
            cursor.executemany("""
                INSERT INTO incident_runbook_chunks VALUES (%s,%s,%s,%s,%s,%s::vector)
            """, [(identity,row['document_id'],row['id'],row['text'],self.config.embedding_dimension,
                   json.dumps(vector,allow_nan=False)) for row,vector in zip(rows,vectors,strict=True)])
