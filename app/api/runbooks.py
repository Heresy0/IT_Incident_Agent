"""Authenticated single-file uploads; stream size is bounded before decoding."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from threading import BoundedSemaphore
from typing import Literal
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from api.auth import Principal, get_current_principal
from knowledge.database import RunbookStore
from knowledge.ingest import prepare, upload_document
from knowledge.retrieval import configuration
from providers.fixtures import ProviderError
from runtime.context import ExecutionError, Limits, MESSAGES, RunContext, activate
from workflow.dependencies import get_workflow_service

router = APIRouter(prefix='/api/v1/runbooks', tags=['runbooks'])
MAX_BYTES = 256 * 1024
_uploads = BoundedSemaphore(2)
FILE_BODY = {'requestBody': {'required': True, 'content': {
    'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}}


def operator(principal: Principal = Depends(get_current_principal)):
    if principal.role != 'operator':
        raise HTTPException(403, '只有运维身份可以管理排障手册。')
    return principal


def binding_for(service: str = Query(min_length=1, max_length=100),
                environment: Literal['staging', 'production'] = Query(),
                service_version: str = Query(min_length=1, max_length=100),
                principal: Principal = Depends(operator), workflow=Depends(get_workflow_service)):
    try:
        binding = workflow.services.resolve({'service': service, 'environment': environment,
                                            'service_version': service_version}, principal)
    except ProviderError:
        raise HTTPException(403, '不能访问该服务范围。') from None
    if not binding.observations.get('runbook_rag'):
        raise HTTPException(422, '该服务尚未配置排障手册 RAG。')
    return binding


async def call(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except ProviderError as exc:
        status = 409 if exc.code == 'RAG_INDEX_CHANGED' else (422 if exc.code == 'RAG_CHUNK_LIMIT' else 503)
        raise HTTPException(status, MESSAGES.get(exc.code, '手册导入或向量服务不可用，请检查服务配置。')) from None
    except ExecutionError as exc:
        raise HTTPException(422, str(exc)) from None
    except ValueError:
        raise HTTPException(422, '手册内容无效或切分后超过 200 个片段。') from None
    except HTTPException:
        raise
    except Exception as exc:
        logging.getLogger('incident.api').error('runbook_api_failed | exception_type=%s', type(exc).__name__)
        raise HTTPException(503, '排障手册服务暂不可用。') from None


def catalog(binding):
    try:
        with RunbookStore(binding, configuration(binding)).snapshot() as snapshot:
            rows = snapshot.catalog()
    except ProviderError as exc:
        if exc.code != 'RAG_INDEX_NOT_READY':
            raise
        return [], False
    return rows, True


@router.get('/services')
def services(principal: Principal = Depends(operator), workflow=Depends(get_workflow_service)):
    items = []
    for item in workflow.services.catalog(principal):
        binding = workflow.services.resolve(item, principal)
        if binding.observations.get('runbook_rag'):
            config = configuration(binding)
            items.append({key: item[key] for key in ('service', 'environment', 'service_version')} |
                         {'chunk_size': config.chunk_size, 'chunk_overlap': config.chunk_overlap})
    return {'items': items, 'max_bytes': MAX_BYTES}


@router.get('')
async def listing(binding=Depends(binding_for)):
    def read():
        rows, ready = catalog(binding)
        return {'index_ready': ready, 'items': [{'document_id': row['document_id'],
            'source_name': row.get('source_name'), 'category': row['category'],
            'updated_at': row['updated_at'].isoformat(), 'revision': row['revision'],
            'chunks': row['chunk_count']} for row in rows]}
    return await call(read)


async def document_from(request, filename, category):
    filename = unicodedata.normalize('NFC', filename)
    if (not 1 <= len(filename) <= 180 or filename != filename.strip()
            or any(char in filename for char in '/\\') or re.search(r'[\x00-\x1f\x7f]', filename)):
        raise HTTPException(422, '文件名无效。')
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension not in {'md', 'txt'}:
        raise HTTPException(415, '仅支持 .md 和 .txt 文件。')
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/octet-stream':
        raise HTTPException(415, '请以 application/octet-stream 上传单个文件。')
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > MAX_BYTES:
            raise HTTPException(413, '文件不能超过 256 KiB。')
        data.extend(part)
    try:
        text = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeDecodeError:
        raise HTTPException(422, '文件必须使用 UTF-8 编码，可带 BOM。') from None
    if not text.strip() or len(text) > 100000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', text):
        raise HTTPException(422, '文件为空、超过 10 万字符或包含无效控制字符。')
    return {'document_id': 'upload-' + hashlib.sha256(filename.casefold().encode()).hexdigest()[:32],
            'category': category, 'text': text, 'source_name': filename,
            'source_format': 'markdown' if extension == 'md' else 'text',
            'updated_at': datetime.now(timezone.utc).isoformat()}, hashlib.sha256(data).hexdigest()


def digest(binding, rows, content_hash, revision):
    stable = [{key: value for key, value in row.items() if key != 'updated_at'} for row in rows]
    return hashlib.sha256(json.dumps([binding.fingerprint, stable, content_hash, revision],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@router.post('/preview', openapi_extra=FILE_BODY)
async def preview(request: Request, filename: str = Query(min_length=1, max_length=180),
                  category: Literal['resource', 'dependency', 'configuration'] = Query(),
                  binding=Depends(binding_for)):
    document, content_hash = await document_from(request, filename, category)
    def plan():
        config, rows = prepare(binding, [document])
        existing, _ = catalog(binding)
        current = next((row for row in existing if row['document_id'] == document['document_id']), None)
        revision = current['revision'] if current else None
        RunbookStore.check_document(existing, document['document_id'], len(rows), revision)
        return {'document_id': document['document_id'], 'source_name': rows[0].get('source_name'),
            'expected_revision': revision, 'replaces_existing': current is not None,
            'preview_digest': digest(binding, rows, content_hash, revision), 'chunks': len(rows),
            'embedding_requests': (len(rows) + 7) // 8, 'chunk_size': config.chunk_size,
            'chunk_overlap': config.chunk_overlap, 'strategy': document['source_format'],
            'samples': [{'index': i + 1, 'characters': len(row['text']), 'text': row['text']}
                        for i, row in enumerate(rows[:5])], 'samples_truncated': len(rows) > 5}
    return await call(plan)


@router.post('/upload', openapi_extra=FILE_BODY)
async def upload(request: Request, filename: str = Query(min_length=1, max_length=180),
                 category: Literal['resource', 'dependency', 'configuration'] = Query(),
                 preview_digest: str = Query(pattern='^[a-f0-9]{64}$'),
                 expected_revision: str = Query(default='', pattern='^([a-f0-9]{64})?$'),
                 embedding_authorized: bool = Query(default=False), binding=Depends(binding_for)):
    if not embedding_authorized:
        raise HTTPException(422, '请先确认本次导入会调用向量服务并可能产生费用。')
    document, content_hash = await document_from(request, filename, category)
    def execute():
        if not _uploads.acquire(blocking=False):
            raise HTTPException(429, '同时导入的任务过多，请稍后再试。', headers={'Retry-After': '5'})
        try:
            _, rows = prepare(binding, [document])
            revision = expected_revision or None
            if digest(binding, rows, content_hash, revision) != preview_digest:
                raise HTTPException(409, '文件、分类或服务配置已变化，请重新预览。')
            context = RunContext(limits=Limits(model_calls=25, reserve_model_calls=0, reserve_seconds=0))
            with activate(context):
                result = upload_document(binding, document, revision)
            return {**result, 'source_name': rows[0].get('source_name'), 'usage': context.summary()}
        finally:
            _uploads.release()
    return await call(execute)
