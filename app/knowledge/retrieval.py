"""Small local vector indexes return scoped, cited runbook evidence."""
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import math
import os

from knowledge.configuration import RunbookRAGConfig
from providers.fixtures import ProviderError
from runtime.context import current, ExecutionError

SCHEMA = 'incident_runbook_v1'


def configuration(binding):
    return RunbookRAGConfig.model_validate(binding.observations['runbook_rag'])


def index_path(binding, config):
    from pathlib import Path
    identity = [SCHEMA, binding.tenant_id, binding.service, binding.environment,
                binding.version, config.embedding_url, config.embedding_model, config.embedding_dimension]
    name = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:32]
    directory = Path(config.index_dir)
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parents[2] / directory
    return directory / (name + '.json')


def valid_vector(vector, dimension):
    return (isinstance(vector, list) and len(vector) == dimension and any(vector)
            and all(type(x) in (int, float) and math.isfinite(x) for x in vector))


def embed(texts, config, *, query=False, transport=None):
    """One HTTP request, charged to the model budget, with reported token usage."""
    from providers.http import request_json
    if not os.getenv(config.embedding_api_key_env):
        raise ProviderError('PROVIDER_AUTH_MISSING')
    context = current()
    if context:
        if sum(map(len, texts)) > context.limits.input_chars:
            raise ExecutionError('BUDGET_EXCEEDED')
        context.reserve('model')
    with context.span('model', 'knowledge_embedding') if context else nullcontext():
        parameters = {'text_type': 'query' if query else 'document'}
        if config.embedding_model in {'text-embedding-v3', 'text-embedding-v4', 'qwen3.7-text-embedding'}:
            parameters['dimension'] = config.embedding_dimension
        response = request_json(config.embedding_url, 'POST',
            '/api/v1/services/embeddings/text-embedding/text-embedding',
            bearer_env=config.embedding_api_key_env, transport=transport,
            json_body={'model': config.embedding_model, 'input': {'texts': texts},
                       'parameters': parameters})
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


def scope_fields(binding):
    return {'tenant_id': binding.tenant_id, 'service': binding.service,
            'environment': binding.environment, 'service_version': binding.version, 'schema_version': SCHEMA}


def read_index(binding, config):
    """Validate the whole snapshot before any paid query embedding."""
    import re
    path = index_path(binding, config)
    if not path.exists():
        raise ProviderError('RAG_INDEX_NOT_READY')
    try:
        with path.open('rb') as handle:
            raw = handle.read(32_000_001)
        if len(raw) > 32_000_000:
            raise ValueError()
        index = json.loads(raw)
        if index['scope'] != scope_fields(binding):
            raise ProviderError('RAG_SCOPE_MISMATCH')
        if (index['embedding_model'] != config.embedding_model or
                index['embedding_dimension'] != config.embedding_dimension):
            raise ProviderError('RAG_INDEX_INCOMPATIBLE')
        rows = index['rows']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            raise ValueError()
        seen = set()
        for row in rows:
            if any(row.get(k) != v for k, v in scope_fields(binding).items()):
                raise ProviderError('RAG_SCOPE_MISMATCH')
            if (not valid_vector(row['vector'], config.embedding_dimension)
                    or row['id'] in seen or not re.fullmatch(r'[a-f0-9]{64}', row['id'])
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', row['document_id'])
                    or row['category'] not in {'resource', 'dependency', 'configuration'}
                    or not isinstance(row['text'], str) or not row['text'].strip()
                    or len(row['text']) > 1800 or datetime.fromisoformat(row['updated_at']).tzinfo is None):
                raise ValueError()
            seen.add(row['id'])
        return rows, hashlib.sha256(raw).hexdigest()[:24]
    except ProviderError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ProviderError('RAG_RESULT_INVALID') from None


def cosine(left, right):
    # hypot avoids overflow for otherwise finite vectors.
    left_norm, right_norm = math.hypot(*left), math.hypot(*right)
    return sum((a/left_norm)*(b/right_norm) for a, b in zip(left, right, strict=True))


def search_runbooks(binding, scope, args, *, transport=None):
    from providers.http import safe_text
    if (scope.tenant_id, scope.service, scope.environment, scope.service_version) != (
            binding.tenant_id, binding.service, binding.environment, binding.version) or scope.user_id not in binding.users:
        raise ProviderError('SCOPE_DENIED')
    config = configuration(binding)
    records, version = read_index(binding, config)
    records = [r for r in records if not args.category or r['category'] == args.category]
    if not records:
        return [], False
    vector = embed([args.query], config, query=True, transport=transport)[0]
    ranked = sorted(((cosine(vector, r['vector']), r) for r in records),
                    key=lambda item: (-item[0], item[1]['id']))
    ranked = [(score, r) for score, r in ranked if score >= config.min_score]
    rows = []
    for score, record in ranked[:config.top_k]:
        rows.append({'id': record['id'][:24], 'timestamp': record['updated_at'],
            'service': binding.service, 'environment': binding.environment,
            'data_version': SCHEMA+'/'+version, 'versions': [binding.version],
            'category': record['category'], 'text': safe_text(record['text']),
            'updated_at': record['updated_at'], 'document_id': record['document_id'],
            'retrieval_score': score, 'content_truncated': False})
    return rows, len(ranked) > config.top_k
