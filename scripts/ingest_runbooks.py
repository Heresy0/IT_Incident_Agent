"""Plan by default; --execute transactionally replaces scoped pgvector runbooks."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'), str(ROOT)]
from config.incident import load_environment
from knowledge.ingest import prepare, build_index
from knowledge.database import scope_id
from providers.registry import ServiceRegistry
from providers.fixtures import ProviderError
from runtime.context import RunContext, Limits, activate, ExecutionError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services-file', type=Path, required=True)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--environment', choices=('staging','production'), required=True)
    parser.add_argument('--documents', type=Path, required=True, help='JSON object with documents array')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    load_environment()
    context = RunContext(limits=Limits(model_calls=30, tool_calls=1, reserve_model_calls=2, seconds=300))
    try:
        registry = ServiceRegistry.from_file(args.services_file)
        matches = [b for b in registry._bindings if (b.tenant_id,b.service,b.environment) ==
                   (args.tenant,args.service,args.environment)]
        if len(matches) != 1 or not matches[0].observations.get('runbook_rag'):
            raise ValueError('RAG_SERVICE_NOT_REGISTERED')
        binding = matches[0]
        raw = json.loads(args.documents.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or set(raw) != {'documents'} or not isinstance(raw['documents'],list):
            raise ValueError('INVALID_DOCUMENT_MANIFEST')
        config, rows = prepare(binding, raw['documents'])
        if not args.execute:
            print(json.dumps({'plan_only':True, 'scope_id':scope_id(binding,config), 'backend':'pgvector',
                'documents':len(raw['documents']), 'chunks':len(rows), 'embedding_requests':(len(rows)+7)//8,
                'network_checked':False, 'existing_index_modified':False,
                'execute_replaces_service_index':True},ensure_ascii=False,indent=2))
            return 0
        with activate(context):
            result = build_index(binding, raw['documents'])
        print(json.dumps({**result,'run_summary':context.summary()},ensure_ascii=False,indent=2))
        return 0
    except (ProviderError, ExecutionError) as exc:
        print(json.dumps({'status':'failed','code':exc.code,'run_summary':context.summary()},ensure_ascii=False))
        return 2
    except Exception:
        # Configuration/text/API details may contain private data. Do not dump them.
        print(json.dumps({'status':'failed','code':'INVALID_INGEST_CONFIGURATION'},ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
