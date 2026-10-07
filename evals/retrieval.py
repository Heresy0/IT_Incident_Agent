"""Small live retrieval/memory benchmark using ONLY public synthetic fixture data."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT))
from evals.run import digest, fixtures, write_json

QUERIES = [
    ('r01','A 如何私有部署和维护？',['A']),
    ('r02','比较 A 与 B 的 Python SDK 和 HTTP API 接入方式。',['A','B']),
    ('r03','B 的数据保留多久？',['B']),
    ('r04','C 的本地部署依赖和可导入文件格式是什么？',['C']),
    ('r05','C 当前是否支持 OCR？',['C']),
    ('r06','团队的研发人数、验证窗口和预算限制是什么？',['TEAM']),
    ('r07','C 每分钟 60 个请求能说明并发吞吐吗？',['C']),
    ('r08','C 的旧测试分支有 OCR 预览，当前版本能用吗？',['C','D']),
]


def ranking(expected, actual):
    # A document counts once even if multiple chunks occur in the top-k.
    labels = actual
    expected = set(expected)
    if not expected:
        raise ValueError('Ranking gold must not be empty')
    ranks = [i + 1 for i, label in enumerate(labels) if label in expected]
    return {'recall_at_k':len(expected.intersection(labels))/len(expected),
            'reciprocal_rank':1/min(ranks) if ranks else 0,
            'hit_at_k':int(bool(ranks))}


def aggregate(rows):
    return {key:sum(row['metrics'][key] for row in rows)/len(rows) if rows else None
            for key in ('recall_at_k','reciprocal_rank','hit_at_k')}


class ObservedVector:
    """Observe real adapter failures so fallback cannot masquerade as vector success."""
    def __init__(self, delegate):
        self.delegate, self.errors, self.reads = delegate, [], []

    def add_documents(self, *args, **kwargs):
        try:
            return self.delegate.add_documents(*args, **kwargs)
        except Exception as exc:
            self.errors.append({'stage':'index','type':type(exc).__name__})
            raise

    def similarity_search(self, *args, **kwargs):
        try:
            result = self.delegate.similarity_search(*args, **kwargs)
            self.reads.append(result)
            return result
        except Exception as exc:
            self.errors.append({'stage':'search','type':type(exc).__name__})
            raise


def run(config, test_dsn, output):
    from langchain_core.documents import Document
    from pymilvus import Collection, connections, utility
    from mult_agents.rag.core import RAGConfig, RAGSystem
    from mult_agents.memory.scoped import ScopedMemoryManager
    from mult_agents.harness.runtime import Limits, RunContext, activate
    if not urlparse(test_dsn).path.removeprefix('/').endswith('_test'):
        raise ValueError('Refusing non-test database')
    if output.exists():
        raise ValueError('Refusing to overwrite evaluation artifact')
    prefix = 'research_eval_' + uuid4().hex[:16]
    knowledge_collection, memory_base = prefix+'_knowledge', prefix+'_memory'
    memory_collection = memory_base + '_scoped_v2'
    alias = prefix
    manager = None
    collections_owned = False
    database_owned = False
    started = perf_counter()
    artifact = {'schema_version':1, 'suite':'live_retrieval_memory', 'evaluation_id':prefix,
                'created_at':datetime.now(timezone.utc).isoformat(), 'embedding_model':'text-embedding-v1',
                'fixture_version':digest(ROOT/'evals/fixtures/solutions.md'), 'complete':False,
                'scope':'Five public synthetic knowledge sections; synthetic user notes; temporary collections and isolated test database. No web or chat model.',
                'knowledge':[], 'memory':[], 'safety':{}, 'cleanup':{}, 'errors':[]}
    context = RunContext(limits=Limits(model_calls=0,tool_calls=40,web_calls=0,seconds=180))
    tenants = [prefix+'_t1',prefix+'_t2']
    write_json(output,artifact)
    try:
        connections.connect(alias=alias,host=config.milvus_host,port=config.milvus_port,timeout=10)
        if any(utility.has_collection(c,using=alias) for c in (knowledge_collection,memory_collection)):
            raise ValueError('Temporary collection collision; refusing reuse')
        collections_owned = True
        _, docs = fixtures()
        with activate(context):
            rag = RAGSystem(config.api_key,RAGConfig(milvus_host=config.milvus_host,milvus_port=config.milvus_port,
                                                    collection_name=knowledge_collection))
            rag.vectorstore.add_documents([Document(page_content=d['snippet'], metadata={'source':d['doc_id'],'label':label})
                                           for label,d in docs.items()],timeout=10)
            for cid, query, expected in QUERIES:
                hits = rag.search_records(query,k=3)
                labels = [h['metadata']['label'] for h in hits]
                artifact['knowledge'].append({'id':cid,'query':query,'expected':expected,'returned':labels,
                                              'k':3,'metrics':ranking(expected,labels)})
                write_json(output,artifact)
            manager = ScopedMemoryManager(postgres_dsn=test_dsn,embedding_api_key=config.api_key,
                                          milvus_host=config.milvus_host,milvus_port=config.milvus_port,
                                          milvus_collection=memory_base,enable_milvus=True)
            if manager.vector is None:
                raise RuntimeError('Real memory vector adapter unavailable')
            with manager.pool.connection() as conn:
                for table in ('assistant_profiles','assistant_sessions','assistant_memories'):
                    if conn.execute(f'SELECT 1 FROM {table} WHERE tenant_id=ANY(%s) LIMIT 1',(tenants,)).fetchone():
                        raise ValueError('Temporary tenant collision; refusing reuse')
            database_owned = True
            observed = ObservedVector(manager.vector)
            manager.vector = observed
            notes = {
                'python':'团队主要用 Python，希望通过 SDK 接入应用。',
                'private':'内部资料不能上传公有云，必须私有部署。',
                'poc':'首轮验证窗口为十四天，研发团队有五人。'}
            ids = {label:manager.add_note(tenants[0],'u1',note) for label,note in notes.items()}
            foreign = [manager.add_note(tenants[0],'u2',notes['private']),
                       manager.add_note(tenants[1],'u1',notes['python'])]
            indexed = {r['memory_id'] for r in Collection(memory_collection,using=alias).query(
                expr='memory_id != ""',output_fields=['memory_id'],timeout=10)}
            all_ids = set(ids.values()).union(foreign)
            artifact['safety']['all_notes_indexed'] = all_ids.issubset(indexed)
            labels_by_id = {mid:label for label,mid in ids.items()}
            for cid, query, expected in [('m01','我们偏好用哪种编程语言和接口方式？',['python']),
                                         ('m02','可以把内部数据放到公有云吗？',['private']),
                                         ('m03','验证周期和团队规模是什么？',['poc'])]:
                before = len(observed.reads)
                hits = manager.search(tenants[0],'u1',query,'demo',limit=2)
                returned = [labels_by_id.get(h['id'],'FOREIGN_OR_UNKNOWN') for h in hits]
                raw = observed.reads[-1] if len(observed.reads)>before else []
                vector_ids = [d.metadata.get('memory_id') for d in raw]
                artifact['memory'].append({'id':cid,'query':query,'expected':expected,'returned':returned,'k':2,
                                          'vector_returned':[labels_by_id.get(mid,'FOREIGN_OR_UNKNOWN') for mid in vector_ids],
                                          'vector_query_observed':len(observed.reads)>before,
                                          'foreign_vector_hits':sum(mid not in ids.values() for mid in vector_ids),
                                          'metrics':ranking(expected,returned)})
            # Leave expired/deleted vectors in the index deliberately; canonical PG reads must exclude them.
            with manager.pool.connection() as conn:
                conn.execute('UPDATE assistant_memories SET expires_at=NOW()-INTERVAL \'1 minute\' WHERE id=%s AND tenant_id=%s',
                             (ids['python'],tenants[0]))
            manager.clear(tenants[0],'u1',target='entry',memory_id=ids['poc'])
            hits = manager.search(tenants[0],'u1','Python SDK 十四天 五人 验证','demo',limit=6)
            returned_ids = {h['id'] for h in hits}
            artifact['safety'].update(expired_hits=int(ids['python'] in returned_ids),
                                      deleted_hits=int(ids['poc'] in returned_ids),
                                      foreign_hits=len(returned_ids.intersection(foreign)),
                                      foreign_vector_hits=sum(r['foreign_vector_hits'] for r in artifact['memory']),
                                      adapter_errors=observed.errors)
            if observed.errors or not artifact['safety']['all_notes_indexed']:
                raise RuntimeError('Vector evaluation incomplete; fallback results cannot establish vector quality')
        artifact['complete'] = True
    except Exception as exc:
        artifact['errors'].append({'type':type(exc).__name__})
    finally:
        if manager and database_owned:
            try:
                with manager.pool.connection() as conn:
                    for table in ('assistant_memories','assistant_sessions','assistant_profiles'):
                        conn.execute(f'DELETE FROM {table} WHERE tenant_id=ANY(%s)',(tenants,))
                artifact['cleanup']['test_tenants'] = True
            except Exception as exc:
                artifact['cleanup']['database_error_type'] = type(exc).__name__
        if manager:
            manager.close()
        for collection in (knowledge_collection,memory_collection):
            try:
                if not collection.startswith(prefix+'_'):
                    raise ValueError('Cleanup target outside current evaluation')
                if collections_owned and utility.has_collection(collection,using=alias):
                    utility.drop_collection(collection,using=alias,timeout=10)
                artifact['cleanup'][collection] = True
            except Exception as exc:
                artifact['cleanup'][collection] = type(exc).__name__
        try:
            connections.disconnect(alias)
        except Exception as exc:
            artifact['cleanup']['disconnect_error_type'] = type(exc).__name__
        artifact['duration_ms'] = int((perf_counter()-started)*1000)
        artifact['summary'] = context.summary()
        artifact['knowledge_metrics'] = aggregate(artifact['knowledge'])
        artifact['memory_metrics'] = aggregate(artifact['memory'])
        safety = artifact['safety']
        artifact['passed'] = (artifact['complete'] and not artifact['errors']
            and artifact['knowledge_metrics']['recall_at_k'] >= .9 and artifact['memory_metrics']['recall_at_k'] >= .9
            and all(safety.get(key)==0 for key in ('expired_hits','deleted_hits','foreign_hits','foreign_vector_hits'))
            and all(value is True for value in artifact['cleanup'].values()))
        write_json(output,artifact)
        report = ['# 真实检索与记忆小型基准', '', artifact['scope'], '',
                  f"Embedding：`{artifact['embedding_model']}`；完整：{artifact['complete']}；门禁：{'passed' if artifact['passed'] else 'failed'}。",
                  f"知识检索（8 查询、top-3）：{artifact['knowledge_metrics']}。",
                  f"记忆召回（3 查询、top-2）：{artifact['memory_metrics']}。",
                  f"隔离与失效检查：{artifact['safety']}。",f"耗时：{artifact['duration_ms']} ms；清理：{artifact['cleanup']}。",'',
                  'Recall 为预标注文档集合的命中比例；MRR 取首个相关结果的倒数排名。语料只有 5 段，结果仅用于验证检索链路和回归，不代表大规模 RAG 效果。',
                  '记忆检索使用真实 Embedding 和 Milvus，逐次记录向量候选与 PG 权威记录输出。记录索引/搜索错误，不能用关键词降级冒充向量召回。',
                  '初始门禁要求两类 Recall 均 ≥90%、无外用户/失效记录、无适配器错误且清理成功；不评估记忆注入后的回答质量。']
        output.with_suffix('.report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    return artifact


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-embeddings',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not args.live_embeddings:
        parser.error('--live-embeddings required; consumes Embedding API credits')
    from local_run import load_local_env
    load_local_env()
    dsn=os.getenv('RESEARCH_TEST_POSTGRES_DSN','')
    if not urlparse(dsn).path.removeprefix('/').endswith('_test'):
        parser.error('RESEARCH_TEST_POSTGRES_DSN must identify a dedicated *_test database')
    from mult_agents.config import AppConfig
    output=args.output or ROOT/'output/evaluations'/uuid4().hex/'retrieval.json'
    if output.exists():
        parser.error('Output exists; choose a new artifact file')
    artifact=run(AppConfig.from_file(),dsn,output)
    print(json.dumps({k:artifact[k] for k in ('complete','passed','knowledge_metrics','memory_metrics','safety','errors')},ensure_ascii=False,indent=2))
    if not artifact['passed']: raise SystemExit(2)


if __name__=='__main__': main()
