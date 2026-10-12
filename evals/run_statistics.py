"""Offline operational and human quality statistics; no model/configuration imports."""
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

VERSION = 'run-statistics-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def load_reports(report_paths=(), bundle_paths=()):
    """Keep failures, verify bundle hashes/configuration, reject duplicates and traversal."""
    loaded = []
    def read(path, expected=None, bundle=None):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected is not None and digest != expected['report_sha256']:
            raise ValueError('Saved report hash mismatch')
        report = json.loads(raw)
        if not isinstance(report, dict) or not report.get('run_id') or not isinstance(report.get('run_summary'), dict):
            raise ValueError('Expected a saved run report with run_id and run_summary')
        if expected is not None:
            if any(report.get(key) != expected[key] for key in ('workflow',) if key in expected) \
                    or report.get('incident_id') != expected['case_id']:
                raise ValueError('Bundle run identity mismatch')
            config = bundle['configuration']
            if report.get('provenance') != bundle.get('provenance') or any(
                    report.get(key) != config[key] for key in ('model', 'execution_mode') if key in config) \
                    or report['run_summary'].get('limits') != config.get('limits'):
                raise ValueError('Bundle configuration/provenance mismatch')
        loaded.append({'report': report, 'sha256': digest, 'path': str(path.resolve())})
    for path in report_paths:
        read(Path(path))
    for path in bundle_paths:
        path = Path(path)
        bundle = json.loads(path.read_text(encoding='utf-8'))
        for entry in bundle['runs']:
            saved = path.parent / entry['report_file']
            if saved.resolve().parent != path.parent.resolve() or saved.suffix != '.json':
                raise ValueError('Bundle report path must stay in its directory')
            read(saved, entry, bundle)
    if not loaded or len({row['report']['run_id'] for row in loaded}) != len(loaded):
        raise ValueError('Supply at least one report, with unique run IDs')
    return loaded


def action_targets(report):
    return [{'target_id': f'A{i}', 'action': deepcopy(row)} for i, row in
            enumerate(report.get('output', {}).get('recommended_actions', []), 1)]


def blocked_targets(report):
    """A blocked target is a candidate for external review, not automatically a false block."""
    return [{'revision': row['revision'], 'target_id': item['target_id'],
             'assessment': deepcopy(item)} for row in report.get('reviews', [])
            for item in row.get('review', {}).get('assessments', [])
            if item.get('verdict') in {'uncertain', 'not_supported'}]


def review_template(loaded):
    return {'version': VERSION, 'runs': [{
        'run_id': row['report']['run_id'], 'case_id': row['report'].get('incident_id'),
        'report_sha256': row['sha256'], 'reviewer': '', 'reviewer_type': 'human',
        'approved': False, 'rationale': '',
        'critical_checks': [], 'conclusion_correct': None,
        'actions': [{**target, 'supported': None, 'reason': ''}
                    for target in action_targets(row['report'])],
        'review_blocks': [{**target, 'unjustified': None, 'reason': ''}
                          for target in blocked_targets(row['report'])],
        'handoff': {'required': None, 'actionable': None, 'reason': ''},
        'human_effort': {'baseline_ms': None, 'assisted_ms': None},
    } for row in loaded]}


def validate_reviews(loaded, reviews):
    originals = {row['report']['run_id']: row for row in loaded}
    accepted, seen = {}, set()
    for review in (reviews or {}).get('runs', []):
        rid = review['run_id']
        if rid in seen or rid not in originals:
            raise ValueError('Unknown or duplicate statistics review')
        seen.add(rid)
        original = originals[rid]
        report = original['report']
        if review.get('report_sha256') != original['sha256'] or review.get('case_id') != report.get('incident_id'):
            raise ValueError('Statistics review provenance mismatch')
        if type(review.get('approved')) is not bool or review.get('reviewer_type') not in {'human', 'assistant'}:
            raise ValueError('Review requires explicit approval and reviewer_type')
        if not review['approved']:
            continue
        if report.get('execution_mode') != 'live':
            raise ValueError('Scripted reports cannot receive business quality scores')
        if not review.get('reviewer', '').strip() or not review.get('rationale', '').strip():
            raise ValueError('Approved review requires reviewer and rationale')
        for name, targets, keys, verdict in (
                ('actions', action_targets(report), ('target_id', 'action'), 'supported'),
                ('review_blocks', blocked_targets(report), ('revision', 'target_id', 'assessment'), 'unjustified')):
            rows = review.get(name, [])
            identities = lambda items: [tuple(canonical(r[k]) for k in keys) for r in items]
            if Counter(identities(rows)) != Counter(identities(targets)) or any(
                    type(r.get(verdict)) is not bool or not r.get('reason', '').strip() for r in rows):
                raise ValueError('Review must assess every original action/block without changing its content')
        checks = review.get('critical_checks', [])
        if not isinstance(checks, list) or len({c['check_id'] for c in checks}) != len(checks) or any(
                not isinstance(c.get('check_id'), str) or not c['check_id'].strip()
                or c.get('status') not in {'observed', 'missed', 'unavailable', 'not_applicable'}
                or not c.get('reason', '').strip() for c in checks):
            raise ValueError('Critical checks require unique IDs, status and reason')
        if review.get('conclusion_correct') is not None and type(review['conclusion_correct']) is not bool:
            raise ValueError('conclusion_correct must be boolean or null')
        handoff = review.get('handoff', {})
        if type(handoff.get('required')) is not bool or (handoff['required'] and (
                type(handoff.get('actionable')) is not bool or not handoff.get('reason', '').strip())):
            raise ValueError('Review must declare whether a handoff is required and assess it when required')
        effort = review.get('human_effort', {})
        values = [effort.get(k) for k in ('baseline_ms', 'assisted_ms')]
        if any(v is not None for v in values) and not all(number(v) for v in values):
            raise ValueError('Human effort requires two nonnegative measured durations')
        # Assistant reviews remain supplied evidence; only confirmed human reviews enter rates.
        if review['reviewer_type'] == 'human':
            accepted[rid] = review
    return accepted


def validate_pricing(pricing):
    if pricing is None:
        return
    if not isinstance(pricing.get('currency'), str) or not pricing['currency'].strip() \
            or not isinstance(pricing.get('models'), dict):
        raise ValueError('Pricing requires currency and model-specific rates')
    for rates in pricing['models'].values():
        if not isinstance(rates, dict) or not all(number(rates.get(k)) for k in ('input_per_million', 'output_per_million')):
            raise ValueError('Token prices must be finite nonnegative numbers')


def engineering(report, pricing=None):
    events, summary = report.get('events', []), report['run_summary']
    events_available = isinstance(report.get('events'), list)
    failures = [e for e in events if e.get('type') == 'validation_failure']
    tools = [e for e in events if e.get('type') == 'tool_result']
    declared = {}
    for task in report.get('task_results', []):
        for check in task.get('completion', {}).get('checks', []):
            declared[canonical([task.get('role'), check.get('tool'), check.get('args')])] = check['status']
    protocol = Counter((e.get('role') or e.get('node') or 'unknown', e.get('reason', 'unknown')) for e in failures)
    usage = summary.get('token_usage', {})
    rates = (pricing or {}).get('models', {}).get(report.get('model'))
    cost = None
    if report.get('execution_mode') == 'live' and rates and usage.get('status') == 'known' \
            and usage.get('known_calls') == summary.get('model_calls') \
            and all(number(usage.get(k)) for k in ('input_tokens', 'output_tokens')):
        cost = (usage['input_tokens'] * rates['input_per_million']
                + usage['output_tokens'] * rates['output_per_million']) / 1_000_000
    result = {
        'events_available': events_available,
        'protocol_error_count': len(failures),
        'protocol_errors': [{'role': r, 'reason': reason, 'count': count}
                            for (r, reason), count in sorted(protocol.items())],
        'correction_count': sum(e.get('type') == 'validation_repair' for e in events),
        'query_errors': sum(e.get('status') == 'error' for e in tools),
        'empty_queries': sum(e.get('status') == 'empty' for e in tools),
        'truncated_queries': sum(e.get('truncated') is True for e in tools),
        'declared_checks': dict(sorted(Counter(declared.values()).items())),
        'declared_checks_total': len(declared),
        'declared_checks_not_executed': sum(v == 'not_executed' for v in declared.values()),
        'termination_reason': summary.get('termination_reason'),
        'budget_stop': summary.get('termination_reason') in {'BUDGET_EXCEEDED', 'TIME_BUDGET_EXCEEDED', 'STEP_LIMIT'},
        'duration_ms': summary.get('duration_ms'), 'model_calls': summary.get('model_calls'),
        'tool_calls': summary.get('tool_calls'), 'token_usage': usage,
        'estimated_token_cost': cost,
        'cost_status': 'estimated_from_supplied_rates' if cost is not None else 'unknown',
    }
    if not events_available:
        for key in ('protocol_error_count', 'correction_count', 'query_errors', 'empty_queries', 'truncated_queries'):
            result[key] = None
    return result


def distribution(values, *, signed=False):
    values = sorted(v for v in values if number(v) or (
        signed and type(v) in (int, float) and math.isfinite(v)))
    return {'known_samples': len(values), 'mean': sum(values) / len(values) if values else None,
            'p50': values[math.ceil(len(values) * .5) - 1] if values else None,
            'p95': values[math.ceil(len(values) * .95) - 1] if values else None}


def rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def group_identity(report):
    provenance = report.get('provenance') or {}
    # A report without an implementation fingerprint cannot establish a comparable cohort.
    source = report.get('source_version') or provenance.get('implementation_sha256_prefix')
    if not source and provenance.get('working_tree_dirty') is False:
        source = provenance.get('git_commit')
    identity = {'workflow': report.get('workflow', report.get('task_type', 'unknown')),
                'model': report.get('model', 'unknown'), 'execution_mode': report.get('execution_mode', 'unknown'),
                'source_version': source, 'workflow_version': report.get('workflow_version'),
                'data_source': report.get('data_source'),
                'dataset_version': provenance.get('dataset_version'), 'limits': report['run_summary'].get('limits'),
                'data_version': report.get('scope', {}).get('data_version'),
                'collection_execution': {k: v for k, v in report.get('collection_execution', {}).items()
                                         if k not in {'parallel_batches'}},
                'blind_review_enabled': report.get('blind_review_enabled'),
                'reviewer_repair_mode': report.get('reviewer_repair_mode'),
                'review_contract_version': report.get('review_contract_version'),
                'provenance_sha256': hashlib.sha256(canonical(provenance).encode()).hexdigest() if provenance else None}
    if not source:
        identity['unverified_run_id'] = report['run_id']
    return identity


def summarize(loaded, reviews=None, pricing=None):
    validate_pricing(pricing)
    judged = validate_reviews(loaded, reviews)
    rows, cohorts = [], defaultdict(list)
    for item in loaded:
        report = item['report']
        identity = group_identity(report)
        group = hashlib.sha256(canonical(identity).encode()).hexdigest()[:16]
        row = {'run_id': report['run_id'], 'case_id': report.get('incident_id'),
               'report_sha256': item['sha256'], 'group_id': group, 'status': report.get('status'),
               'prompt_version': report.get('prompt_version'),
               'engineering': engineering(report, pricing), 'quality': judged.get(report['run_id']),
               'quality_status': 'excluded_scripted' if report.get('execution_mode') != 'live'
                   else 'human_reviewed' if report['run_id'] in judged else 'human_review_pending'}
        rows.append(row)
        cohorts[canonical(identity)].append(row)
    groups = []
    for key, selected in cohorts.items():
        es = [r['engineering'] for r in selected]
        qs = [r['quality'] for r in selected if r['quality'] is not None]
        checks = [c for q in qs for c in q['critical_checks']]
        actions = [a for q in qs for a in q['actions']]
        blocks = [b for q in qs for b in q['review_blocks']]
        handoffs = [q['handoff'] for q in qs if q['handoff']['required']]
        conclusions = [q['conclusion_correct'] for q in qs if q.get('conclusion_correct') is not None]
        efforts = [q['human_effort'] for q in qs if number(q.get('human_effort', {}).get('baseline_ms'))]
        costs = [e['estimated_token_cost'] for e in es if e['estimated_token_cost'] is not None]
        protocol = Counter()
        declared = Counter()
        for e in es:
            declared.update(e['declared_checks'])
            for error in e['protocol_errors']:
                protocol[(error['role'], error['reason'])] += error['count']
        groups.append({'group_id': selected[0]['group_id'], 'identity': json.loads(key), 'runs': len(selected),
            'prompt_versions_observed': sorted({r['prompt_version'] for r in selected if r['prompt_version']}),
            'statuses': dict(Counter(r['status'] for r in selected)),
            'engineering': {
                'events_known_runs': sum(e['events_available'] for e in es),
                'protocol_error_run_rate': rate(sum(e['protocol_error_count'] > 0 for e in es if e['events_available']),
                                               sum(e['events_available'] for e in es)),
                'protocol_errors': [{'role': r, 'reason': reason, 'count': count} for (r, reason), count in sorted(protocol.items())],
                **{(k if k.endswith('_total') else k + '_total'):
                    sum(e[k] for e in es if number(e[k])) if any(number(e[k]) for e in es) else None for k in (
                    'protocol_error_count', 'correction_count', 'query_errors', 'empty_queries', 'truncated_queries',
                    'declared_checks_total', 'declared_checks_not_executed')},
                'budget_stop_runs': sum(e['budget_stop'] for e in es),
                'declared_checks': dict(sorted(declared.items())),
                'termination_reasons': dict(Counter(e['termination_reason'] for e in es)),
                'duration_ms': distribution(e['duration_ms'] for e in es),
                **{k: {'known_runs': sum(number(e[k]) for e in es),
                       'total': sum(e[k] for e in es if number(e[k])) if any(number(e[k]) for e in es) else None}
                   for k in ('model_calls', 'tool_calls')},
                'input_tokens_known': sum(e['token_usage'].get('input_tokens', 0) for e in es),
                'output_tokens_known': sum(e['token_usage'].get('output_tokens', 0) for e in es),
                'estimated_token_cost': {'known_runs': len(costs), 'known_total': sum(costs) if costs else None,
                    'total': sum(costs) if len(costs) == len(es) else None,
                    'currency': (pricing or {}).get('currency')},
            }, 'quality': {
                'human_reviewed_runs': len(qs),
                'critical_check_omission': rate(sum(c['status'] == 'missed' for c in checks),
                                              sum(c['status'] in {'observed', 'missed'} for c in checks)),
                'critical_checks_unavailable': sum(c['status'] == 'unavailable' for c in checks),
                'critical_checks_not_applicable': sum(c['status'] == 'not_applicable' for c in checks),
                'wrong_conclusion': rate(sum(c is False for c in conclusions), len(conclusions)),
                'unsupported_published_advice': rate(sum(not a['supported'] for a in actions), len(actions)),
                'reviewer_false_block': rate(sum(b['unjustified'] for b in blocks), len(blocks)),
                'actionable_handoff': rate(sum(h['actionable'] for h in handoffs), len(handoffs)),
                'human_effort_pairs': len(efforts),
                'human_effort_difference': distribution((e['baseline_ms'] - e['assisted_ms'] for e in efforts), signed=True),
                'human_effort_difference_ms': [e['baseline_ms'] - e['assisted_ms'] for e in efforts],
            }})
    return {'version': VERSION, 'runs_total': len(rows), 'groups': groups, 'runs': rows,
            'pricing': pricing, 'notes': [
                '各组按模型、执行模式、源码/数据版本、预算和流程配置分开；失败和partial保留。',
                'prompt_version包含按服务和观测生成的动态工具Schema，逐条记录而不作为分组键；提示词实现由源码指纹隔离。',
                '协议错误按校验失败尝试计数，包括已纠正错误；声明检查未执行不等于关键检查漏查。',
                '业务比例仅使用批准的人类评价；脚本和助手评价不计入；无评价或无分母为null。',
                '源不可用单列，不当成漏查；合法引用、内部passed和completed不证明结论正确。',
                '耗时为系统运行耗时，不是人工工时或MTTR；人工收益需要同任务实测前后工时。',
                '成本仅按用户提供价格估算Token费用；用量不完整时总费用未知，不包含工具/基础设施费用。']}


def markdown(summary):
    lines = ['# 故障诊断分类统计', '', f"运行数：{summary['runs_total']}；可比较分组：{len(summary['groups'])}。", '']
    def show(value):
        return '未知' if value is None else str(round(value, 4)) if isinstance(value, float) else str(value)
    def fraction(value):
        return '未知（0样本）' if value['rate'] is None else f"{value['numerator']}/{value['denominator']}（{value['rate']:.1%}）"
    for group in summary['groups']:
        identity, e, q = group['identity'], group['engineering'], group['quality']
        lines += [f"## 分组 {group['group_id']}", '',
                  f"流程：{identity['workflow']}；模型：{identity['model']}；模式：{identity['execution_mode']}；源码：{identity['source_version']}。",
                  f"数据源：{identity['data_source']}；数据集版本：{identity['dataset_version']}；预算：{canonical(identity['limits'])}。",
                  f"运行数：{group['runs']}；状态：{canonical(group['statuses'])}；人工评价：{q['human_reviewed_runs']}。", '',
                  '| 指标 | 结果 |', '| --- | --- |',
                  f"| 事件记录可用运行 | {e['events_known_runs']} / {group['runs']} |",
                  f"| 出现协议错误的运行 | {fraction(e['protocol_error_run_rate'])} |",
                  f"| 协议错误 / 纠正尝试 | {show(e['protocol_error_count_total'])} / {show(e['correction_count_total'])} |",
                  f"| 查询失败 / 空结果 / 截断 | {show(e['query_errors_total'])} / {show(e['empty_queries_total'])} / {show(e['truncated_queries_total'])} |",
                  f"| 声明检查未执行 / 声明检查总数 | {e['declared_checks_not_executed_total']} / {e['declared_checks_total']} |",
                  f"| 声明检查状态分类 | {canonical(e['declared_checks'])} |",
                  f"| 预算退出运行 | {e['budget_stop_runs']} |",
                  f"| 耗时均值 / P50 / P95（ms） | {show(e['duration_ms']['mean'])} / {show(e['duration_ms']['p50'])} / {show(e['duration_ms']['p95'])} |",
                  f"| 模型 / 工具调用 | {show(e['model_calls']['total'])} / {show(e['tool_calls']['total'])} |",
                  f"| 已记录输入 / 输出Token | {e['input_tokens_known']} / {e['output_tokens_known']} |",
                  f"| Token总费用估算 | {show(e['estimated_token_cost']['total'])} {e['estimated_token_cost']['currency'] or ''}；费用已知运行 {e['estimated_token_cost']['known_runs']} |",
                  f"| 关键检查遗漏 | {fraction(q['critical_check_omission'])}；渠道不可用 {q['critical_checks_unavailable']} |",
                  f"| 错误结论 | {fraction(q['wrong_conclusion'])} |",
                  f"| 无依据的已发布建议 | {fraction(q['unsupported_published_advice'])} |",
                  f"| 复核误拦 | {fraction(q['reviewer_false_block'])} |",
                  f"| 有效人工交接 | {fraction(q['actionable_handoff'])} |",
                  f"| 实测人工工时差均值（ms，正数为节省） | {show(q['human_effort_difference']['mean'])}；配对 {q['human_effort_pairs']} |", '',
                  '协议错误分类：', '', '| 角色 | 原因 | 次数 |', '| --- | --- | --- |']
        for error in e['protocol_errors']:
            lines.append(f"| {error['role']} | {error['reason']} | {error['count']} |")
        if not e['protocol_errors']:
            lines.append('| — | 无已记录校验错误 | 0 |' if e['events_known_runs'] else '| — | 事件记录未知 | 未知 |')
        lines.append('')
    lines += ['## 统计口径', ''] + ['- ' + note for note in summary['notes']]
    return '\n'.join(lines) + '\n'
