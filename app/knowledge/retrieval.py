"""pgvector results become ordinary scoped, cited runbook evidence."""
from contextlib import nullcontext
from datetime import datetime
import math
import os

from knowledge.configuration import RunbookRAGConfig
from providers.fixtures import ProviderError
from runtime.context import current, ExecutionError

from knowledge.database import SCHEMA, RunbookStore, scope_fields, scope_id


def configuration(binding):
    return RunbookRAGConfig.model_validate(binding.observations['runbook_rag'])


def valid_vector(vector, dimension):
    return (isinstance(vector, list) and len(vector) == dimension and any(vector)
            and all(type(x) in (int, float) and math.isfinite(x) for x in vector))


def embed(texts, config, *, query=False, transport=None, _durable=False):
    """One HTTP request, charged to the model budget, with reported token usage."""
    from providers.http import request_json
    if not os.getenv(config.embedding_api_key_env):
        raise ProviderError('PROVIDER_AUTH_MISSING')
    context = current()
    if context and context.journal is not None and not _durable:
        request = {'name': 'knowledge_embedding', 'model': config.embedding_model,
                   'dimension': config.embedding_dimension, 'texts': texts, 'query': query}
        with context.journal.operation('model', request):
            return embed(texts, config, query=query, transport=transport, _durable=True)
    if context:
        if sum(map(len, texts)) > context.limits.input_chars:
            raise ExecutionError('BUDGET_EXCEEDED')
        context.reserve('model')
    with context.span('model', 'knowledge_embedding') if context else nullcontext():
        parameters = {'text_type': 'query' if query else 'document'}
        if config.embedding_model in {'text-embedding-v3', 'text-embedding-v4', 'qwen3.7-text-embedding'}:
            parameters['dimension'] = config.embedding_dimension
        def request():
            return request_json(config.embedding_url, 'POST',
                '/api/v1/services/embeddings/text-embedding/text-embedding',
                bearer_env=config.embedding_api_key_env, transport=transport,
                json_body={'model': config.embedding_model, 'input': {'texts': texts},
                           'parameters': parameters})
        response = context.journal.response(request, kind='embedding') if context and context.journal is not None else request()
        usage = response.get('usage') if isinstance(response, dict) else None
        tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
        if context and type(tokens) is int and tokens >= 0:
            context.add_usage({'input_tokens': tokens, 'output_tokens': 0})
            context.emit({'type': 'usage', 'node': 'knowledge_embedding', 'input_tokens': tokens, 'output_tokens': 0})
        try:
            records = response['output']['embeddings']
            records = sorted(records, key=lambda row: row['text_index'])
            if [r['text_index'] for r in records] != list(range(len(texts))):
                raise ValueError()
            vectors = [r['embedding'] for r in records]
            if any(not valid_vector(v, config.embedding_dimension) for v in vectors):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ProviderError('EMBEDDING_FORMAT_INVALID') from None
        return vectors


def search_runbooks(binding, scope, args, *, transport=None):
    from providers.http import safe_text
    if (scope.tenant_id, scope.service, scope.environment, scope.service_version) != (
            binding.tenant_id, binding.service, binding.environment, binding.version) or scope.user_id not in binding.users:
        raise ProviderError('SCOPE_DENIED')
    config = configuration(binding)
    with RunbookStore(binding,config).snapshot() as snapshot:
        catalog = snapshot.catalog()
        if not catalog:
            raise ProviderError('RAG_INDEX_NOT_READY')
        if not any(not args.category or row['category'] == args.category for row in catalog):
            return [], False
        vector = embed([args.query],config,query=True,transport=transport)[0]
        records = snapshot.search(vector,args.category)
        rows, seen = [], set()
        for record in records:
            if record.get('scope_id') != scope_id(binding,config) or any(record.get(key) != value for key,value in scope_fields(binding).items()):
                raise ProviderError('RAG_SCOPE_MISMATCH')
            if (record.get('embedding_model') != config.embedding_model
                    or record.get('embedding_dimension') != config.embedding_dimension):
                raise ProviderError('RAG_INDEX_INCOMPATIBLE')
            try:
                score, text, stamp = record['retrieval_score'],record['text'],record['updated_at']
                document = next((row for row in catalog if row['document_id'] == record['document_id']),None)
                if (type(score) not in (float,int) or not math.isfinite(score)
                        or not isinstance(text,str) or not text.strip() or len(text) > 1800
                        or not isinstance(stamp,datetime) or stamp.tzinfo is None
                        or (args.category and record['category'] != args.category) or document is None
                        or record['revision'] != document['revision']):
                    raise ValueError()
                import re
                if not re.fullmatch(r'[a-f0-9]{64}',record['id']) or not re.fullmatch(r'[a-f0-9]{64}',record['revision']):
                    raise ValueError()
            except (KeyError,TypeError,ValueError):
                raise ProviderError('RAG_RESULT_INVALID') from None
            if score < config.min_score or record['id'] in seen:
                continue
            seen.add(record['id'])
            rows.append({'id':record['id'][:24], 'timestamp':stamp.isoformat(), 'service':binding.service,
                'environment':binding.environment, 'data_version':SCHEMA+'/'+record['revision'][:24],
                'versions':[binding.version], 'category':record['category'], 'text':safe_text(text),
                'updated_at':stamp.isoformat(), 'document_id':record['document_id'],
                'retrieval_score':score, 'content_truncated':False})
        return rows[:config.top_k],len(rows) > config.top_k
