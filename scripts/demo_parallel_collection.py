"""Free actual-graph concurrency demo; scripted responses and artificial latency."""
import hashlib
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from agents.scripted import scripted_models
from api.auth import Principal
from evals.common import implementation_version, write_json
from evals.incident.comparison import score_report
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from workflow.coordinator import collaborate
from workflow.graph import WORKFLOW_VERSION


class DelayedModel:
    def __init__(self, model, delay=0.25):
        self.model, self.delay, self.first = model, delay, True

    def bind_tools(self, tools):
        return DelayedModel(self.model.bind_tools(tools), self.delay)

    def invoke(self, messages):
        if self.first:
            self.first = False
            time.sleep(self.delay)
        return self.model.invoke(messages)


def task_intervals(report):
    events = report['events']
    ends = {e['span_id']: e['timestamp'] for e in events
            if e['type'] == 'call_end' and e.get('kind') == 'task'}
    return [{'role': e['name'], 'start': e['timestamp'], 'end': ends[e['span_id']]}
            for e in events if e['type'] == 'call_start' and e.get('kind') == 'task']


def main():
    folder = ROOT / 'output' / 'parallel-collection' / str(uuid4())
    rows = []
    principal = Principal('synthetic_demo', 'cli_reader')
    limits = Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20)
    for parallel in (False, True):
        models = scripted_models()
        for role in ('investigation', 'knowledge'):
            models[role] = DelayedModel(models[role])
        started = time.perf_counter()
        report = collaborate(models, FixtureProvider('case_003', principal), principal,
                             limits=limits, parallel_collection=parallel)
        duration_ms = round((time.perf_counter() - started) * 1000)
        report['workflow'] = 'multi'
        report['execution_mode'] = 'scripted_control_only'
        checks = score_report(report, {'necessary_checks': []})['mechanical_checks']
        intervals = task_intervals(report)
        initial = intervals[:2]
        overlap = max(r['start'] for r in initial) < min(r['end'] for r in initial)
        passed = (all(checks.values()) and report['status'] == 'completed'
            and report['review_status'] == 'passed' and report['rework_rounds'] == 1
            and report['run_summary']['model_calls'] == 12
            and report['run_summary']['tool_calls'] == 5 and overlap == parallel)
        name = 'parallel' if parallel else 'serial'
        path = folder / (name + '.json')
        write_json(path, report)
        rows.append({'mode': name, 'duration_ms': duration_ms, 'passed': passed,
            'initial_collection_ms': round((max(r['end'] for r in initial)
                                           - min(r['start'] for r in initial)) * 1000),
            'status': report['status'], 'review_status': report['review_status'],
            'model_calls': report['run_summary']['model_calls'],
            'tool_calls': report['run_summary']['tool_calls'], 'rework_rounds': report['rework_rounds'],
            'initial_roles_overlapped': overlap, 'task_intervals': intervals,
            'mechanical_checks': checks, 'report_file': path.name,
            'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    summary = {'case_id': 'case_003', 'implementation_sha256_prefix': implementation_version(),
        'workflow_version': WORKFLOW_VERSION,
        'execution_mode': 'scripted_control_only', 'paid_model_calls': 0,
        'latency_source': 'artificial 250ms on first invocation of each collection task',
        'quality_status': 'excluded_scripted', 'real_provider_performance': 'not_measured',
        'limits': limits.__dict__, 'runs': rows, 'passed': all(r['passed'] for r in rows)}
    write_json(folder / 'summary.json', summary)
    print(json.dumps({'output': str(folder), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
