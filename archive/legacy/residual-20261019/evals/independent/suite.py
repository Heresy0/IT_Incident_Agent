"""Frozen catalog and offline data validation. Hash verification never reveals answers."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from api.auth import Principal
from evidence.contracts import TOOL_ARGS
from evals.independent.provider import DATA_ROOT, IndependentProvider

ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = ROOT / 'evals' / 'independent'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_freeze(*, suite_root=SUITE_ROOT, data_root=DATA_ROOT):
    lock = read_json(suite_root / 'freeze.json')
    for relative, expected in lock['files'].items():
        path = ROOT / relative
        if not path.resolve().is_relative_to(ROOT.resolve()) or digest(path) != expected:
            raise ValueError('Frozen dataset changed; create a new version, do not overwrite its record')
    manifest = read_json(suite_root / 'manifest.json')
    suite_relative = suite_root.relative_to(ROOT).as_posix()
    data_relative = data_root.relative_to(ROOT).as_posix()
    expected_files = {f'{suite_relative}/manifest.json', f'{suite_relative}/rubric.json',
                      f'{data_relative}/catalog.json'}
    expected_files.update(f'{data_relative}/{cid}.json' for cid in manifest['cases'])
    expected_files.update(f'{suite_relative}/answers/{cid}.json' for cid in manifest['cases'])
    if set(lock['files']) != expected_files or lock['dataset_version'] != manifest['dataset_version']:
        raise ValueError('Freeze inventory mismatch')
    return lock


def select_cases(split='validation', cases=None, release_holdout=False, *, suite_root=SUITE_ROOT):
    manifest = read_json(suite_root / 'manifest.json')
    selected = list(manifest['splits'][split] if cases is None else cases)
    if not selected or len(selected) != len(set(selected)) or not set(selected).issubset(manifest['splits'][split]):
        raise ValueError('Unknown, duplicate or mixed-split cases')
    if split == 'holdout' and not release_holdout:
        raise ValueError('Reserved cases require --release-holdout; use validation while developing')
    return selected


def check_suite(*, suite_root=SUITE_ROOT, data_root=DATA_ROOT, provider_factory=IndependentProvider,
                answer_split='validation'):
    lock = verify_freeze(suite_root=suite_root, data_root=data_root)
    manifest = read_json(suite_root / 'manifest.json')
    catalog = read_json(data_root / 'catalog.json')
    ids = manifest['cases']
    if [row['case_id'] for row in catalog] != ids or set(ids) != set(sum(manifest['splits'].values(), [])) \
            or len(sum(manifest['splits'].values(), [])) != len(ids):
        raise ValueError('Catalog/split mismatch')
    if set(ids).intersection(read_json(ROOT / 'evals/incident/manifest.json')['cases']):
        raise ValueError('Independent and development IDs overlap')
    principal = Principal('synthetic_demo', 'cli_reader')
    tools_checked = 0
    for cid in ids:
        provider = provider_factory(cid, principal)
        ticket = provider.ticket()
        data = provider._data
        if data.get('synthetic') is not True or data['ticket']['incident_id'] != cid \
                or {'cause', 'gold', 'correct_answer', 'expected_outcome', 'split'}.intersection(data):
            raise ValueError('Fixture must contain observations only')
        provider.authorize(principal, ticket.scope)
        for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
            rows = data[kind]
            if len({r['id'] for r in rows}) != len(rows):
                raise ValueError('Duplicate observation ID')
            for row in rows:
                if row['service'] not in {ticket.scope.service, *ticket.scope.allowed_dependencies} \
                        or row['environment'] != ticket.scope.environment \
                        or row['data_version'] != manifest['dataset_version']:
                    raise ValueError('Observation scope/version mismatch')
                timestamp = datetime.fromisoformat(row['timestamp'])
                if timestamp.tzinfo is None or not ticket.scope.start <= timestamp <= ticket.scope.end:
                    raise ValueError('Observation outside ticket window')
        window = {k: ticket.scope.model_dump(mode='json')[k] for k in ('start', 'end')}
        queries = [('get_service_metrics', {**window, 'metrics': sorted({r['metric'] for r in data['metrics']})}),
                   *[('get_service_logs', {**window, 'category': c}) for c in ('dependency', 'resource', 'configuration')],
                   ('get_recent_changes', window), ('get_service_owner', {'alias': ticket.scope.service}),
                   ('search_runbooks', {'query': ticket.symptoms[:200]}),
                   ('search_incidents', {'symptoms': ticket.symptoms[:200]})]
        for name, raw in queries:
            args = TOOL_ARGS[name].model_validate_json(json.dumps(raw))
            try:
                rows, truncated = provider.query(name, args, ticket.scope)
            except Exception as exc:
                if getattr(exc, 'code', None) != data.get('source_errors', {}).get(name):
                    raise
            else:
                if any({'search_text', 'visibility', 'cause', 'gold'}.intersection(row) for row in rows) or truncated:
                    raise ValueError('Unexpected leakage/truncation in authored dataset')
            tools_checked += 1
    # Reserved answers stay unopened: hashes only until explicit release and saved runs.
    rubric = read_json(suite_root / 'rubric.json')
    for cid in manifest['splits'][answer_split]:
        answer = read_json(suite_root / 'answers' / f'{cid}.json')
        if answer['case_id'] != cid or set(answer['guidance']) != set(rubric['dimensions']):
            raise ValueError('Reference rubric mismatch')
    return {'dataset_version': lock['dataset_version'], 'cases': len(ids),
            'splits': {k: len(v) for k, v in manifest['splits'].items()},
            'provider_queries': tools_checked, 'model_calls': 0,
            'reserved_answers_parsed': False, 'quality_status': 'not_evaluated'}
