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
    'oracle_queries', 'truth_status', 'human_verified', 'is_gold', 'key_observation_ids'}


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


def validate_dataset(dataset):
    manifest, cases = dataset['manifest'], dataset['cases']
    _require(manifest.get('schema_version') == SCHEMA_VERSION, 'Wrong manifest schema')
    _require(manifest.get('holdout_sealed') is False, 'Initial authored candidate set is not sealed')
    _require(manifest.get('human_verified') is False and manifest.get('is_gold') is False,
        'Provisional dataset must not claim human gold')
    _require(cases and len({case['case_id'] for case in cases}) == len(cases), 'Duplicate or empty cases')
    groups = {}
    for case in cases:
        validate_public(case['public'])
        validate_public_diagnostics(case['public_diagnostics'])
        truth = case['truth']
        for key in ('case_id', 'split', 'independence_group'):
            _require(case[key] == truth[key], 'Truth identity mismatch: ' + key)
        _require(truth['schema_version'] == SCHEMA_VERSION, 'Wrong truth schema')
        _require(truth['identifiability'] in IDENTIFIABILITY, 'Invalid identifiability')
        _require(truth['origin'] == 'authored_synthetic_provisional', 'Unsupported truth origin')
        _require(truth['truth_status'] == 'pending_human_review', 'Initial truth must remain pending')
        _require(truth['holdout_sealed'] is False, 'No sealed holdout before independent curation')
        provenance = truth['provenance']
        _require(provenance['human_verified'] is False and provenance['is_gold'] is False,
            'No invented human review or gold label')
        _require(not provenance['reviewers'] and not provenance['reproduction_evidence'],
            'Initial authored truth has no independently verified reproduction')
        _require(provenance.get('causal_confirmation_status') == 'not_confirmed' and
            provenance.get('human_review_status') == 'pending', 'Provisional confirmation status must remain explicit')
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
    return {'valid': True, 'cases': len(cases), 'independence_groups': len(groups),
        'human_gold_cases': 0, 'formal_accuracy_available': False,
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
