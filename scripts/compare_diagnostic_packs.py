"""Free planning by default; explicitly execute mechanical synthetic controls only."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from evals.diagnostic_packs.experiment import cases, digest, run_controls, METHOD
from evals.common import write_json, implementation_version


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-control', action='store_true')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/diagnostic-pack-experiment')
    args = parser.parse_args(argv)
    selected = cases()
    if args.cases:
        names = {r['case_id'] for r in selected}
        if len(set(args.cases)) != len(args.cases) or not set(args.cases) <= names:
            parser.error('Unknown or duplicate controlled case')
        selected = [r for r in selected if r['case_id'] in args.cases]
    source = implementation_version()
    plan = {'plan_only': not args.execute_control, 'method': METHOD, 'source_version': source,
            'cases': [{ 'case_id': r['case_id'], 'fixture_sha256': digest(r['snapshot']),
                'pack_fingerprint': r['pack'].fingerprint if r['pack'] else None,
                'baseline': r['baseline'], 'tool_budget': r.get('tool_budget', 8)} for r in selected],
            'paired_runs': 2 * len(selected), 'paid_model_calls': 0,
            'semantic_quality': 'excluded_scripted',
            'note': '只执行合成取证控制；没有live选项，不读取模型密钥或真实服务登记。'}
    if not args.execute_control:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    directory = args.output / str(uuid4())
    write_json(directory / 'plan.json', plan)
    for item in selected:
        write_json(directory / (item['case_id'] + '.fixture.json'), item['snapshot'])
    def save_run(report, row):
        report['source_version'] = source
        name = report['experiment_case'] + '-' + report['experiment_arm'] + '.json'
        write_json(directory / name, report)
        row.update(report_file=name, report_sha256=hashlib.sha256((directory / name).read_bytes()).hexdigest())
    result, reports = run_controls(selected, on_result=save_run)
    if implementation_version() != source:
        raise RuntimeError('Application changed during controlled experiment')
    result['source_version'] = source
    result['registration_sha256'] = hashlib.sha256((ROOT / 'examples/diagnostic-packs.example.json').read_bytes()).hexdigest()
    write_json(directory / 'summary.json', result)
    lines = ['# 诊断包受控取证实验', '',
             f"源码：{source}；{result['cases']} 个控制场景、{result['runs']} 次图运行；付费调用 0。", '',
             '| 场景 | 基线检查状态 | 诊断包检查状态 | 额外工具调用 | 诊断包状态 | 输入门禁（基线/包） |',
             '| --- | --- | --- | --- | --- | --- |']
    for pair in result['pairs']:
        lines.append(f"| {pair['case_id']} | {pair['baseline']['query_statuses']} | {pair['pack']['query_statuses']} | {pair['extra_tool_calls']} | {pair['pack']['pack_status']} | {pair['baseline']['input_budget_rejections']}/{pair['pack']['input_budget_rejections']} |")
    lines += ['', '## 解释限制', ''] + ['- ' + text for text in result['limitations']]
    (directory / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(directory / 'summary.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
