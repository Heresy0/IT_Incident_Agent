"""Execute eight distinct local mechanisms and publish only measured facts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from evals.rca_cases import _package
from evals.rca_contracts import SCHEMA_VERSION, load_dataset, read_json, sha_file

VERSION = 'rca-independent-local-mechanisms-v1'
CASE_IDS = tuple(f'rca_independent_{n:03}' for n in range(1, 9))
SOURCE_NAMES = ('evals/rca_independent_experiments_a.py',
    'evals/rca_independent_experiments_b.py', 'evals/rca_independent_cases.py',
    'scripts/reproduce_rca_independent.py')
ROOT = Path(__file__).resolve().parents[1]


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8'))


def execute_experiments(folder, repeats=3):
    from evals.rca_independent_experiments_a import run_experiments as run_a
    from evals.rca_independent_experiments_b import run_experiments as run_b
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('Use a new empty execution directory')
    folder.mkdir(parents=True, exist_ok=True)
    sources = {name: sha_file(ROOT / name) for name in SOURCE_NAMES}
    started = datetime.now(timezone.utc).isoformat()
    results = run_a(repeats=repeats) + run_b(repeats=repeats)
    if tuple(r['case_id'] for r in results) != CASE_IDS or len({r['family'] for r in results}) != 8:
        raise ValueError('Eight distinct declared mechanism families are required')
    entries = []
    for result in results:
        checks = []
        for item in result['checks']:
            checks.append({'check': item.get('check', item.get('name')), 'passed': item['passed'],
                'observed': deepcopy(item.get('observed', {k: v for k, v in item.items() if k not in {'name', 'check'}}))})
        raw = {**deepcopy(result), 'schema_version': 'rca-program-experiments-v1',
            'origin': 'local_controlled_program_execution', 'source_sha256': sources,
            'human_verified': False, 'is_gold': False, 'human_review_status': 'pending',
            'causal_truth_validated': False, 'checks': checks,
            'status': 'passed' if checks and all(c['passed'] is True for c in checks) else 'failed'}
        name = result['case_id'] + '.json'
        _write(folder / name, raw)
        entries.append({'case_id': result['case_id'], 'path': name,
            'sha256': sha_file(folder / name), 'status': raw['status']})
    unchanged = all(sha_file(ROOT / p) == h for p, h in sources.items())
    manifest = {'schema_version': 'rca-program-experiments-v1', 'origin': 'local_controlled_program_execution',
        'started_at': started, 'completed_at': datetime.now(timezone.utc).isoformat(),
        'repeats': repeats, 'source_sha256': sources, 'source_unchanged_during_run': unchanged,
        'status': 'passed' if unchanged and all(e['status'] == 'passed' for e in entries) else 'failed',
        'human_verified': False, 'is_gold': False, 'human_review_status': 'pending', 'model_calls': 0,
        'original_incident_reproduced': False, 'business_application_imported': False, 'cases': entries}
    for name in sources:
        target = folder / 'source' / Path(name).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    _write(folder / 'manifest.json', manifest)
    return manifest


def publish_dataset(experiment_manifest, folder):
    experiment_manifest, folder = Path(experiment_manifest).resolve(), Path(folder)
    manifest = read_json(experiment_manifest)
    if manifest.get('status') != 'passed' or manifest.get('source_unchanged_during_run') is not True:
        raise ValueError('Only successful source-bound executions may be published')
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('Use a new empty dataset version directory')
    if tuple(e['case_id'] for e in manifest['cases']) != CASE_IDS:
        raise ValueError('Unexpected mechanism coverage')
    machine = folder / 'machine'
    machine.mkdir(parents=True, exist_ok=True)
    (machine / 'manifest.json').write_bytes(experiment_manifest.read_bytes())
    snapshots = []
    for name, digest in manifest['source_sha256'].items():
        source = experiment_manifest.parent / 'source' / Path(name).name
        if sha_file(source) != digest:
            raise ValueError('Executed source snapshot changed')
        relative = 'machine/source/' + Path(name).name
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        snapshots.append({'path': relative, 'sha256': digest, 'kind': 'executed_program_source'})
    entries = []
    for index, entry in enumerate(manifest['cases'], 1):
        raw_path = experiment_manifest.parent / entry['path']
        if sha_file(raw_path) != entry['sha256']:
            raise ValueError('Execution artifact changed')
        result = read_json(raw_path)
        (machine / entry['path']).write_bytes(raw_path.read_bytes())
        scope = {'service': f'component_{100 + index}', 'environment': 'staging',
            'service_version': 'local-program-v1', 'start': manifest['started_at'],
            'end': (datetime.fromisoformat(manifest['completed_at']) + timedelta(seconds=1)).isoformat(),
            'allowed_dependencies': []}
        world = {'synthetic': True,
            'observation_origin': 'Executed local program; staging is an authorization label, not a staging deployment.',
            'ticket': {'incident_id': f'independent_observation_{index:03}',
                'title': 'Local request observation investigation',
                'symptoms': 'A local request returned an error or an unexpected result during the observed execution. Investigate using available sources.',
                'scope': scope}, 'metrics': [], 'logs': [], 'changes': [], 'owners': [], 'runbooks': [], 'incidents': []}
        for row in result['public_rows']:
            text = json.dumps(row['message'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(text) > 1100:
                raise ValueError('Measured observation exceeds prespecified public snippet allowance: ' + entry['case_id'])
            world['logs'].append({'id': row['observation_id'], 'timestamp': manifest['completed_at'],
                'timestamp_basis': 'execution_capture_completion_not_individual_request_time',
                'service': scope['service'], 'environment': scope['environment'], 'data_version': VERSION,
                'visibility': 'public_synthetic', 'category': row['category'], 'level': 'INFO',
                'error_code': 'MEASURED_OBSERVATION', 'message': text})
        reference = result['reference']
        first = world['logs'][0]
        controls = [{'control_id': 'control_01',
            'statement': f"观测记录{first['id']}的类别为{first['category']}。",
            'level': 'mechanism', 'selectors': [[first['id'], 'category']], 'expected_relation': 'supported'}]
        case = _package(entry['case_id'], world, split='candidate_acceptance', family=result['family'],
            group='independent_program_' + result['family'], identifiability='root_identifiable',
            conditions=[reference['causal_condition']], explanation=reference['reference_explanation'],
            essential_ids=reference['key_observation_ids'],
            unknowns=['Production behavior, original incidents and organizational causes are outside this local experiment.'],
            controls=controls, notes=['One controlled case per distinct executable mechanism; repetitions are correlated.'])
        truth = case['truth']
        truth['origin'] = 'executed_synthetic_experiment'
        truth['reference_mechanism'] = deepcopy(reference)
        truth['boundaries']['forbidden_assertions'] = reference['forbidden_assertions']
        truth['acquisition_eligibility'] = {'required_evidence_available': True,
            'initial_ticket_contains_required_evidence': False,
            'rationale': 'All key rows are query-reachable; neutral ticket contains symptoms and scope only, without observations or interventions.'}
        truth['machine_evidence'] = {'status': 'executed_not_human_adjudicated',
            'experiment_manifest': {'path': 'machine/manifest.json', 'sha256': sha_file(experiment_manifest)},
            'artifacts': [{'path': 'machine/' + entry['path'], 'sha256': entry['sha256'], 'kind': 'raw_execution'}, *deepcopy(snapshots)],
            'case_parameters': deepcopy(result['parameters']), 'original_authored_world_confirmed': False,
            'human_verified': False, 'is_gold': False, 'limitations': reference['limitations']}
        truth['provenance']['source_origin'] = 'executed_distinct_local_mechanism'
        truth['provenance']['reference_basis'] = 'Actual controlled execution; final scored judgments are separately attributed to AI.'
        value = {k: case[k] for k in ('case_id', 'split', 'scenario_family', 'independence_group')}
        for label, field in (('public', 'public'), ('truth', 'truth'), ('diagnostics', 'public_diagnostics')):
            relative = ('private' if label == 'truth' else label) + '/' + case['case_id'] + '.json'
            _write(folder / relative, case[field])
            value[label + '_path'], value[label + '_sha256'] = relative, sha_file(folder / relative)
        entries.append(value)
    _write(folder / 'manifest.json', {'schema_version': SCHEMA_VERSION, 'dataset_version': VERSION,
        'holdout_sealed': False, 'human_verified': False, 'is_gold': False,
        'origin': 'executed_synthetic_experiment', 'truth_status': 'pending_human_review',
        'formal_acceptance_enabled': False, 'calibration_cases': 0, 'candidate_cases': 8,
        'intended_candidate_composition': {'root_identifiable': 8, 'mechanism_only': 0, 'not_identifiable': 0},
        'independence_policy': 'Eight executable failure mechanisms, one local controlled case each; three repetitions do not create more independent cases. Shared harness and authored observability remain limitations.',
        'cases': entries})
    return load_dataset(folder / 'manifest.json')
