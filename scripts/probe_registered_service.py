"""Read registered observations through the harness; no models, DB writes or repairs."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

from api.auth import get_current_principal
from agents.investigation import validate_output
from evidence.contracts import Finding, InvestigationOutput
from fastapi.security import HTTPAuthorizationCredentials
from providers.registry import ServiceRegistry
from runtime.context import Limits, RunContext, activate
from tools.executor import ToolExecutor


def probe(registry, principal, service, environment='staging', minutes=60, *, now=None):
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError('WINDOW_INVALID')
    catalog = [row for row in registry.catalog(principal)
               if row['service'] == service and row['environment'] == environment]
    if len(catalog) != 1:
        raise ValueError('SERVICE_NOT_REGISTERED')
    end = now or datetime.now(timezone.utc)
    window = {'start': (end - timedelta(minutes=minutes)).isoformat(), 'end': end.isoformat()}
    snapshot = {'id': str(uuid4()), 'title': '登记服务只读联调', 'symptoms': '核对真实数据和证据，不做故障诊断',
                'service': service, 'environment': environment,
                'service_version': catalog[0]['service_version'], **window}
    provider = registry.provider(snapshot, principal)
    ticket = provider.ticket()
    context = RunContext(scope=ticket.scope, limits=Limits(model_calls=0, tool_calls=12,
                         reserve_model_calls=0, reserve_seconds=0))
    executor = ToolExecutor(provider, principal, ticket.scope, context)
    knowledge = ToolExecutor(provider, principal, ticket.scope, context, role='knowledge')
    checks, gaps, metrics = [], [], []

    def check(tool, args, name, ex=executor):
        result = ex.execute(tool, args)
        valid = 0
        try:
            for item in result.evidence:
                field = {'get_service_metrics': 'value', 'get_service_logs': 'message',
                         'get_service_owner': 'team', 'search_runbooks': 'text'}[tool]
                refs = [ex.references[option.reference_id] for option in item.reference_options
                        if option.field_path == field]
                if len(refs) != 1 or field in {'message', 'team', 'text'} and not item.payload.get(field):
                    raise ValueError('SUBSTANTIVE_REFERENCE_MISSING')
                validate_output(InvestigationOutput(findings=[Finding(statement='只读联调引用校验', refs=refs)]),
                                ex.evidence, ('observation', 'runbook'))
                valid += 1
        except (ValueError, TypeError):
            gaps.append(f'{name}: EVIDENCE_VALIDATION_FAILED')
        info = {'check': name, 'tool': tool, 'status': result.status, 'records': len(result.evidence),
                'truncated': result.truncated, 'validated_references': valid,
                'evidence_ids': [item.evidence_id for item in result.evidence],
                'error_code': result.error.code if result.error else None}
        if 'start' in args:
            info['window'] = {key: args[key] for key in ('start', 'end')}
        if tool == 'get_service_metrics':
            latest = {}
            for item in result.evidence:
                row = item.payload
                if row['metric'] not in latest:
                    latest[row['metric']] = {key: row[key] for key in ('metric', 'value', 'unit', 'timestamp')}
            metrics.extend(latest.values())
            missing = set(args['metrics']) - set(latest)
            if missing:
                gaps.append('metrics: NO_SAMPLES_FOR_' + ','.join(sorted(missing)))
        elif tool == 'get_service_logs':
            info['levels'] = dict(Counter(item.payload['level'] for item in result.evidence))
            info['source_counts'] = dict(getattr(provider, 'last_log_stats', {}))
        if result.status != 'ok' or result.truncated:
            gaps.append(f'{name}: ' + (result.error.code if result.error else
                        'TRUNCATED' if result.truncated else 'NO_MATCHING_OBSERVATIONS'))
        checks.append(info)

    with activate(context):
        names = list(provider.binding.observations.get('metrics', {}))
        if names:
            # This is connectivity/format verification, not a complete incident history.
            # Small independent samples fit the existing evidence response-size limit.
            metric_window = {'start': (end - timedelta(minutes=min(minutes, 5))).isoformat(), 'end': end.isoformat()}
            for metric in names[:7]:
                check('get_service_metrics', {**metric_window, 'metrics': [metric], 'granularity': '5m'}, f'metrics/{metric}')
            if len(names) > 7:
                gaps.append('metrics: ONLY_FIRST_SEVEN_REGISTERED_METRICS_CHECKED')
        else:
            gaps.append('metrics: NO_REGISTERED_METRICS')
        categories = provider.binding.observations.get('log_queries', {})
        for category in ('resource', 'dependency', 'configuration'):
            if category in categories:
                check('get_service_logs', {**window, 'category': category}, f'logs/{category}')
        if not categories:
            gaps.append('logs: NO_REGISTERED_LOG_QUERIES')
        check('get_service_owner', {'alias': service}, 'owner')
        check('search_runbooks', {'query': service}, 'runbook', knowledge)
    return {'mode': 'registered_read_only_probe', 'service': service, 'environment': environment,
            'run_id': context.run_id,
            'window': window, 'status': 'partial' if gaps else 'passed', 'checks': checks,
            'latest_metrics': metrics, 'gaps': list(dict.fromkeys(gaps)), 'run_summary': context.summary(),
            'diagnosis_performed': False, 'repairs_performed': False, 'raw_logs_included': False}


def main():
    parser = argparse.ArgumentParser(description='已登记服务免费只读联调，不调用模型、不执行修复')
    parser.add_argument('--service', required=True)
    parser.add_argument('--environment', choices=('staging', 'production'), default='staging')
    parser.add_argument('--minutes', type=int, default=60)
    parser.add_argument('--services-file', type=Path)
    parser.add_argument('--credentials-file', type=Path, default=ROOT / '.demo-credentials.local.json')
    parser.add_argument('--user', help='选择本地凭据中的用户；省略时选择唯一 operator')
    parser.add_argument('--tenant')
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'registered-service-probe.json')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env.local', override=True)
    load_dotenv(ROOT / '.env', override=False)
    os.chdir(ROOT)
    if args.services_file:
        os.environ['INCIDENT_SERVICES_FILE'] = str(args.services_file.resolve())
    try:
        rows = json.loads(args.credentials_file.read_text(encoding='utf-8'))['principals']
        candidates = [row for row in rows if (row.get('user_id') == args.user if args.user
                      else row.get('role') == 'operator')
                      and (not args.tenant or row.get('tenant_id') == args.tenant)]
        if len(candidates) != 1:
            raise ValueError('CREDENTIAL_SELECTION_INVALID')
        # Resolve the token through the same server authentication, not caller-declared identity.
        principal = get_current_principal(HTTPAuthorizationCredentials(scheme='Bearer', credentials=candidates[0]['token']))
        report = probe(ServiceRegistry.from_environment(), principal, args.service, args.environment, args.minutes)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                          'message': '联调无法开始，请检查登记、身份和参数；未输出配置或凭据。'}, ensure_ascii=False))
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
