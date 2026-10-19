"""Publish measured experiment observations without inventing human gold.

The three local-program experiments are new scenarios with explicit parameters.
They do not validate the old authored worlds. Private execution traces remain
machine attachments and are never copied into a model-visible observation.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

from evals.rca_cases import _package
from evals.rca_contracts import SCHEMA_VERSION, load_dataset, read_json, sha_file


VERSION = 'rca-executed-program-observations-v1'
REPRODUCED_MANIFEST = Path(__file__).parent / 'datasets' / 'rca_reproduced_v1' / 'manifest.json'
CASE_IDS = ('rca_candidate_001', 'rca_candidate_019', 'rca_candidate_025')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _load_experiments(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    if manifest.get('schema_version') != 'rca-program-experiments-v1':
        raise ValueError('Wrong local program experiment schema')
    if (manifest.get('status') != 'passed' or manifest.get('human_verified') is not False
            or manifest.get('is_gold') is not False or manifest.get('model_calls') != 0
            or manifest.get('source_unchanged_during_run') is not True):
        raise ValueError('Only passed, source-bound machine executions are publishable; human gold is not implied')
    if {entry['case_id'] for entry in manifest['cases']} != set(CASE_IDS) or len(manifest['cases']) != 3:
        raise ValueError('Representative dataset requires all three measured experiments')
    experiments = {}
    for entry in manifest['cases']:
        relative = Path(entry['path'])
        path = (manifest_path.parent / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(manifest_path.parent):
            raise ValueError('Experiment path must remain inside its evidence directory')
        if sha_file(path) != entry['sha256']:
            raise ValueError('Experiment evidence hash mismatch')
        value = read_json(path)
        if (value.get('case_id') != entry['case_id'] or value.get('status') != 'passed'
                or not value.get('checks') or not all(item['passed'] for item in value['checks'])
                or value.get('human_verified') is not False or value.get('is_gold') is not False
                or value.get('human_review_status') != 'pending'):
            raise ValueError('Experiment execution or identity gate failed')
        if value.get('source_sha256') != manifest['source_sha256']:
            raise ValueError('Case execution source differs from experiment manifest')
        experiments[entry['case_id']] = {'result': value, 'path': path, 'entry': entry}
    source_files = []
    names = set()
    for original_path, digest in manifest['source_sha256'].items():
        path = Path(original_path)
        name = original_path.replace('\\', '/').rsplit('/', 1)[-1]
        # A copied evidence set is self-contained; prefer its source snapshot.
        snapshot = manifest_path.parent / 'source' / name
        source = snapshot if snapshot.is_file() else path
        if name in names or not source.is_file() or sha_file(source) != digest:
            raise ValueError('Executed program source hash mismatch')
        names.add(name)
        source_files.append({'path': source, 'original_path': original_path,
                             'sha256': digest, 'relative_path': 'machine/source/' + name})
    if not source_files:
        raise ValueError('Executed program source snapshot is required')
    return manifest, experiments, source_files


def _world(manifest, case_id, symptom):
    suffix = case_id.rsplit('_', 1)[-1]
    completed = datetime.fromisoformat(manifest['completed_at'])
    return {
        'synthetic': True,
        'observation_origin': 'Measured local Python program; staging below is the required native authorization scope label, not a staging deployment.',
        'ticket': {'incident_id': 'eval_program_observation_' + suffix,
            'title': 'Measured local program observation exercise', 'symptoms': symptom,
            'scope': {'service': 'component_' + suffix, 'environment': 'staging',
                'service_version': 'local-program-v1', 'start': manifest['started_at'],
                'end': (completed + timedelta(seconds=1)).isoformat(), 'allowed_dependencies': []}},
        'metrics': [], 'logs': [], 'changes': [], 'owners': [], 'runbooks': [], 'incidents': [],
    }


def _append(world, manifest, row_id, category, value):
    scope = world['ticket']['scope']
    world['logs'].append({
        'id': row_id, 'timestamp': manifest['completed_at'],
        'timestamp_basis': 'experiment_manifest_capture_completion_not_request_event_time',
        'service': scope['service'], 'environment': scope['environment'],
        'visibility': 'public_synthetic', 'data_version': VERSION,
        'category': category, 'level': 'INFO', 'error_code': 'MEASURED_OBSERVATION',
        'message': _json(value),
    })


def _pool_rows(manifest, result):
    world = _world(manifest, result['case_id'], 'Some local pooled-resource acquisitions reach their configured deadline under the measured burst load.')
    _append(world, manifest, 'row_01', 'configuration', {
        'program': 'local Python semaphore resource model; no SQL or remote database',
        'parameters': result['parameters'],
        'burst_definition': '30 measured request tasks released together at a barrier',
        'smooth_definition': '30 requests scheduled evenly over one second',
    })
    columns = ('repeat', 'phase', 'capacity', 'distribution', 'request_count',
               'acquired', 'acquisition_timeouts', 'max_observed_in_use', 'resources_released')
    for row_id, phases in (('row_02', {'before', 'capacity_restored'}),
                           ('row_03', {'capacity_reduced'}),
                           ('row_04', {'smooth_negative_control'})):
        selected = [trial for trial in result['trials'] if trial['phase'] in phases]
        _append(world, manifest, row_id, 'resource', {
            'trials': [{key: trial[key] for key in columns} for trial in selected],
        })
    return world, {
        'identifiability': 'root_identifiable', 'family': 'executed_burst_capacity',
        'conditions': ['在30请求同时到达、资源持有参数20ms、获取期限100ms的本地实验中，池容量从40缩为4。'],
        'explanation': '在该受控本地模型的相同突发负载下，容量40→4→40仅中间条件出现获取超时；这不确认旧版平稳流量场景，也不证明任何数据库故障。',
        'unknowns': ['Original authored incident, SQL behavior, real external service load and organizational why are not established.'],
        'essential_ids': ['row_01', 'row_02', 'row_03'],
        'statement': '在本地实验中，相同30请求同时到达的突发负载下，池容量40→4→40时仅容量4的试次出现资源获取超时。',
        'selectors': [['row_01', 'message'], ['row_02', 'message'], ['row_03', 'message']],
    }


def _mutex_rows(manifest, result):
    world = _world(manifest, result['case_id'], 'A measured local request times out acquiring a mutex; the owner stack is not in the available observation.')
    public = deepcopy(result['trials'][0]['public_observation'])
    if not all(trial['public_observation'] == public for trial in result['trials']):
        raise ValueError('Mutex public projections are not observationally equivalent')
    _append(world, manifest, 'row_01', 'resource', {key: public[key] for key in
        ('request_id', 'acquired', 'outcome', 'configured_deadline_ms', 'owner_stack_captured')})
    _append(world, manifest, 'row_02', 'dependency', {key: public[key] for key in
        ('request_id', 'outbound_attempted')})
    return world, {
        'identifiability': 'mechanism_only', 'family': 'executed_mutex_boundary',
        'conditions': ['request=q31在本地互斥锁获取处到达80ms配置期限。'],
        'explanation': '本次可见观测支持本地互斥锁获取超时，不能区分私有执行轨迹中的不同持锁程序路径。',
        'unknowns': ['The holder operation, stack, release condition and reason for holding the mutex are withheld from the available observation.'],
        'essential_ids': ['row_01', 'row_02'],
        'statement': 'request=q31在80ms配置期限到达时未取得本地互斥锁，并且该请求未发起出站调用。',
        'selectors': [['row_01', 'message'], ['row_02', 'message']],
    }


def _gateway_rows(manifest, result):
    world = _world(manifest, result['case_id'], 'The measured local gateway client receives a generic502 response while detailed upstream diagnostics are absent from the available observation.')
    public = deepcopy(result['trials'][0]['public_observation'])
    if not all(trial['public_observation'] == public for trial in result['trials']):
        raise ValueError('Gateway public projections are not observationally equivalent')
    _append(world, manifest, 'row_01', 'dependency', {key: public[key] for key in
        ('request_id', 'http_status', 'body', 'original_exception_captured',
         'downstream_status_captured', 'wire_trace_captured')})
    _append(world, manifest, 'row_02', 'configuration', {key: public[key] for key in
        ('request_id', 'change_source_status')})
    world['source_errors'] = {'get_recent_changes': public['change_source_status']}
    return world, {
        'identifiability': 'not_identifiable', 'family': 'executed_gateway_boundary',
        'conditions': [],
        'explanation': '仅凭公开的通用502响应及缺失诊断字段，不能区分实际执行的不同上游协议失败；私有解析异常未提供给模型。',
        'unknowns': ['The upstream failure kind and original parser exception are outside the available observation.'],
        'essential_ids': ['row_01', 'row_02'],
        'statement': 'request=q37收到HTTP 502且原始异常未被采集，并且变更记录源状态为SOURCE_NOT_CONFIGURED。',
        'selectors': [['row_01', 'message'], ['row_02', 'message']],
    }


def build_reproduced_cases(experiment_manifest):
    manifest, experiments, source_files = _load_experiments(experiment_manifest)
    projectors = dict(zip(CASE_IDS, (_pool_rows, _mutex_rows, _gateway_rows), strict=True))
    cases = []
    source_artifacts = [{'path': item['relative_path'], 'sha256': item['sha256'],
                         'kind': 'executed_program_source'} for item in source_files]
    for case_id in CASE_IDS:
        execution = experiments[case_id]
        result = execution['result']
        world, reference = projectors[case_id](manifest, result)
        controls = [{
            'control_id': 'control_01', 'statement': reference['statement'], 'level': 'mechanism',
            'selectors': reference['selectors'], 'expected_relation': 'supported',
        }, {
            'control_id': 'control_02', 'statement': '该故障观测时间窗内，远端数据库进程发生崩溃。',
            'level': 'trigger', 'selectors': [[reference['essential_ids'][0], 'message']],
            'expected_relation': 'insufficient',
        }]
        case = _package(case_id, world, split='candidate_acceptance',
            family=reference['family'], group='executed_template_' + reference['family'],
            identifiability=reference['identifiability'], conditions=reference['conditions'],
            explanation=reference['explanation'], essential_ids=reference['essential_ids'],
            unknowns=reference['unknowns'], controls=controls,
            notes=['Measured new local-program scenario; not a reproduction of the original authored v1 incident.'])
        if case_id == 'rca_candidate_025':
            query = {'tool': 'get_recent_changes', 'args': {key: world['ticket']['scope'][key]
                                                         for key in ('start', 'end')}}
            case['public_diagnostics']['source_queries'].append(deepcopy(query))
            oracle = {**query, 'expected_observation_ids': [], 'expected_error': 'SOURCE_NOT_CONFIGURED'}
            case['truth']['oracle_queries'].append(deepcopy(oracle))
            case['truth']['sufficient_evidence_queries'].append(deepcopy(oracle))
        for metadata in case['public']['capability_metadata']['metrics'].values():
            metadata['source'] = VERSION
        truth = case['truth']
        truth['origin'] = 'executed_synthetic_experiment'
        truth['provenance'].update(
            source_origin='executed_local_program_observations',
            reference_basis='Passed local-program measurements with raw execution and source snapshots; causal interpretation remains pending human review.',
            required_next_step='Review the measured evidence, information boundary and causal interpretation; machine execution is not human gold.',
        )
        truth['machine_evidence'] = {
            'status': 'executed_not_human_adjudicated',
            'experiment_manifest': {'path': 'machine/manifest.json', 'sha256': sha_file(experiment_manifest)},
            'artifacts': [{'path': 'machine/' + execution['entry']['path'],
                           'sha256': execution['entry']['sha256'], 'kind': 'raw_execution'}] + deepcopy(source_artifacts),
            'case_parameters': deepcopy(result['parameters']),
            'original_authored_world_confirmed': False,
            'human_verified': False, 'is_gold': False,
            'limitations': deepcopy(result['limitations']),
        }
        cases.append(case)
    return {'cases': cases, 'experiment_manifest': manifest,
            'executions': experiments, 'source_files': source_files}


def write_dataset(experiment_manifest, folder=REPRODUCED_MANIFEST.parent):
    """Freeze a new self-contained observation dataset from retained executions."""
    built = build_reproduced_cases(experiment_manifest)
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('Dataset output is not empty; use a new version directory')
    for name in ('public', 'private', 'diagnostics', 'machine/source'):
        (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / 'machine/manifest.json').write_bytes(Path(experiment_manifest).read_bytes())
    for execution in built['executions'].values():
        (folder / 'machine' / execution['entry']['path']).write_bytes(execution['path'].read_bytes())
    for item in built['source_files']:
        (folder / item['relative_path']).write_bytes(item['path'].read_bytes())
    entries = []
    for case in built['cases']:
        entry = {key: case[key] for key in ('case_id', 'split', 'scenario_family', 'independence_group')}
        for label, field in (('public', 'public'), ('truth', 'truth'), ('diagnostics', 'public_diagnostics')):
            relative = ('private' if label == 'truth' else label) + '/' + case['case_id'] + '.json'
            path = folder / relative
            path.write_text(json.dumps(case[field], ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8', newline='\n')
            entry[label + '_path'], entry[label + '_sha256'] = relative, sha_file(path)
        entries.append(entry)
    manifest = {
        'schema_version': SCHEMA_VERSION, 'dataset_version': VERSION,
        'holdout_sealed': False, 'human_verified': False, 'is_gold': False,
        'origin': 'executed_synthetic_experiment', 'truth_status': 'pending_human_review',
        'formal_acceptance_enabled': False, 'calibration_cases': 0, 'candidate_cases': 3,
        'intended_candidate_composition': {'root_identifiable': 1, 'mechanism_only': 1, 'not_identifiable': 1},
        'independence_policy': 'Three development mechanisms measured locally; repetitions are not independent incidents.',
        'sealing_requirements': ['Independent human review of measured causal scope and information boundaries',
                                 'New, independently curated holdout cases before a production acceptance claim'],
        'version_boundary': {
            'original_dataset': 'rca-authored-candidate-dataset-v1',
            'original_cases_modified': False, 'original_worlds_confirmed': False,
            'pool_new_parameters': 'Explicit simultaneous burst and 100ms acquisition deadline; smooth30/s negative control retained.',
            'comparison_limit': 'Same case identifiers describe representative mechanism families; observation worlds and parameters differ from v1 and must not be pooled as exact paired reruns.',
        },
        'cases': entries,
    }
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                        encoding='utf-8', newline='\n')
    return load_dataset(folder / 'manifest.json')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiments', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=REPRODUCED_MANIFEST.parent)
    args = parser.parse_args()
    print(_json(write_dataset(args.experiments, args.output)['validation']))
