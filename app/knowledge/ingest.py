"""Offline ingestion: embed first, then transactionally replace scoped documents."""
from datetime import datetime
import hashlib
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from knowledge.chunking import split_text
from knowledge.retrieval import configuration, embed
from knowledge.database import RunbookStore, scope_fields, scope_id


class RunbookDocument(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
    category: Literal['resource', 'dependency', 'configuration']
    text: str = Field(min_length=1, max_length=100000)
    updated_at: str
    source_format: Literal['text', 'markdown'] = 'text'
    source_name: str | None = Field(default=None, min_length=1, max_length=180)

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
    ids, rows = set(), []
    for raw in documents:
        document = RunbookDocument.model_validate(raw)
        if document.document_id in ids or not document.text.strip():
            raise ValueError('DUPLICATE_OR_EMPTY_DOCUMENT')
        ids.add(document.document_id)
        # Redact before splitting so credentials spanning a chunk boundary
        # cannot survive as a partial value in the next chunk.
        for index, text in enumerate(split_text(safe_text(document.text, max_chars=100000), config, document.source_format)):
            rows.append({**scope_fields(binding), 'category': document.category,
                'text': text, 'document_id': document.document_id, 'updated_at': document.updated_at,
                'id': hashlib.sha256(f'{document.document_id}/{index}/{text}'.encode()).hexdigest()})
            if document.source_name:
                rows[-1]['source_name'] = safe_text(document.source_name, max_chars=180)
    if not rows or len(rows) > 200:
        raise ValueError('INDEX_REQUIRES_1_TO_200_CHUNKS')
    return config, rows


def build_index(binding, documents, *, transport=None):
    config, rows = prepare(binding,documents)
    store = RunbookStore(binding,config)
    store.initialize()
    expected = store.signature()
    vectors = []
    for offset in range(0,len(rows),8):
        vectors.extend(embed([r['text'] for r in rows[offset:offset+8]],config,transport=transport))
    store.replace(rows,vectors,expected)
    return {'scope_id':scope_id(binding,config), 'documents':len(documents), 'chunks':len(rows),
            'backend':'pgvector', 'status':'ready'}


def upload_document(binding, document, expected_revision, *, transport=None):
    """Single-document import: preflight before billing, atomic replacement after it."""
    config, rows = prepare(binding, [document])
    store = RunbookStore(binding, config)
    store.initialize()
    with store.snapshot() as snapshot:
        store.check_document(snapshot.catalog(), rows[0]['document_id'], len(rows), expected_revision)
    vectors = []
    for offset in range(0, len(rows), 8):
        vectors.extend(embed([row['text'] for row in rows[offset:offset+8]], config, transport=transport))
    revision = store.upsert_document(rows, vectors, expected_revision)
    return {'document_id': rows[0]['document_id'], 'revision': revision,
            'chunks': len(rows), 'backend': 'pgvector', 'status': 'ready'}
