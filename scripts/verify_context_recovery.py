"""Replay frozen failure cases on current code; optional labeled reference fault.

This is an engineering replay, not an independent quality or accuracy benchmark.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from agents.contracts import DiagnosisDraft
from agents.models import build_roles
from api.auth import Principal
from config.incident import IncidentConfig
from evals.common import write_json
from evidence.render import render
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from scripts.run_frozen_review import FrozenReviewProvider


class ReferenceFault:
    def __init__(self, model):
        self.model, self.calls, self.injected = model, 0, False

    def invoke(self, messages):
        response = self.model.invoke(messages)
        self.calls += 1
        if self.calls == 1:
            try:
                data = json.loads(response.content)
                refs = data['findings'][0]['refs']
                if refs:
                    refs[0]['reference_id'] = 'REF_' + '0' * 24
                    self.injected = True
                    return response.model_copy(update={'content': json.dumps(data, ensure_ascii=False)})
            except (ValueError, KeyError, IndexError, TypeError):
                pass
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--cases', nargs='+', choices=('fr_001', 'fr_002', 'fr_003'),
                        default=['fr_001', 'fr_002', 'fr_003'])
    parser.add_argument('--force-first-reference-error', action='store_true')
    args = parser.parse_args()
    if len(args.cases) != len(set(args.cases)):
        parser.error('Duplicate cases')
    lock = json.loads((ROOT / 'evals/freeze_review/freeze.json').read_text(encoding='utf-8'))
    for relative, expected in lock['files'].items():
        if relative.startswith(('fixtures/', 'evals/')):
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    config, limits, source = IncidentConfig.from_file(), Limits.from_env(), implementation_version()
    assert config.model == lock['model'] and config.extra_body == lock['transport_extra_body']
    assert asdict(limits) == lock['limits']
    plan = {'source_version': source, 'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'model': config.model, 'limits': asdict(limits), 'cases': args.cases, 'attempts_per_case': 1,
        'maximum_model_calls': len(args.cases) * limits.model_calls, 'estimated_cost': None,
        'read_only_synthetic': True, 'forced_reference_fault': args.force_first_reference_error,
        'criteria_sent_to_models': False, 'independent_quality_benchmark': False,
        'fixture_baseline': 'examples/showcase/frozen-review-runs.json'}
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return
    directory = ROOT / 'output/context-recovery' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    summaries = []
    for case in args.cases:
        assert implementation_version() == source
        print(json.dumps({'starting': case, 'directory': str(directory)}, ensure_ascii=False), flush=True)
        models = build_roles(config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body)
        fault = ReferenceFault(models['diagnosis']) if args.force_first_reference_error else None
        if fault:
            models['diagnosis'] = fault
        report = capture(case, 'multi', lambda flow: models, Principal('synthetic_demo', 'cli_reader'), limits,
                         provider_factory=FrozenReviewProvider, blind_review=False)
        report.update(source_version=source, model=config.model, execution_mode='live',
                      reference_fault_injected=bool(fault and fault.injected))
        path = directory / f'{case}.json'
        write_json(path, report)
        assert implementation_version() == source
        DiagnosisDraft.model_validate_json(json.dumps(report['output'], ensure_ascii=False))
        path.with_suffix('.md').write_text(render(report), encoding='utf-8')
        row = {'case_id': case, 'status': report['status'], 'review_status': report['review_status'],
            'report_file': path.relative_to(ROOT).as_posix(), 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'reference_fault_injected': report['reference_fault_injected'], 'run_summary': report['run_summary'],
            'unreviewed_observations': len(report.get('unreviewed_observations', [])),
            'correction_inputs': [e for e in report['events'] if e['type'] == 'correction_input_rebuilt'],
            'budget_rejections': [e for e in report['events'] if e['type'] == 'budget_rejected'],
            'validation_failures': [{k: e.get(k) for k in ('role', 'reason', 'details')}
                                     for e in report['events'] if e['type'] == 'validation_failure']}
        summaries.append(row)
        write_json(directory / 'summary.json', summaries)
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
