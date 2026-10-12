"""Offline ingestion: embed first, then atomically replace a local index."""
from datetime import datetime
import hashlib
import json
import os
import tempfile
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from langchain_text_splitters import RecursiveCharacterTextSplitter
from knowledge.retrieval import index_path, configuration, embed, scope_fields
from providers.fixtures import ProviderError
from runtime.context import ExecutionError


class RunbookDocument(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    category: Literal['resource', 'dependency', 'configuration']
    text: str = Field(min_length=1, max_length=100000)
    updated_at: str

    @field_validator('updated_at')
    @classmethod
    def timestamp(cls, value):
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError('TIMESTAMP_REQUIRED')
        return stamp.isoformat()


def prepare(binding, documents):
    from providers.http import safe_text
    config = configuration(binding)
    splitter = RecursiveCharacterTextSplitter(chunk_size=config.chunk_size, chunk_overlap=config.chunk_overlap,
        separators=['\n\n', '\n', '。', '！', '？', '；', '，', ' ', ''])
    ids, rows = set(), []
    for raw in documents:
        document = RunbookDocument.model_validate(raw)
        if document.document_id in ids or not document.text.strip():
            raise ValueError('DUPLICATE_OR_EMPTY_DOCUMENT')
        ids.add(document.document_id)
        # Redact before splitting so credentials spanning a chunk boundary
        # cannot survive as a partial value in the next chunk.
        for index, text in enumerate(splitter.split_text(safe_text(document.text, max_chars=100000))):
            rows.append({**scope_fields(binding), 'category': document.category,
                'text': text, 'document_id': document.document_id, 'updated_at': document.updated_at,
                'id': hashlib.sha256(f'{document.document_id}/{index}/{text}'.encode()).hexdigest()})
    if not rows or len(rows) > 200:
        raise ValueError('INDEX_REQUIRES_1_TO_200_CHUNKS')
    return config, rows


def build_index(binding, documents, *, transport=None):
    config, rows = prepare(binding, documents)
    temporary = None
    try:
        # A failed embedding leaves the previous usable snapshot intact.
        vectors = []
        for offset in range(0, len(rows), 8):
            vectors.extend(embed([r['text'] for r in rows[offset:offset+8]], config, transport=transport))
        snapshot = {'scope': scope_fields(binding), 'embedding_model': config.embedding_model,
            'embedding_dimension': config.embedding_dimension,
            'rows': [{**r, 'vector': v} for r, v in zip(rows, vectors, strict=True)]}
        name = index_path(binding, config)
        name.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=name.parent,
                                        suffix='.tmp', delete=False) as handle:
            temporary = handle.name
            json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name)
        temporary = None
        return {'index': str(name), 'documents': len(documents), 'chunks': len(rows), 'status': 'ready'}
    except (ProviderError, ExecutionError):
        raise
    except Exception:
        raise ProviderError('RAG_UNAVAILABLE') from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
