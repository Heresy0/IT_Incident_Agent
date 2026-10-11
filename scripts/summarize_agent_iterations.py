"""Offline, hash-bound projection of all iteration runs, including failures.

No model calls or reference-answer reads. Semantic review remains separate.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def summarize(roots):
    rows, seen = [], set()
    for root in roots:
        for path in sorted(root.rglob('*.json')):
            raw = path.read_bytes()
            report = json.loads(raw)
            if not isinstance(report, dict) or 'run_summary' not in report or 'output' not in report:
                continue
            if report['run_id'] in seen:
                raise ValueError('Duplicate run ID in selected roots')
            seen.add(report['run_id'])
            events, summary = report['events'], report['run_summary']
            comparison = path.parent / 'comparison.json'
            scored = json.loads(comparison.read_text(encoding='utf-8')) if comparison.exists() else {}
            mechanical = next((row for row in scored.get('runs', [])
                               if row['run_id'] == report['run_id']), {})
            rows.append({
                'batch': root.name, 'case_id': report['incident_id'], 'workflow': report['workflow'],
                'run_id': report['run_id'], 'report_sha256': hashlib.sha256(raw).hexdigest(),
                'report_file': str(path.resolve()), 'execution_mode': report['execution_mode'],
                'model': report['model'], 'workflow_version': report.get('workflow_version'),
                'implementation': report.get('provenance', {}).get('implementation_sha256_prefix'),
                'dataset_version': report.get('provenance', {}).get('dataset_version'),
                'fixture_sha256': report.get('provenance', {}).get('fixture_sha256', {}).get(report['incident_id']),
                'status': report['status'], 'review_status': report['review_status'],
                'termination_reason': summary['termination_reason'],
                'model_calls': summary['model_calls'], 'tool_calls': summary['tool_calls'],
                'token_usage': summary['token_usage'], 'duration_ms': summary['duration_ms'],
                'role_model_calls': dict(Counter(e['name'] for e in events
                    if e['type'] == 'call_start' and e['kind'] == 'model')),
                'repairs': report['repairs'], 'repair_counts': report.get('repair_counts'),
                'mechanical_checks': mechanical.get('mechanical_checks'),
                'mechanical_pass': mechanical.get('mechanical_pass'),
                'rework_rounds': report.get('rework_rounds', 0),
                'protocol_finalization_used': report.get('protocol_finalization_used', False),
                'blind_review_enabled': report.get('blind_review_enabled', False),
                'observation_reviews': report.get('observation_reviews', []),
                'rejected_actions': report.get('rejected_actions', []),
                'validation_failures': [{k: e.get(k) for k in ('role', 'reason', 'details')}
                    for e in events if e['type'] == 'validation_failure'],
                'queries': [{k: e.get(k) for k in ('name', 'status', 'query_profile', 'truncated')}
                    for e in events if e['type'] == 'tool_result'],
                'hypotheses': [{'cause': h['cause'], 'level': h.get('level'), 'status': h['status']}
                    for h in report['output'].get('hypotheses', [])],
                'actions': report['output'].get('recommended_actions', []),
                'limitations': report['output']['missing_information'],
            })
    return {'review_type': 'execution_projection', 'formal_semantic_review': 'pending',
        'estimated_cost': None, 'runs': rows,
        'totals': {'runs': len(rows), 'model_calls': sum(r['model_calls'] for r in rows),
                   'tool_calls': sum(r['tool_calls'] for r in rows)},
        'note': 'All selected runs retained. Completed/partial and valid protocols do not prove diagnostic correctness.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    result = summarize(args.roots)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), **result['totals']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
