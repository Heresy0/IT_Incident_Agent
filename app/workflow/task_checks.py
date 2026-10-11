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


def log_query_feedback(executor):
    """Explain actual filter coverage and safe relaxation, not causal truth.

    Proposals only remove optional filters from a validated scoped read. They
    do not expand the window, choose a new category or execute automatically.
    """
    feedback = []
    observed_codes = {e.payload.get('error_code') for e in executor.evidence.values()
                      if e.kind == 'observation' and 'message' in e.payload}
    for row in executor.check_results.values():
        if row['tool'] != 'get_service_logs':
            continue
        args = row['args']
        complete = row['status'] != 'error' and not row['truncated']
        restricted = [key for key in ('level', 'error_code') if args.get(key) is not None]
        # Ordinary unfiltered samples are already in collection/evidence. Only
        # repeat metadata needed to interpret restrictions or incomplete reads.
        if complete and row['status'] == 'ok' and not restricted:
            continue
        if complete and restricted and any(
                other['tool'] == 'get_service_logs' and other['status'] != 'error'
                and not other['truncated']
                and not any(other['args'].get(key) is not None for key in ('level', 'error_code'))
                and covers('get_service_logs', other['args'], args)
                for other in executor.check_results.values()):
            # The unfiltered superset has already checked this restriction.
            # Its original event remains in collection for provenance.
            continue
        proposals = []
        if complete and row['status'] == 'empty' and restricted:
            broader = executor.validate_args('get_service_logs',
                {**args, 'level': None, 'error_code': None}).model_dump(mode='json')
            proposal = {'tool': 'get_service_logs', 'args': broader}
            if adds_information([proposal], executor):
                proposals.append(proposal)
        feedback.append({'query_profile': args, 'status': row['status'],
            'query_complete': complete, 'restricted_by': restricted,
            'error_code_observed_in_log_fields': args.get('error_code') in observed_codes
                if args.get('error_code') else None,
            'meaning': ('query_failed' if row['status'] == 'error' else
                        'incomplete_sample' if row['truncated'] else
                        'no_matches_in_selected_scope' if row['status'] == 'empty' else
                        'matched_samples_not_causal_proof'),
            'broader_checks': proposals,
            'this_query_covers_other_categories': False})
    return feedback[-4:]
