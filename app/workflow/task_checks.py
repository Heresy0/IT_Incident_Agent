"""Mechanical coverage of declared task checks; deliberately not a semantic judge."""
from datetime import datetime
from tools.query_coverage import covers


def completion(checks, executor):
    results, gaps, limits = [], [], []
    for check in checks:
        matches = [row for row in executor.check_results.values()
                   if row['tool'] == check['tool'] and covers(check['tool'], row['args'], check['args'])]
        assessed = []
        for row in matches:
            status = ('failed' if row['status'] == 'error' else 'truncated' if row['truncated']
                      else 'empty' if row['status'] == 'empty' else 'satisfied')
            if status == 'satisfied' and 'start' in check['args']:
                args = check['args']
                samples = [executor.evidence[eid] for eid in row['evidence_ids']
                    if datetime.fromisoformat(args['start']) <= executor.evidence[eid].observed_from
                        <= datetime.fromisoformat(args['end'])]
                if check['tool'] == 'get_service_metrics':
                    if not set(args['metrics']) <= {e.payload.get('metric') for e in samples}:
                        status = 'missing_samples'
                # A complete superset proves a narrower event query has no matches,
                # even if its nonmatching rows were outside the narrower window.
                elif not any(all(args.get(key) is None or e.payload.get(key) == args[key]
                        for key in ('level', 'error_code') if check['tool'] == 'get_service_logs')
                        and (args.get('category') is None or e.payload.get('category') == args['category'])
                        for e in samples):
                    status = 'empty'
            assessed.append((status, row))
        order = {'satisfied': 0, 'empty': 1, 'missing_samples': 2, 'truncated': 3, 'failed': 4}
        status, row = min(assessed, key=lambda value: order[value[0]]) if assessed else ('not_executed', {})
        query_complete = status in {'satisfied', 'empty', 'missing_samples'}
        results.append({**check, 'status': status, 'query_complete': query_complete,
                        'evidence_ids': row.get('evidence_ids', []),
                        'error': row.get('error')})
        if status == 'empty':
            limits.append(check['tool'] + '：所选窗口及过滤条件内没有匹配记录；查询已完成，但不证明服务正常或原因成立。')
        if status not in {'satisfied', 'empty'} or (status == 'empty' and check['tool'] == 'get_service_metrics'):
            labels = {'not_executed': '尚未执行', 'failed': '查询失败', 'truncated': '结果截断',
                      'empty': '没有匹配样本', 'missing_samples': '所需窗口或字段缺少样本'}
            gaps.append(check['tool'] + '：' + labels[status] + '；不能据此认定任务目标已满足。')
    return {'status': 'checks_incomplete' if gaps else 'checks_completed_empty' if limits
            else 'checks_satisfied' if checks else 'not_specified',
            'query_complete': bool(checks) and all(c['query_complete'] for c in results),
            'goal_verified': False, 'checks': results, 'missing_information': gaps,
            'limitations': limits}


def adds_information(checks, executor):
    """Check proposals against the run ledger, never infer semantic completion."""
    coverage = completion(checks, executor)
    return any((row['tool'], executor.validate_args(row['tool'], row['args']).model_dump_json())
               not in executor.check_results and row['status'] not in {'satisfied', 'empty'}
               for row in coverage['checks'])
