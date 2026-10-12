"""One bounded paired live replay of known synthetic cases; free planning by default."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from agents.models import build_roles
from config.incident import IncidentConfig
from evals.common import write_json
from evals.diagnostic_packs.experiment import cases, digest, SnapshotProvider, P
from evals.diagnostic_packs.reporting import label, labels
from evidence.render import render
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from workflow.task_checks import completion
from tools.executor import ToolExecutor
from runtime.context import RunContext

KNOWN = ('connection_wait', 'source_unavailable', 'authentication')


def arm_label(arm):
    return {'baseline': '原流程', 'pack': '诊断包流程',
            'strict': '调整前复核规则', 'conservative': '调整后复核规则'}[arm]


def summarize_report(report, required, provider, limits):
    executor = ToolExecutor(provider, P, provider.ticket().scope, RunContext(scope=provider.ticket().scope, limits=limits))
    # Rebuild only the mechanical coverage view from persisted read/evidence
    # ledgers; this never executes tools or infers a cause from a query count.
    from evidence.contracts import Evidence
    executor.evidence = {e['evidence_id']: Evidence.model_validate_json(json.dumps(e)) for e in report['evidence']}
    for index, event in enumerate(report['events']):
        if event['type'] == 'tool_result' and event['role'] == 'investigation':
            executor.check_results[index] = {'tool': event['name'], 'args': event['query_profile'],
                'status': event['status'], 'truncated': event['truncated'], 'evidence_ids': event['evidence_ids'],
                'error': (event.get('error') or {}).get('code')}
    summary = report['run_summary']
    return {'status': report['status'], 'review_status': report['review_status'],
            'termination_reason': summary['termination_reason'],
            'query_statuses': [r['status'] for r in completion(required, executor)['checks']],
            'blocking_check_ids': report.get('diagnostic_pack_exit', {}).get('latest', {}).get('blocking_check_ids', []),
            'model_calls': summary['model_calls'], 'tool_calls': summary['tool_calls'],
            'token_usage': summary['token_usage'], 'estimated_cost': summary.get('estimated_cost'),
            'duration_ms': summary['duration_ms'],
            'input_chars_max': max((e.get('input_chars', 0) for e in report['events'] if e['type'] == 'model_input'), default=0),
            'input_budget_rejections': sum(e.get('dimension') == 'input_chars' for e in report['events'] if e['type'] == 'budget_rejected'),
            'protocol_failures': sum(e['type'] == 'validation_failure' for e in report['events']),
            'accepted_drafts': len(report.get('drafts', [])), 'accepted_reviews': len(report.get('reviews', [])),
            'published_actions': len(report['output']['recommended_actions']),
            'pending_actions': len(report.get('pending_actions', [])),
            'observations': len(report['evidence'])}


def markdown(summary):
    config = summary['configuration']
    protection_comparison = config.get('comparison') == 'review-protection'
    lines = ['# 复核保护调整对照验证' if protection_comparison else '# 增加预算后的诊断包验证', '',
             f"模型：{config['model']}；已运行 {len(summary['runs'])} 次；每个流程输入上限 {config['limits']['input_chars']} 字符，"
             f"模型调用上限 {config['limits']['model_calls']} 次，工具调用上限 {config['limits']['tool_calls']} 次，"
             f"单次输出上限 {config['max_output_tokens']} 个词元。", '',
             '| 场景 | 流程 | 运行结果 | 复核结果 | 登记检查状态 | 模型调用次数 | 工具调用次数 | 输入词元 | 输出词元 | 最大输入字符数 | 输入超限次数 | 耗时（秒） |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in summary['runs']:
        usage = row['token_usage']
        token = lambda key: str(usage[key]) if usage.get('status') == 'known' else str(usage.get(key, 0)) + '（已知部分）'
        lines.append(f"| {label(row['case_id'])} | {arm_label(row['arm'])} | "
                     f"{label(row['status'])} | {label(row['review_status'])} | {labels(row['query_statuses'])} | "
                     f"{row['model_calls']} | {row['tool_calls']} | {token('input_tokens')} | {token('output_tokens')} | "
                     f"{row['input_chars_max']} | {row['input_budget_rejections']} | {row['duration_ms']/1000:.1f} |")
    lines += ['', '| 场景 | 流程 | 退出原因 | 协议校验失败次数 | 接受的诊断版本数 | 接受的复核次数 | 已发布建议数 | 待核查建议数 |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in summary['runs']:
        lines.append(f"| {label(row['case_id'])} | {arm_label(row['arm'])} | "
                     f"{label(row['termination_reason'])} | {row['protocol_failures']} | {row['accepted_drafts']} | "
                     f"{row['accepted_reviews']} | {row['published_actions']} | {row['pending_actions']} |")
    lines += ['', '## 解释边界', '',
              ('- 两边都启用相同诊断包，使用相同模型、预算和数据，每个场景每种规则只运行一次。仅复核保护及其模型输入契约不同，不重跑筛选。'
               if protection_comparison else '- 每个场景每个流程只运行一次；两流程使用相同模型、提高后的预算和数据。此对照评估诊断包在高预算下的增量效果，不单独证明提高预算的效果。'),
              '- 使用已知合成开发案例，不是外部盲测或独立保留集。程序不向模型提供评价标准或参考答案。',
              '- 完成状态、建议数量和程序覆盖不能代替业务质量审阅；合理的有限诊断及人工交接可以有价值。',
              '- 费用未配置计价单价，不能将未知费用视为零；词元及调用统计用于核对实际开销。',
              '- 原始报告、数据快照和执行计划保留；错误运行也计入，不通过重跑筛选结果。']
    if summary.get('stopped_early'):
        lines.append('- 模型认证或账户额度错误使批次提前停止；未运行项目不计为成功。')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--comparison', choices=('diagnostic-pack', 'review-protection'), default='diagnostic-pack')
    parser.add_argument('--cases', nargs='+', choices=KNOWN)
    parser.add_argument('--model-budget', type=int)
    parser.add_argument('--tool-budget', type=int)
    parser.add_argument('--input-chars', type=int)
    parser.add_argument('--output-tokens', type=int, default=3200)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/diagnostic-pack-validation')
    args = parser.parse_args(argv)
    if args.execute_live and (not args.cases or any(value is None for value in
            (args.model_budget, args.tool_budget, args.input_chars))):
        parser.error('真实调用需要明确场景、模型预算、工具预算和输入上限')
    chosen = args.cases or list(KNOWN)
    if len(chosen) != len(set(chosen)):
        parser.error('场景不能重复')
    limits = Limits(model_calls=args.model_budget or 48, tool_calls=args.tool_budget or 64,
                    input_chars=args.input_chars or 56000, seconds=args.seconds,
                    reserve_model_calls=3, reserve_seconds=20)
    if not (6 <= limits.model_calls <= 64 and 2 <= limits.tool_calls <= 96
            and 28000 <= limits.input_chars <= 112000 and 2200 <= args.output_tokens <= 8000
            and 180 <= limits.seconds <= 900):
        parser.error('预算超出本验证允许范围')
    config = IncidentConfig.from_file()
    selected = [next(case for case in cases() if case['case_id'] == cid) for cid in chosen]
    # Preflight every scope/pack and declared read before the first model call.
    required = {}
    for case in selected:
        provider = SnapshotProvider(case['snapshot'])
        provider.authorize(P, provider.ticket().scope)
        case['pack'].assert_scope(provider.ticket())
        executor = ToolExecutor(provider, P, provider.ticket().scope, RunContext(scope=provider.ticket().scope, limits=limits))
        window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
        required[case['case_id']] = [{'tool': check.tool,
            'args': executor.validate_args(check.tool, {**window, **check.args}).model_dump(mode='json')}
            for check in case['pack'].checks]
    source = implementation_version()
    configuration = {'model': config.model, 'limits': asdict(limits), 'max_output_tokens': args.output_tokens,
                     'comparison': args.comparison,
                     'temperature': 0, 'request_timeout': 30, 'max_retries': 0,
                     'transport_extra_body_sha256': digest(config.extra_body)}
    plan = {'method': 'paired_live_review_protection_v1' if args.comparison == 'review-protection' else 'paired_live_diagnostic_pack_replay_v1', 'source_version': source,
            'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'configuration': configuration, 'plan_only': not args.execute_live,
            'attempts_per_case_and_arm': 1, 'paired_runs': 2 * len(selected),
            'maximum_model_calls': 2 * len(selected) * limits.model_calls,
            'maximum_tool_calls': 2 * len(selected) * limits.tool_calls,
            'estimated_cost': None, 'criteria_sent_to_models': False, 'independent_quality_benchmark': False,
            'cases': [{'case_id': case['case_id'], 'fixture_sha256': digest(case['snapshot']),
                       'pack_fingerprint': case['pack'].fingerprint} for case in selected]}
    if not args.execute_live:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not config.api_key or config.api_key == 'test-only':
        parser.error('未配置真实模型密钥，未发起模型请求')
    directory = args.output / str(uuid4())
    write_json(directory / 'plan.json', plan)
    for case in selected:
        write_json(directory / (case['case_id'] + '.fixture.json'), case['snapshot'])
    summary = {**plan, 'runs': [], 'stopped_early': False}
    write_json(directory / 'summary.json', summary)
    print(json.dumps({'directory': str(directory), 'model': config.model, 'planned_runs': plan['paired_runs']}, ensure_ascii=False), flush=True)
    for index, case in enumerate(selected):
        # Alternate order to reduce a uniform first/second-arm timing effect.
        arms = ('strict', 'conservative') if args.comparison == 'review-protection' else ('baseline', 'pack')
        for arm in (arms if index % 2 == 0 else tuple(reversed(arms))):
            if implementation_version() != source:
                raise RuntimeError('验证期间应用源码发生变化，停止后续调用')
            print(json.dumps({'starting': label(case['case_id']), 'arm': arm_label(arm)}, ensure_ascii=False), flush=True)
            provider = SnapshotProvider(case['snapshot'])
            provider.name = 'diagnostic_pack_live_synthetic_replay'
            factory = lambda _: build_roles(config.api_key, config.model, base_url=config.base_url,
                extra_body=config.extra_body, max_output_tokens=args.output_tokens)
            report = capture(case['case_id'], 'multi', factory, P, limits,
                provider_factory=lambda cid, principal: provider,
                diagnostic_pack=case['pack'] if arm != 'baseline' else None,
                review_protection='strict_v1' if arm == 'strict' else 'conservative_v1')
            if report['task_type'] == 'incident_multi':
                # capture's generic exception report uses a workflow alias;
                # this runner is exclusively the collaboration workflow.
                report['task_type'] = 'incident_collaboration'
                report.setdefault('task_results', [])
                report.setdefault('negotiation', [])
            report.update(source_version=source, execution_mode='live', model=config.model,
                          experiment_arm=arm, experiment_case=case['case_id'],
                          fixture_snapshot_sha256=digest(case['snapshot']), model_settings=configuration,
                          provenance={'dataset_version': 'diagnostic_pack_known_development_replay_v1',
                                      'configuration_sha256': digest(configuration),
                                      'runner_sha256': plan['runner_sha256']})
            path = directory / (case['case_id'] + '-' + arm + '.json')
            write_json(path, report)  # Retain the full run before scoring/rendering.
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
            row = {**summarize_report(report, required[case['case_id']], provider, limits),
                   'case_id': case['case_id'], 'arm': arm, 'run_id': report['run_id'],
                   'report_file': path.name, 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            summary['runs'].append(row)
            if (row['termination_reason'] in {'AUTH_ERROR', 'QUOTA_EXCEEDED'} or
                    any(error.get('code') in {'AUTH_ERROR', 'QUOTA_EXCEEDED'} for error in report['run_summary']['errors'])):
                summary['stopped_early'] = True
            write_json(directory / 'summary.json', summary)
            (directory / 'summary.md').write_text(markdown(summary), encoding='utf-8')
            print(json.dumps({'finished': label(case['case_id']), 'arm': arm_label(arm),
                              'status': label(row['status']), 'model_calls': row['model_calls'], 'tool_calls': row['tool_calls']}, ensure_ascii=False), flush=True)
            if summary['stopped_early']:
                print(directory / 'summary.md', flush=True)
                return 2
    if implementation_version() != source:
        raise RuntimeError('最终源码指纹与执行计划不一致')
    print(directory / 'summary.md', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
