"""Register public-only wording pairs and reference ablations before model runs."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evals.rca_contracts import load_dataset

CASE_IDS = ('rca_candidate_001', 'rca_candidate_019', 'rca_candidate_025')
STATEMENTS = {
    'rca_candidate_001': '在这组局部资源池实验中，同步突发30个任务、资源持有参数20ms、获取期限100ms时，容量4出现获取超时，容量40的前后对照未出现超时；平滑30请求/秒、容量4的对照未出现超时。',
    'rca_candidate_019': '请求q31在配置的80ms期限内未取得本地互斥锁，且未发起网络调用；当前公开记录没有持锁者堆栈。',
    'rca_candidate_025': '请求q37收到HTTP502；当前公开记录未包含原始异常类型、下游状态和线路追踪，变更观测源未配置。',
}


def check_registered_measurements(case):
    """Reject stale task prose when a new experiment changes observed parameters."""
    rows = {row['id']: json.loads(row['message']) for row in case['public']['world']['logs']}
    case_id = case['case_id']
    if case_id == 'rca_candidate_001':
        parameters = rows['row_01']['parameters']
        valid = all(parameters.get(k) == v for k, v in {
            'requests_per_window': 30, 'resource_hold_ms': 20,
            'window_seconds': 1.0,
            'acquisition_timeout_ms': 100, 'pool_capacities': [40, 4, 40],
            'smooth_negative_control_capacity': 4}.items())
        for row_id, capacity, distribution, timed_out in (
                ('row_02', 40, 'burst', False), ('row_03', 4, 'burst', True),
                ('row_04', 4, 'smooth', False)):
            trials = rows[row_id]['trials']
            valid = valid and bool(trials) and all(
                trial['capacity'] == capacity and trial['distribution'] == distribution
                and trial['request_count'] == 30
                and (trial['acquisition_timeouts'] > 0) is timed_out for trial in trials)
    elif case_id == 'rca_candidate_019':
        valid = (rows['row_01']['request_id'] == 'q31' and
            rows['row_01']['configured_deadline_ms'] == 80 and
            rows['row_01']['acquired'] is False and rows['row_01']['owner_stack_captured'] is False
            and rows['row_02']['outbound_attempted'] is False)
    elif case_id == 'rca_candidate_025':
        valid = (rows['row_01']['request_id'] == 'q37' and rows['row_01']['http_status'] == 502
            and all(rows['row_01'][key] is False for key in
                ('original_exception_captured', 'downstream_status_captured', 'wire_trace_captured'))
            and rows['row_02']['change_source_status'] == 'SOURCE_NOT_CONFIGURED')
    else:
        valid = False
    if not valid:
        raise ValueError('Measured public parameters changed; register a new task wording version')


def prepare(old_manifest, new_manifest, reproduced_manifest, output):
    output = Path(output).resolve()
    old, new, reproduced = [load_dataset(p) for p in (old_manifest, new_manifest, reproduced_manifest)]
    versions = [{case['case_id']: case for case in dataset['cases']}
                for dataset in (old, new, reproduced)]
    tasks = []

    def add(case, dataset, group, comparison, condition, statement, level, selectors):
        public = Path(dataset['manifest_path']).parent / case['public_path']
        tasks.append({'task_id': group + '_' + condition, 'group_id': group,
            'case_id': case['case_id'], 'comparison_kind': comparison, 'condition': condition,
            'public_path': Path(os.path.relpath(public, output.parent)).as_posix(),
            'source_queries': deepcopy(case['public_diagnostics']['source_queries']),
            'statement': statement, 'level': level, 'selectors': deepcopy(selectors)})

    for case_id in CASE_IDS:
        left, right = versions[0][case_id], versions[1][case_id]
        if left['public_sha256'] != right['public_sha256']:
            raise ValueError('Wording pairs must preserve exact public bytes')
        if left['public_diagnostics']['source_queries'] != right['public_diagnostics']['source_queries']:
            raise ValueError('Wording pairs must preserve source reads')
        a, b = [next(c for c in row['public_diagnostics']['fixed_controls']
                     if c['control_id'] == 'control_02') for row in (left, right)]
        if a['selectors'] != b['selectors'] or a['level'] != b['level'] or a['statement'] == b['statement']:
            raise ValueError('Wording pair must change only its original sentence')
        group = 'legacy_' + case_id.rsplit('_', 1)[-1] + '_wording'
        add(left, old, group, 'wording_pair', 'wording_v1', a['statement'], a['level'], a['selectors'])
        add(right, new, group, 'wording_pair', 'wording_v2', b['statement'], b['level'], b['selectors'])

        measured = versions[2][case_id]
        check_registered_measurements(measured)
        group = 'reproduced_' + case_id.rsplit('_', 1)[-1] + '_refs'
        row_ids = [row['id'] for row in measured['public']['world']['logs']]
        full = [[row_id, 'message'] for row_id in row_ids]
        for condition, selectors in (('full_refs', full), ('no_refs', []),
                                     ('wrong_field', [[row_id, 'error_code'] for row_id in row_ids])):
            add(measured, reproduced, group, 'reference_ablation', condition,
                STATEMENTS[case_id], 'mechanism', selectors)
    value = {'schema_version': 'rca-integration-tasks-v1', 'tasks': tasks}
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if output.exists() and output.read_text(encoding='utf-8') != rendered:
        raise FileExistsError('Refusing to overwrite a different registered task set')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding='utf-8')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-dataset', type=Path, default=ROOT / 'evals/datasets/rca_v1/manifest.json')
    parser.add_argument('--new-dataset', type=Path, default=ROOT / 'evals/datasets/rca_v2/manifest.json')
    parser.add_argument('--reproduced-dataset', type=Path, default=ROOT / 'evals/datasets/rca_reproduced_v1/manifest.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'evals/datasets/rca_integration_v1/tasks.json')
    args = parser.parse_args()
    result = prepare(args.old_dataset, args.new_dataset, args.reproduced_dataset, args.output)
    print(json.dumps({'tasks': len(result['tasks']), 'groups': len({r['group_id'] for r in result['tasks']}),
        'model_calls': 0, 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
