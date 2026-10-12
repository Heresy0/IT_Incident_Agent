"""One bounded run per frozen fresh case; no reference criteria are sent to models."""
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
from agents.models import build_roles
from agents.contracts import DiagnosisDraft
from config.incident import IncidentConfig
from evals.common import write_json
from evals.independent.provider import IndependentProvider
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from evidence.render import render


class FreshProvider(IndependentProvider):
    name = 'fresh_validation_synthetic_fixture'
    data_root = ROOT / 'fixtures/incident_fresh_validation'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--budget-profile', choices=('baseline', 'expanded'), default='baseline')
    args = parser.parse_args()
    limits = (Limits(model_calls=32, tool_calls=48, seconds=300, reserve_model_calls=3, reserve_seconds=20)
              if args.budget_profile == 'expanded' else
              Limits(model_calls=16, tool_calls=8, seconds=180, reserve_model_calls=3, reserve_seconds=20))
    lock = json.loads((ROOT / 'evals/fresh_validation/freeze.json').read_text(encoding='utf-8'))
    assert implementation_version() == lock['source_version'], 'Application changed after freeze'
    for relative, expected in lock['files'].items():
        path = ROOT / relative
        assert path.resolve().is_relative_to(ROOT.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    catalog = json.loads((FreshProvider.data_root / 'catalog.json').read_text(encoding='utf-8'))
    principal = Principal('synthetic_demo', 'cli_reader')
    for row in catalog:
        provider = FreshProvider(row['case_id'], principal)
        provider.authorize(principal, provider.ticket().scope)
    if not args.execute_live:
        print(json.dumps({'validated_cases':len(catalog),'source':lock['source_version'],'model_calls':0,
                         'budget_profile':args.budget_profile,'limits':asdict(limits)}))
        return
    config = IncidentConfig.from_file()
    assert config.model == lock['model']
    directory = ROOT / 'output/fresh-validation' / str(uuid4())
    directory.mkdir(parents=True)
    write_json(directory / 'freeze.json', lock)
    write_json(directory / 'execution-plan.json', {'budget_profile':args.budget_profile,
        'limits':asdict(limits),'source_version':lock['source_version'],'model':config.model,
        'attempts_per_case':1,'baseline_reports':'examples/showcase/fresh-validation-runs.json'})
    summaries=[]
    for row in catalog:
        case = row['case_id']
        print(json.dumps({'starting':case,'directory':str(directory)}),flush=True)
        assert implementation_version() == lock['source_version']
        models = build_roles(config.api_key,config.model)
        report = capture(case,'multi',lambda flow:models,principal,limits,provider_factory=FreshProvider)
        report.update(model=config.model,execution_mode='live',source_version=lock['source_version'],
                      budget_profile=args.budget_profile)
        path=directory/f'{case}.json'
        write_json(path,report)
        valid=True
        try:
            DiagnosisDraft.model_validate_json(json.dumps(report['output'],ensure_ascii=False))
            path.with_suffix('.md').write_text(render(report),encoding='utf-8')
        except Exception as exc:
            valid=False
            path.with_suffix('.validation.txt').write_text(str(exc),encoding='utf-8')
        assert implementation_version() == lock['source_version']
        summary=dict(case_id=case,status=report['status'],review_status=report['review_status'],
            report_file=path.relative_to(ROOT).as_posix(),report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            contract_valid=valid,run_summary=report['run_summary'],
            validation_failures=[{k:e.get(k) for k in ('role','reason','details')} for e in report['events'] if e['type']=='validation_failure'])
        summaries.append(summary)
        write_json(directory/'summary.json',summaries)
        print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
