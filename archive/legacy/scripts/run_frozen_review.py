"""One run per frozen synthetic case; review criteria never enter model inputs."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from agents.contracts import DiagnosisDraft
from agents.models import build_roles
from config.incident import IncidentConfig
from evals.common import write_json
from evals.independent.provider import IndependentProvider
from evidence.render import render
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture


class FrozenReviewProvider(IndependentProvider):
    name = 'frozen_review_synthetic_fixture'
    data_root = ROOT / 'fixtures/incident_freeze_review'


def verify_freeze(lock, config, limits):
    assert implementation_version() == lock['source_version'], 'Frozen application changed'
    assert config.model == lock['model'], 'Frozen model changed'
    assert config.extra_body == lock['transport_extra_body'], 'Frozen vendor parameters changed'
    assert asdict(limits) == lock['limits'], 'Frozen budgets changed'
    for relative, expected in lock['files'].items():
        path = ROOT / relative
        assert path.resolve().is_relative_to(ROOT.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--transport-retry-of', type=Path)
    args = parser.parse_args()
    lock = json.loads((ROOT / 'evals/freeze_review/freeze.json').read_text(encoding='utf-8'))
    config, limits = IncidentConfig.from_file(), Limits.from_env()
    verify_freeze(lock, config, limits)
    principal = Principal('synthetic_demo', 'cli_reader')
    catalog = json.loads((FrozenReviewProvider.data_root / 'catalog.json').read_text(encoding='utf-8'))
    if args.cases:
        known = {row['case_id'] for row in catalog}
        if len(args.cases) != len(set(args.cases)) or not set(args.cases) <= known:
            parser.error('Unknown or duplicate cases')
        catalog = [row for row in catalog if row['case_id'] in args.cases]
    retry = None
    if args.transport_retry_of:
        retry = args.transport_retry_of.resolve()
        if not retry.is_relative_to((ROOT / 'output/frozen-review').resolve()):
            parser.error('Retry must reference a recorded frozen-review batch')
        for row in catalog:
            previous = json.loads((retry / f"{row['case_id']}.json").read_text(encoding='utf-8'))
            summary = previous['run_summary']
            if (summary['termination_reason'] not in {'NETWORK_ERROR', 'TIMEOUT'}
                    or summary['token_usage']['known_calls'] != 0
                    or previous['source_version'] != lock['source_version']
                    or previous['model'] != lock['model']):
                parser.error('Only transport failures before any model reply may be retried')
    for row in catalog:
        provider = FrozenReviewProvider(row['case_id'], principal)
        provider.authorize(principal, provider.ticket().scope)
    plan = {**{k: lock[k] for k in ('source_version', 'implementation_commit', 'model', 'limits')},
        'cases': [row['case_id'] for row in catalog], 'attempts_per_case': 1,
        'maximum_model_calls': len(catalog) * limits.model_calls,
        'read_only_synthetic': True, 'estimated_cost': None,
        'criteria_sent_to_models': False, 'review_mode': 'direct',
        'freeze_sha256': hashlib.sha256((ROOT / 'evals/freeze_review/freeze.json').read_bytes()).hexdigest()}
    if retry:
        plan['transport_retry_of'] = retry.relative_to(ROOT).as_posix()
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return
    directory = ROOT / 'output/frozen-review' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    write_json(directory / 'freeze.json', lock)
    rows = []
    for case in plan['cases']:
        verify_freeze(lock, config, limits)
        print(json.dumps({'starting': case, 'directory': str(directory)}, ensure_ascii=False), flush=True)
        models = build_roles(config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body)
        report = capture(case, 'multi', lambda flow: models, principal, limits,
            provider_factory=FrozenReviewProvider, blind_review=False)
        report.update(source_version=lock['source_version'], model=config.model, execution_mode='live')
        path = directory / f'{case}.json'
        write_json(path, report)
        valid = True
        try:
            DiagnosisDraft.model_validate_json(json.dumps(report['output'], ensure_ascii=False))
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
        except Exception as exc:
            valid = False
            path.with_suffix('.validation.txt').write_text(type(exc).__name__, encoding='utf-8')
        verify_freeze(lock, config, limits)
        row = {'case_id': case, 'status': report['status'], 'review_status': report['review_status'],
            'contract_valid': valid, 'run_summary': report['run_summary'],
            'report_file': path.relative_to(ROOT).as_posix(),
            'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'repair_counts': report.get('repair_counts'), 'rework_rounds': report.get('rework_rounds'),
            'validation_failures': [{k: e.get(k) for k in ('role', 'reason', 'details')}
                for e in report['events'] if e['type'] == 'validation_failure']}
        rows.append(row)
        write_json(directory / 'summary.json', rows)
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
