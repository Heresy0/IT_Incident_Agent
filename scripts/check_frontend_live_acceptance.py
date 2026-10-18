"""Read-only verification of new UI-started Flash runs, persisted SSE and usage.

This verifies execution/delivery. It does not score root-cause accuracy or infer
browser visibility from an API response. Actual browser observations are separate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts.check_frontend_acceptance import validate_origin
from scripts.demo_frontend_acceptance import TOKEN
from scripts.demo_frontend_live_acceptance import read, write, MODEL


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def check(base_url, output, run_ids):
    import httpx
    validate_origin(base_url)
    output = Path(output)
    with httpx.Client(base_url=base_url, headers={'Authorization': f'Bearer {TOKEN}'},
                      timeout=25, trust_env=False) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response
        state = get('/_frontend_live_acceptance/status').json()
        write(output / 'status.json', state)
        plan = state['admission']
        checks = {'real_http_health': get('/health').json().get('status') == 'ok',
            'closed_loop_admission': plan['execution_variant'] == 'closed_loop'
                and plan['enable_rework'] and plan['enable_challenge'],
            'flash_frozen': state['model'] == MODEL,
            'isolated_database': state['daily_database_used'] is False,
            'no_real_repair_executors': state['live_repair_executors'] is False,
            'actual_vue_served': 'id="app"' in get('/').text}
        rows = []
        for run_id in run_ids:
            run = get('/api/v1/runs/' + run_id).json()
            write(output / 'runs' / f'{run_id}.json', run)
            if run['status'] == 'running':
                raise ValueError('Wait for the existing run to terminate; this checker never restarts it.')
            result, spec = run['result'], run['config']['run_spec']
            events_text = get('/api/v1/runs/' + run_id + '/events').text
            events = [json.loads(line[6:]) for line in events_text.splitlines() if line.startswith('data: ')]
            write(output / 'events' / f'{run_id}.json', events)
            finals = [e for e in events if e['type'] == 'final']
            types = [e['type'] for e in events]
            model_calls = [e for e in events if e['type'] == 'call_start' and e.get('kind') == 'model']
            usage = [e for e in events if e['type'] == 'usage']
            ledger_path = output.parent / 'server/model-ledger.json'
            paid = [c for c in read(ledger_path)['calls'] if c['run_id'] == run_id]
            run_checks = {
                'new_live_flash': result.get('execution_mode') == 'live' and result.get('model') == MODEL
                    and not result.get('frontend_acceptance_replay'),
                'actual_v2_closed_loop': spec['engine_id'] == 'autonomous_v2'
                    and spec['execution_variant'] == 'closed_loop' and spec['enable_rework']
                    and spec['enable_challenge'] and result['execution_variant'] == 'closed_loop',
                'frozen_source_and_configuration': spec['source_version_full'] == plan['source_version']
                    and spec['configuration_sha256'] == plan['configuration_sha256'],
                'real_tool_reads': any(e['type'] == 'tool_result' for e in events) and bool(result.get('evidence')),
                'typed_review_executed': 'review_v2_batch' in types,
                'report_persisted': bool(result.get('final')),
                'single_final_and_ordered_sequence': len(finals) == 1
                    and [e['seq'] for e in events] == list(range(1, len(events) + 1)),
                'sse_delivers_identical_report': len(finals) == 1 and finals[0].get('final') == result['final'],
                'sse_cursor_no_duplicate': not get('/api/v1/runs/' + run_id + '/events?after_seq=' + str(events[-1]['seq'])).text.strip(),
                'paid_calls_match_runtime': len(paid) == result['run_summary']['model_calls'] == len(model_calls),
                'every_model_call_has_measured_usage': len(usage) == len(model_calls)
                    and all(c.get('usage') is not None for c in paid),
                'usage_matches_paid_records': sum(e['input_tokens'] for e in usage)
                    == sum(c['usage']['input_tokens'] for c in paid if c.get('usage'))
                    and sum(e['output_tokens'] for e in usage)
                    == sum(c['usage']['output_tokens'] for c in paid if c.get('usage')),
                'within_frozen_budget': result['run_summary']['model_calls'] <= spec['limits']['model_calls']
                    and result['run_summary']['tool_calls'] <= spec['limits']['tool_calls'],
                'bounded_semantic_rework': result.get('rework_rounds', 0) <= 1,
                'repair_not_eligible': result.get('repair_eligible') is False,
            }
            challenge_events = [e for e in events if e['type'] == 'semantic_challenge']
            rework_events = [e for e in events if e['type'] == 'semantic_rework_started']
            if rework_events:
                run_checks['rework_reaches_final_review'] = any(e['type'] == 'review_v2_batch'
                    and e['seq'] > rework_events[0]['seq'] for e in events)
            checks.update({run_id + ':' + key: value for key, value in run_checks.items()})
            rows.append({'run_id': run_id, 'incident_id': run['config']['incident_id'],
                'case_id': run['config']['incident_snapshot'].get('demo_case_id'),
                'status': run['status'], 'review_status': result['review_status'],
                'termination_reason': result['run_summary'].get('termination_reason'),
                'model_calls': len(paid), 'tool_calls': result['run_summary']['tool_calls'],
                'input_tokens': sum(c['usage']['input_tokens'] for c in paid if c.get('usage')),
                'output_tokens': sum(c['usage']['output_tokens'] for c in paid if c.get('usage')),
                'duration_ms': result['run_summary'].get('duration_ms'),
                'event_count': len(events), 'challenge_count': len(challenge_events),
                'rework_rounds': result.get('rework_rounds'),
                'rework_reasons': [r.get('reason') for r in result.get('reworks', [])],
                'evidence_count': len(result.get('evidence', [])),
                'report_sha256': hashlib.sha256(result['final'].encode()).hexdigest(),
                'native_run_sha256': canonical_sha(run)})
        before = output.parent / 'before/manifest.json'
        if before.exists():
            for old in read(before)['runs']:
                unchanged = canonical_sha(get('/api/v1/runs/' + old['run_id']).json()) == old['sha256']
                checks['preserved:' + old['run_id']] = unchanged
        checks['global_call_cap_respected'] = state['model_ledger']['admitted_calls'] <= state['model_ledger']['maximum_calls']
        checks['read_requires_authentication'] = client.get('/api/v1/incidents', headers={'Authorization': ''}).status_code == 401
        receipt = {'schema_version': 'frontend-live-http-acceptance-v1', 'passed': all(checks.values()),
                   'checks': checks, 'runs': rows, 'model_ledger': state['model_ledger'],
                   'browser_visibility_verified_by_this_script': False,
                   'root_cause_accuracy_scored': False, 'human_verified': False,
                   'scope': 'New actual Flash execution and delivery; synthetic development observations.'}
        write(output / 'receipt.json', receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8176')
    parser.add_argument('--run-id', action='append', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/frontend-live-acceptance-20261018/http')
    args = parser.parse_args(argv)
    result = check(args.base_url, args.output, args.run_id)
    print(json.dumps({'passed': result['passed'], 'checks': len(result['checks']),
                      'runs': result['runs'], 'model_ledger': result['model_ledger']}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
