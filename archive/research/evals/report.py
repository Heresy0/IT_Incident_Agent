"""Rebuild an auditable report from captured samples and separately signed reviews."""
import argparse
import json
from pathlib import Path
from evals.metrics import compare, gate, summarize
from evals.scoring import SCORER_VERSION, apply_reviews


def validate(artifact):
    if artifact.get('schema_version') != 2 or artifact.get('scorer_version') != SCORER_VERSION:
        raise ValueError('Unsupported evaluation schema/scorer')
    cases, repeats = artifact['selected_cases'], artifact['repeats']
    if not cases or len(cases) != len(set(cases)) or type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError('Invalid case/repeat manifest')
    expected = {(case, attempt) for case in cases for attempt in range(1, repeats + 1)}
    rows = artifact['results']
    actual = [(r['case']['id'], r['attempt']) for r in rows]
    if len(actual) != len(set(actual)) or not set(actual).issubset(expected):
        raise ValueError('Duplicate or unexpected evaluation sample')
    if len({r['run_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate run IDs')
    if artifact['expected_run_count'] != len(expected) or artifact['complete'] != (set(actual) == expected):
        raise ValueError('Completeness does not match manifest')
    for row in rows:
        for key in ('model', 'model_config', 'workflow_version', 'dataset_version', 'fixture_version', 'prompt_version'):
            if row.get(key) != artifact.get(key):
                raise ValueError('Sample metadata mismatch: ' + key)
        if row.get('implementation_version') != artifact.get('implementation_version'):
            raise ValueError('Sample implementation version mismatch')
    return artifact


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def ratio(value):
    return '未测/未复核' if value is None else f'{value:.1%}'


def generate(artifact, *, reviews=None, baseline=None, baseline_reviews=None, output=None, policy=None):
    validate(artifact)
    rows = apply_reviews(artifact, reviews)
    metrics = summarize(rows)
    old_metrics = None
    if baseline is not None:
        validate(baseline)
        compare(artifact, baseline)
        old_metrics = summarize(apply_reviews(baseline, baseline_reviews))
        if old_metrics['review_completion_rate'] != 1 or metrics['review_completion_rate'] != 1:
            raise ValueError('Baseline quality comparisons require complete reviews on both sides')
        if old_metrics['reviewer_kinds'] != metrics['reviewer_kinds']:
            raise ValueError('Baseline reviewer provenance differs')
    decision = gate(metrics, old_metrics, policy)
    if not artifact['complete']:
        decision['decision'] = 'incomplete'
        decision['failures'].append('Captured samples do not cover the declared manifest')
    lines = ['# 评估报告', '', f"评估 ID：`{artifact['evaluation_id']}`。模型：`{artifact['model']}`。工作流：`{artifact['workflow_version']}`。",
             f"数据集 / 资料 / 提示词 / 评分器：`{artifact['dataset_version']}` / `{artifact['fixture_version']}` / `{artifact['prompt_version']}` / `{artifact['scorer_version']}`。",
             '', '范围：' + artifact['scope'], '',
             f"生产工作流代码哈希：`{artifact.get('implementation_version', '此批未记录；见交付说明的源代码提交')}`。",
             f"已采集 **{len(rows)}/{artifact['expected_run_count']}** 次，{metrics['case_count']} 个案例，每例计划 {artifact['repeats']} 次。语义复核 **{metrics['reviewed_count']}/{len(rows)}**；复核者类型：{metrics['reviewer_kinds']}。",
             '语义通过率的分母仅为已复核次数；不代表人工复核、生产数据效果或网络搜索质量。关键词命中只作诊断，不作为语义通过依据。', '',
             '| 指标 | 当前结果 |', '| --- | --- |']
    for name, key in [('程序校验通过率','automated_pass_rate'), ('已复核样本语义通过率','semantic_pass_rate'),
                      ('标准答案覆盖率（宏平均）','requirement_coverage'), ('结论支持率（逐报告宏平均）','claim_support_rate'),
                      ('引用 ID 合法率（微平均）','citation_id_validity'), ('执行失败率','execution_failure_rate'),
                      ('存在运行错误的次数比例（含可降级错误）','run_error_rate'),
                      ('重复状态一致率','repeat_status_agreement'), ('重复语义判定一致率','repeat_quality_agreement'),
                      ('所有重复均通过的案例比例','all_repeats_pass_rate')]:
        lines.append(f'| {name} | {ratio(metrics[key])} |')
    duration = metrics['duration_ms']
    lines += ['', f"耗时毫秒（mean/p50/p95/max）：{duration}；平均模型调用：{metrics['mean_model_calls']}。",
              f"已知输入/输出 Token：{metrics['known_input_tokens']}/{metrics['known_output_tokens']}；用量不完整次数：{metrics['partial_token_usage_runs']}。费用未估算。",
              f"运行状态：{metrics['status_counts']}；终止原因：{metrics['termination_reasons']}；错误类型：{metrics['error_counts']}。", '',
              f"回归门禁：**{decision['decision']}**。阈值是项目的初始验收约定，不是行业标准。",
              '失败项：' + cell(decision['failures']), '待完成项：' + cell(decision['pending']),
              '阈值：`' + json.dumps(decision['policy'], ensure_ascii=False) + '`', '',
              '## 分组结果', '', '| 分组 | 次数 | 程序通过 | 语义复核次数 | 语义通过 | 标准答案覆盖 |', '| --- | --- | --- | --- | --- | --- |']
    groups = {}
    for row in rows:
        for group in [row['case']['split'], *row['case']['tags']]:
            groups.setdefault(group, []).append(row)
    for group, samples in sorted(groups.items()):
        m = summarize(samples)
        lines.append(f"| {cell(group)} | {len(samples)} | {ratio(m['automated_pass_rate'])} | {m['reviewed_count']} | {ratio(m['semantic_pass_rate'])} | {ratio(m['requirement_coverage'])} |")
    lines += ['', '## 每次运行', '', '| 案例 / 重复 | 状态 | 程序校验 | 语义判定 | 问题 |', '| --- | --- | --- | --- | --- |']
    for row in rows:
        g = row['grading']
        issues = list(g['violations']) + [e['code'] for e in row['summary'].get('errors', [])]
        if row.get('evaluation_error_type'):
            issues.append(row['evaluation_error_type'])
        review = g.get('review', {})
        issues += [f"{q['id']}: {q['verdict']} ({q['reason']})" for q in review.get('requirements', []) if q['verdict'] != 'met']
        issues += [f"{q['claim_id']}: {q['verdict']} ({q['reason']})" for q in review.get('claims', []) if q['verdict'] != 'supported']
        if review.get('notes'):
            issues.append(review['notes'])
        semantic = '未复核' if g['semantic_pass'] is None else '通过' if g['semantic_pass'] else '未通过'
        lines.append(f"| {row['case']['id']} / {row['attempt']} | {cell(row['status'])} | {'通过' if g['automated_pass'] else '未通过'} | {semantic} | {cell('; '.join(issues))} |")
    if old_metrics:
        lines += ['', '## 基线比较', '', f"基线评估 ID：`{baseline['evaluation_id']}`。双方案例、重复次数、资料与评分器相同。",
                  f"基线模型/工作流/提示词：`{baseline['model']}` / `{baseline['workflow_version']}` / `{baseline['prompt_version']}`。",
                  f"语义通过率：{ratio(old_metrics['semantic_pass_rate'])} → {ratio(metrics['semantic_pass_rate'])}。",
                  f"平均耗时：{old_metrics['duration_ms']['mean']} → {metrics['duration_ms']['mean']} ms。",
                  '模型、提示词或代码变化需结合两份原始记录说明；一次改善不能证明统计显著。']
    text = '\n'.join(lines) + '\n'
    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding='utf-8')
    return {'metrics':metrics, 'gate':decision, 'report':text}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--baseline-reviews', type=Path)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-gates', action='store_true')
    args = parser.parse_args()
    result = generate(read(args.artifact), reviews=read(args.reviews), baseline=read(args.baseline),
                      baseline_reviews=read(args.baseline_reviews), policy=read(args.policy),
                      output=args.output or args.artifact.with_name(args.artifact.stem+'.report.md'))
    print(json.dumps({'metrics':result['metrics'], 'gate':result['gate']}, ensure_ascii=False, indent=2))
    if args.check_gates:
        raise SystemExit({'passed':0,'failed':2,'needs_review':3,'incomplete':4}[result['gate']['decision']])


if __name__ == '__main__':
    main()
