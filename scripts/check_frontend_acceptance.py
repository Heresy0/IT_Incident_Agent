"""Read-only companion to real-browser acceptance; never creates a diagnosis.

Checks HTTP, persisted content and SSE. Browser visibility is separately recorded
from actual UI observations, not inferred from a successful HTTP response.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts.demo_frontend_acceptance import TOKEN, PRESERVED_FIELDS, read, sha, write


def validate_origin(value):
    origin = urlsplit(value)
    if (origin.scheme != 'http' or origin.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or origin.username or origin.password or origin.path not in {'', '/'} or origin.query or origin.fragment):
        raise ValueError('Only an explicit loopback HTTP origin is allowed.')


def check_http(base_url):
    import httpx
    validate_origin(base_url)
    with httpx.Client(base_url=base_url, headers={'Authorization': f'Bearer {TOKEN}'},
                      timeout=20, trust_env=False) as client:
        status_response = client.get('/_frontend_acceptance/status')
        status_response.raise_for_status()
        session = status_response.json()
        if session.get('new_provider_calls') != 0 or not session.get('frozen_reports_unchanged'):
            raise ValueError('Expected zero new provider calls and unchanged recorded reports.')
        checks, report_rows = {}, []
        health = client.get('/health')
        checks['real_http_health'] = health.status_code == 200
        page = client.get('/')
        checks['actual_vue_html'] = page.status_code == 200 and 'id="app"' in page.text
        for relative, digest in session['frontend_assets_sha256'].items():
            response = client.get('/' + relative)
            checks['asset:' + relative] = response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == digest
        for replay in session['replays']:
            source = ROOT / replay['source_path']
            if sha(source) != replay['report_sha256']:
                raise ValueError('Frozen native report changed.')
            raw = read(source)['native_report']
            response = client.get('/api/v1/runs/' + replay['run_id'])
            response.raise_for_status()
            run = response.json()
            content_matches = all(run['result'].get(k) == raw.get(k) for k in PRESERVED_FIELDS)
            stream = client.get('/api/v1/runs/' + replay['run_id'] + '/events')
            stream.raise_for_status()
            events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith('data: ')]
            finals = [e for e in events if e['type'] == 'final']
            seqs = [e['seq'] for e in events]
            event_checks = (seqs == list(range(1, len(seqs) + 1)) and len(finals) == 1
                and all(finals[0].get(k) == raw.get(k) for k in PRESERVED_FIELDS))
            tail = client.get('/api/v1/runs/' + replay['run_id'] + '/events',
                              headers={'Last-Event-ID': str(seqs[-1])})
            tail.raise_for_status()
            rid = replay['report_id']
            checks[rid + ':persisted_content'] = content_matches
            checks[rid + ':sse_content_and_sequence'] = event_checks
            checks[rid + ':cursor_no_duplicate_final'] = not tail.text.strip()
            report_rows.append({**replay, 'event_count': len(events), 'final_count': len(finals),
                'native_fields_preserved': content_matches, 'sse_verified': event_checks})
        listing = client.get('/api/v1/incidents?limit=100')
        # The public API deliberately caps a page at 100.
        listing.raise_for_status()
        incidents = listing.json()['items']
        replay_ids = {r['incident_id'] for r in session['replays']}
        controls = []
        for incident in incidents:
            if incident['id'] in replay_ids or not incident.get('latest_run_id'):
                continue
            run_response = client.get('/api/v1/runs/' + incident['latest_run_id'])
            run_response.raise_for_status()
            run = run_response.json()
            result = run.get('result') or {}
            if result.get('execution_mode') != 'scripted_control_only':
                raise ValueError('Unexpected newly executed non-control diagnosis.')
            stream = client.get('/api/v1/runs/' + run['run_id'] + '/events')
            stream.raise_for_status()
            events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith('data: ')]
            finals = [e for e in events if e['type'] == 'final']
            checks['control:' + run['run_id'] + ':complete_report'] = (
                run['status'] == 'completed' and bool(result.get('final')) and result.get('model') == 'scripted')
            checks['control:' + run['run_id'] + ':sse'] = (
                [e['seq'] for e in events] == list(range(1, len(events) + 1)) and len(finals) == 1
                and all(finals[0].get(k) == result.get(k) for k in PRESERVED_FIELDS))
            controls.append({'incident_id': incident['id'], 'run_id': run['run_id'], 'status': run['status'],
                'execution_mode': result['execution_mode'], 'engine_id': result.get('engine_id'),
                'report_available': bool(result.get('final')), 'new_provider_calls': 0,
                'event_count': len(events), 'final_count': len(finals),
                'result_canonical_sha256': hashlib.sha256(json.dumps(result, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode('utf8')).hexdigest()})
        unauth = client.get('/api/v1/runs/' + session['replays'][0]['run_id'], headers={'Authorization': ''})
        checks['authentication_required'] = unauth.status_code == 401
        return {'schema_version': 'frontend-http-acceptance-v1', 'passed': all(checks.values()),
            'origin': base_url, 'checks': checks, 'replays': report_rows, 'new_control_runs': controls,
            'incident_count': len(incidents), 'new_provider_calls': 0,
            'browser_visibility_verified_by_this_script': False, 'human_verified': False,
            'scope': 'Read-only actual HTTP/Vue assets/PostgreSQL/SSE checks; actual browser observations are separate.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8176')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = check_http(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write(args.output, result)
    print(json.dumps({'output': str(args.output), 'passed': result['passed'],
        'checks': len(result['checks']), 'replayed_reports': len(result['replays']),
        'new_control_runs': len(result['new_control_runs']), 'new_provider_calls': 0}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
