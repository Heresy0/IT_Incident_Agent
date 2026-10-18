"""Version ambiguous fixed-review questions without rewriting past observations.

This is an evaluation-question revision, not an independently confirmed causal
dataset. The v1 public worlds, query schedules and causal references are kept;
candidate relation labels remain proposed labels awaiting human review.
"""
from copy import deepcopy
import json
from pathlib import Path

from evals.rca_contracts import DEFAULT_MANIFEST, load_dataset, sha_file


VERSION = 'rca-authored-candidate-dataset-v2-atomic-controls'
V2_MANIFEST = Path(__file__).parent / 'datasets' / 'rca_v2' / 'manifest.json'
SOURCE_FINGERPRINT = '51baa7d36a4ddc53e75ad87ce4884f57ee0d904568d14c40ac8d36d44923427b'
ATOMIC_UNKNOWN_STATEMENT = '该故障观测时间窗内，远端数据库进程发生崩溃。'

# A separate question tests contradiction using an explicit observed opposite.
# These are not extra observations, and they do not enter end-to-end model input.
DIRECT_COUNTEREVIDENCE = {
    'rca_candidate_001': {
        'statement': '06:03生效的连接池上限为40。',
        'selectors': [['row_01', 'message']],
        'counterevidence': 'applied pool_max=4; previous pool_max=40',
    },
    'rca_candidate_019': {
        'statement': 'request=q31在本次请求中已发起网络调用。',
        'selectors': [['row_02', 'message']],
        'counterevidence': 'request=q31 never attempted a network call',
    },
    'rca_candidate_025': {
        'statement': 'request=q37的原始异常已被捕获。',
        'selectors': [['row_01', 'message']],
        'counterevidence': 'original exception, downstream status and wire trace were not captured',
    },
}


def build_revised_cases(source_dataset=None):
    """Return versioned question edits over the exact frozen v1 input worlds."""
    source = source_dataset if source_dataset is not None else load_dataset(DEFAULT_MANIFEST)
    if source['fingerprint'] != SOURCE_FINGERPRINT:
        raise ValueError('Question revision requires the frozen rca_v1 dataset')
    cases = deepcopy(source['cases'])
    for case in cases:
        if case['split'] != 'candidate_acceptance':
            continue
        truth = case['truth']
        control = next(item for item in truth['fixed_controls'] if item['control_id'] == 'control_02')
        original = deepcopy(control)
        control['statement'] = ATOMIC_UNKNOWN_STATEMENT
        control['expected_relation'] = 'insufficient'
        # The old selector still points to the exact evidence supplied in v1.
        # Absence of crash evidence is not a statement proving no crash happened.
        edits = [{
            'control_id': 'control_02',
            'operation': 'replace_ambiguous_statement',
            'previous_statement': original['statement'],
            'previous_candidate_relation': original['expected_relation'],
            'candidate_relation': 'insufficient',
            'reason': 'Judge the process-state proposition alone; there is no direct crash or alive-state evidence. Missing evidence is not a contradiction.',
        }]
        direct = DIRECT_COUNTEREVIDENCE.get(case['case_id'])
        if direct:
            truth['fixed_controls'].append({
                'control_id': 'control_03', 'statement': direct['statement'],
                'level': 'mechanism', 'selectors': deepcopy(direct['selectors']),
                'expected_relation': 'contradicted',
            })
            edits.append({
                'control_id': 'control_03', 'operation': 'add_separate_direct_contradiction',
                'candidate_relation': 'contradicted',
                'counterevidence_text': direct['counterevidence'],
                'reason': 'An explicit opposite statement exists in the supplied observation; this question is independent from the unsupported database-state proposition.',
            })
        truth['evaluation_question_revision'] = {
            'version': VERSION, 'source_dataset_fingerprint': source['fingerprint'],
            'source_public_sha256': case['public_sha256'],
            'observations_changed': False, 'source_queries_changed': False,
            'human_adjudication': 'pending', 'is_gold': False, 'edits': edits,
        }
        case['public_diagnostics']['fixed_controls'] = [
            {key: deepcopy(value) for key, value in item.items() if key != 'expected_relation'}
            for item in truth['fixed_controls']
        ]
    return cases


def write_dataset(folder=V2_MANIFEST.parent, *, source_manifest=DEFAULT_MANIFEST):
    """Write a new version only; never modify v1 or an existing output folder."""
    source = load_dataset(source_manifest)
    cases = build_revised_cases(source)
    folder = Path(folder)
    if folder.resolve() == Path(source_manifest).resolve().parent:
        raise ValueError('A question revision must not overwrite its source dataset')
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError('Dataset output is not empty; use a new version directory')
    for name in ('public', 'private', 'diagnostics'):
        (folder / name).mkdir(parents=True, exist_ok=True)
    entries = []
    for case in cases:
        entry = {key: case[key] for key in ('case_id', 'split', 'scenario_family', 'independence_group')}
        for label, field in (('public', 'public'), ('truth', 'truth'), ('diagnostics', 'public_diagnostics')):
            subfolder = 'private' if label == 'truth' else label
            relative = subfolder + '/' + case['case_id'] + '.json'
            artifact = folder / relative
            if label == 'public' or case['split'] == 'calibration':
                # Preserve byte identity, including encoding and newline style.
                source_file = Path(source_manifest).resolve().parent / case[label + '_path']
                artifact.write_bytes(source_file.read_bytes())
            else:
                artifact.write_text(json.dumps(case[field], ensure_ascii=False, indent=2) + '\n',
                                    encoding='utf-8', newline='\n')
            entry[label + '_path'], entry[label + '_sha256'] = relative, sha_file(artifact)
        entries.append(entry)
    manifest = deepcopy(source['manifest'])
    manifest['dataset_version'] = VERSION
    manifest['cases'] = entries
    manifest['question_revision'] = {
        'source_dataset_version': source['manifest']['dataset_version'],
        'source_manifest_sha256': source['manifest_sha256'],
        'source_dataset_fingerprint': source['fingerprint'],
        'public_observations_byte_identical': True,
        'public_source_queries_unchanged': True,
        'historical_results_relabelled': False,
        'candidate_control_02_replacements': 30,
        'separate_direct_counterevidence_controls': sorted(DIRECT_COUNTEREVIDENCE),
        'unchanged_control_01': True,
        'comparison_note': 'E2E and sufficient-evidence inputs are byte-identical to v1. Reworded control_02 is a new task, not a paired accuracy improvement; added control_03 is reported separately.',
        'relation_reference_status': 'proposed_pending_human_review',
        'human_verified': False, 'is_gold': False,
    }
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                        encoding='utf-8', newline='\n')
    return load_dataset(folder / 'manifest.json')


if __name__ == '__main__':
    print(json.dumps(write_dataset()['validation'], ensure_ascii=False))
