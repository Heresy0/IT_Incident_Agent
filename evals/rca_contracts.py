"""Data-only RCA evaluation contracts; no application or model imports.

Authored references are not human gold. Public observations, optional diagnostic
tasks and private references have separate files and independently bound hashes.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


SCHEMA_VERSION = 'rca-evaluation-v1'
DEFAULT_MANIFEST = Path(__file__).parent / 'datasets' / 'rca_v1' / 'manifest.json'
IDENTIFIABILITY = ('root_identifiable', 'mechanism_only', 'not_identifiable')
PUBLIC_KEYS = {'schema_version', 'world', 'capability_metadata'}
FORBIDDEN_PUBLIC_KEYS = {'truth', 'private', 'expected_relation', 'expected_answer',
    'causal_conditions', 'causal_edges', 'evidence_criteria', 'identifiability',
    'oracle_queries', 'truth_status', 'human_verified', 'is_gold', 'key_observation_ids',
    'machine_evidence'}


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


canonical_sha256 = canonical_sha


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _no_private_keys(value, location='public'):
    if isinstance(value, dict):
        leaked = FORBIDDEN_PUBLIC_KEYS.intersection(value)
        _require(not leaked, f'Private reference field in {location}: {sorted(leaked)}')
        for key, item in value.items():
            _no_private_keys(item, location + '.' + key)
    elif isinstance(value, list):
        for item in value:
            _no_private_keys(item, location)


def validate_public(public):
    _require(isinstance(public, dict) and set(public) == PUBLIC_KEYS,
        'Public case must contain only schema_version, world, capability_metadata')
    _require(public['schema_version'] == SCHEMA_VERSION, 'Wrong public schema version')
    _no_private_keys(public)
    world = public['world']
    _require(world.get('synthetic') is True, 'Only explicitly synthetic observations supported')
    _require(not {'cause', 'gold', 'correct_answer'}.intersection(world), 'Answer in public world')
    _require(isinstance(public['capability_metadata'], dict), 'Invalid capabilities')
    scope = world['ticket']['scope']
    from datetime import datetime
    start, end = datetime.fromisoformat(scope['start']), datetime.fromisoformat(scope['end'])
    _require(start.tzinfo is not None and end.tzinfo is not None and start < end, 'Invalid scoped time window')
    identifiers = []
    for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
        _require(isinstance(world.get(kind), list), 'Missing observation channel: ' + kind)
        identifiers.extend(row['id'] for row in world[kind])
    _require(len(identifiers) == len(set(identifiers)), 'Duplicate source observation IDs')
    return True


def validate_public_diagnostics(value):
    _require(set(value) == {'schema_version', 'source_queries', 'fixed_controls'},
        'Diagnostic inputs must be a separate public task, without reference fields')
    _require(value['schema_version'] == SCHEMA_VERSION, 'Wrong diagnostic schema version')
    _no_private_keys(value, 'public_diagnostics')
    for query in value['source_queries']:
        _require(set(query) == {'tool', 'args'} and isinstance(query['args'], dict),
            'Public source query must contain tool and args only')
    for control in value['fixed_controls']:
        _require(set(control) == {'control_id', 'statement', 'level', 'selectors'},
            'Public fixed control contains unexpected fields')
        _require(control['level'] in {'mechanism', 'trigger', 'alternative'} and
            isinstance(control['statement'], str) and 1 <= len(control['statement']) <= 500,
            'Fixed control must fit the native claim contract')
    return True


def _inside(folder, relative):
    path = (folder / relative).resolve()
    _require(path.is_relative_to(folder.resolve()) and path.suffix == '.json', 'Unsafe dataset path')
    return path


def _machine_artifacts(truth, folder):
    """Check retained program evidence for internal consistency, not authenticity.

    Hashes bind the declared bytes and execution inputs. They are not signatures
    or proof that an untrusted author really ran a program, and never establish
    human adjudication or original-incident truth.
    """
    machine = truth.get('machine_evidence')
    _require(isinstance(machine, dict) and machine.get('status') ==
        'executed_not_human_adjudicated', 'Executed case requires explicit machine evidence')
    _require(isinstance(machine.get('artifacts'), list) and bool(machine['artifacts']),
        'Executed case requires nonempty raw artifacts')
    manifest = machine.get('experiment_manifest')
    _require(isinstance(manifest, dict), 'Missing execution manifest')
    files, artifacts = {}, []
    for item in [manifest, *machine['artifacts']]:
        _require(isinstance(item, dict) and isinstance(item.get('path'), str)
            and isinstance(item.get('sha256'), str), 'Invalid machine artifact binding')
        relative = item['path']
        _require(not Path(relative).is_absolute() and '..' not in Path(relative).parts,
            'Unsafe machine artifact path')
        path = (folder / relative).resolve()
        _require(path.is_relative_to(folder.resolve()) and path.is_file(),
            'Missing or unsafe machine artifact')
        _require(sha_file(path) == item['sha256'], 'Machine artifact hash mismatch')
        _require(relative not in files or files[relative] == item['sha256'],
            'Conflicting machine artifact binding')
        files[relative] = item['sha256']
        artifacts.append((item, path))
    _require(isinstance(machine.get('case_parameters'), dict), 'Missing actual case parameters')

    def pending(value, label):
        _require(value.get('human_verified') is False and value.get('is_gold') is False,
            label + ' must not claim human review or gold')
        _require(value.get('human_review_status', 'pending') == 'pending',
            label + ' must remain pending human review')

    def no_nested_human_claims(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'human_verified', 'is_gold'}:
                    _require(item is False, 'Machine evidence must not claim human review or gold')
                elif key == 'human_review_status':
                    _require(item == 'pending', 'Machine evidence must remain pending human review')
                no_nested_human_claims(item)
        elif isinstance(value, list):
            for item in value:
                no_nested_human_claims(item)

    pending(machine, 'Machine evidence')
    no_nested_human_claims(machine)
    _require(machine.get('original_authored_world_confirmed') is False,
        'Program evidence must not confirm the original authored world')
    execution = read_json(artifacts[0][1])
    _require(isinstance(execution, dict) and execution.get('schema_version') ==
        'rca-program-experiments-v1', 'Invalid experiment manifest schema')
    pending(execution, 'Experiment manifest')
    no_nested_human_claims(execution)
    _require(execution.get('status') == 'passed' and type(execution.get('model_calls')) is int
        and execution.get('model_calls') == 0
        and execution.get('source_unchanged_during_run') is True,
        'Experiment manifest execution gate failed')
    _require(execution.get('origin') == 'local_controlled_program_execution'
        and execution.get('original_incident_reproduced') is False
        and execution.get('business_application_imported') is False,
        'Experiment manifest origin or incident boundary invalid')
    entries = execution.get('cases')
    _require(isinstance(entries, list) and entries and all(isinstance(entry, dict)
        and isinstance(entry.get('case_id'), str) for entry in entries),
        'Invalid experiment manifest cases')
    _require(len({entry['case_id'] for entry in entries}) == len(entries),
        'Duplicate experiment manifest case')
    selected = [entry for entry in entries if entry['case_id'] == truth['case_id']]
    _require(len(selected) == 1, 'Current case missing from experiment manifest')
    entry = selected[0]
    _require(entry.get('status') == 'passed' and isinstance(entry.get('path'), str)
        and isinstance(entry.get('sha256'), str), 'Invalid current experiment case entry')
    relative = Path(entry['path'])
    _require(not relative.is_absolute() and '..' not in relative.parts,
        'Unsafe experiment manifest raw path')
    raw_path = (artifacts[0][1].parent / relative).resolve()
    _require(raw_path.is_relative_to(artifacts[0][1].parent),
        'Unsafe experiment manifest raw path')
    raw_bindings = [(item, path) for item, path in artifacts[1:]
        if item.get('kind') == 'raw_execution' and path == raw_path
        and item['sha256'] == entry['sha256']]
    _require(len(raw_bindings) == 1, 'Current raw execution is not bound to manifest')
    raw = read_json(raw_path)
    _require(isinstance(raw, dict) and raw.get('schema_version') ==
        'rca-program-experiments-v1', 'Invalid raw execution schema')
    pending(raw, 'Raw execution')
    no_nested_human_claims(raw)
    _require(raw.get('case_id') == truth['case_id'] and raw.get('status') == 'passed'
        and raw.get('origin') == 'local_controlled_program_execution'
        and raw.get('causal_truth_validated') is False,
        'Raw execution identity or status invalid')
    checks = raw.get('checks')
    _require(isinstance(checks, list) and checks and all(isinstance(item, dict)
        and isinstance(item.get('check'), str) and item['check']
        and item.get('passed') is True and 'observed' in item for item in checks),
        'Raw execution checks are missing or failed')
    _require(isinstance(raw.get('trials'), list) and raw['trials'],
        'Raw execution trials are missing')
    _require(raw.get('parameters') == machine['case_parameters'],
        'Machine case parameters differ from raw execution')
    sources = execution.get('source_sha256')
    _require(isinstance(sources, dict) and sources and all(
        isinstance(name, str) and name and isinstance(digest, str)
        for name, digest in sources.items()), 'Missing executed program source hashes')
    _require(raw.get('source_sha256') == sources, 'Raw execution source hashes differ from manifest')
    snapshots = [(item, path) for item, path in artifacts[1:]
        if item.get('kind') == 'executed_program_source']
    _require(len(snapshots) == len(sources), 'Executed source snapshot coverage mismatch')
    names = set()
    for original, digest in sources.items():
        # Original source locations can be Windows paths in a copied POSIX tree.
        name = original.replace('\\', '/').rsplit('/', 1)[-1]
        _require(name and name not in names, 'Ambiguous executed source name')
        names.add(name)
        matched = [(item, path) for item, path in snapshots
            if path.name == name and item['sha256'] == digest]
        _require(len(matched) == 1, 'Executed program source snapshot does not match manifest')
    return files


def validate_dataset(dataset):
    manifest, cases = dataset['manifest'], dataset['cases']
    _require(manifest.get('schema_version') == SCHEMA_VERSION, 'Wrong manifest schema')
    _require(manifest.get('holdout_sealed') is False, 'Initial authored candidate set is not sealed')
    _require(manifest.get('human_verified') is False and manifest.get('is_gold') is False,
        'Provisional dataset must not claim human gold')
    _require(cases and len({case['case_id'] for case in cases}) == len(cases), 'Duplicate or empty cases')
    groups, bound_artifacts = {}, {}
    for case in cases:
        validate_public(case['public'])
        validate_public_diagnostics(case['public_diagnostics'])
        truth = case['truth']
        for key in ('case_id', 'split', 'independence_group'):
            _require(case[key] == truth[key], 'Truth identity mismatch: ' + key)
        _require(truth['schema_version'] == SCHEMA_VERSION, 'Wrong truth schema')
        _require(truth['identifiability'] in IDENTIFIABILITY, 'Invalid identifiability')
        _require(truth['origin'] in {'authored_synthetic_provisional',
            'executed_synthetic_experiment'}, 'Unsupported truth origin')
        _require(truth['truth_status'] == 'pending_human_review', 'Initial truth must remain pending')
        _require(truth['holdout_sealed'] is False, 'No sealed holdout before independent curation')
        provenance = truth['provenance']
        _require(provenance['human_verified'] is False and provenance['is_gold'] is False,
            'No invented human review or gold label')
        _require(not provenance['reviewers'] and not provenance['reproduction_evidence'],
            'Initial authored truth has no independently verified reproduction')
        _require(provenance.get('causal_confirmation_status') == 'not_confirmed' and
            provenance.get('human_review_status') == 'pending', 'Provisional confirmation status must remain explicit')
        if truth['origin'] == 'executed_synthetic_experiment':
            _require(bool(dataset.get('manifest_path')), 'Execution artifacts require a dataset location')
            extra = _machine_artifacts(truth, Path(dataset['manifest_path']).parent)
            for name, digest in extra.items():
                _require(name not in bound_artifacts or bound_artifacts[name] == digest,
                    'Conflicting shared machine artifact')
                bound_artifacts[name] = digest
        else:
            _require('machine_evidence' not in truth,
                'Authored case must not masquerade as an executed experiment')
        previous = groups.setdefault(case['independence_group'], case['split'])
        _require(previous == case['split'], 'Related variants cross calibration/acceptance split')
        known = {row['id'] for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents')
                 for row in case['public']['world'][kind]}
        for criterion in truth['evidence_criteria']:
            for alternative in criterion['acceptable_observation_sets']:
                _require(set(alternative) <= known, 'Evidence criterion refers to absent source')
        for query in truth['oracle_queries']:
            _require(set(query['expected_observation_ids']) <= known, 'Oracle refers to absent observation')
        public_queries = case['public_diagnostics']['source_queries']
        _require(public_queries == [{'tool': q['tool'], 'args': q['args']} for q in truth['oracle_queries']],
            'Public diagnostic task differs from private reachability witness')
        private_controls = [{k: v for k, v in c.items() if k != 'expected_relation'}
                            for c in truth['fixed_controls']]
        _require(private_controls == case['public_diagnostics']['fixed_controls'], 'Control binding mismatch')
        if truth['identifiability'] == 'root_identifiable':
            _require(bool(truth['causal_conditions']) and bool(truth['causal_edges']) and
                bool(truth['evidence_criteria']), 'Identifiable candidate lacks a proposed causal reference')
    dataset['bound_artifacts'] = bound_artifacts
    return {'valid': True, 'cases': len(cases), 'independence_groups': len(groups),
        'human_gold_cases': 0, 'formal_accuracy_available': False,
        'machine_experiment_cases': sum(c['truth']['origin'] == 'executed_synthetic_experiment' for c in cases),
        'status': 'candidate_data_pending_human_review'}


def load_dataset(manifest_path=DEFAULT_MANIFEST):
    path = Path(manifest_path).resolve()
    manifest = read_json(path)
    cases = []
    for entry in manifest['cases']:
        case = deepcopy(entry)
        for label, field in (('public', 'public'), ('truth', 'truth'), ('diagnostics', 'public_diagnostics')):
            artifact = _inside(path.parent, entry[label + '_path'])
            _require(sha_file(artifact) == entry[label + '_sha256'], f'{label} file hash mismatch')
            case[field] = read_json(artifact)
        cases.append(case)
    result = {'manifest': manifest, 'cases': cases, 'fingerprint': canonical_sha(manifest),
        'manifest_path': str(path), 'manifest_sha256': sha_file(path)}
    result['validation'] = validate_dataset(result)
    return result
