"""Free interview demos through the actual graph. No live switch or credentials."""
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from evals.common import implementation_version, write_json
from evals.incident.comparison import score_report
from evals.targeted.provider import TargetedProvider
from evals.targeted.suite import verify_freeze
from evidence.render import render
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from scripts.compare_incident import capture
from scripts.interview_demo_models import interview_models


SCENARIOS = (
    {'key': 'history', 'case': 'probe_001', 'name': '历史经验核查',
     'question': '上周扩容有效，本次积压是否也应该直接扩容？',
     'capability': 'Knowledge 提供历史适用前提，Investigation 核查当前协议与负载，Diagnosis 区分历史线索与本次机制。',
     'watch': '历史扩容依赖CPU饱和；当前CPU低且远端400拒绝字段。查看H2被排除及H1的当前引用。'},
    {'key': 'scope', 'case': 'probe_002', 'name': '冲突证据与范围核查',
     'question': '健康面板绿色，为何业务仍失败？旧磁盘错误是否相关？',
     'capability': '当前请求、健康探针及旧实例分别核对；Reviewer 对候选原因和建议条件逐项评估。',
     'watch': 'metadata探针未覆盖写入；当前403与旧runner磁盘错误来自不同路径和范围。'},
    {'key': 'rework', 'case': 'case_003', 'name': '复核驱动的定向补查',
     'question': '初稿被历史数据库CPU故障误导，流程能否发现并修正？',
     'capability': 'Reviewer 质疑 → 精确只读检查 → Supervisor 有限派单 → 补查证据 → Diagnosis 修订 → 再复核。',
     'watch': '比较初稿和修订稿；一次补查有真实工具尝试和新增证据，不只是多写一段反思。'},
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_interview(report):
    """Accept only this demo's public synthetic sources and scripted responses."""
    allowed = {'synthetic_fixture', 'targeted_synthetic_fixture'}
    if (report.get('execution_mode') != 'scripted_control_only' or report.get('data_source') not in allowed
            or any(e.get('provider') not in allowed for e in report['evidence'])):
        raise ValueError('Interview export requires scripted public synthetic evidence')
    event_keys = {'seq', 'type', 'role', 'node', 'name', 'action', 'status', 'reason', 'code',
                  'revision', 'evidence_ids', 'target_id', 'hypothesis_id', 'query_profile'}
    counts = Counter(e['name'] for e in report['events'] if e['type'] == 'call_start' and e['kind'] == 'model')
    return {'run_id': report['run_id'], 'case_id': report['incident_id'], 'status': report['status'],
        'review_status': report['review_status'], 'termination_reason': report['run_summary']['termination_reason'],
        'model_steps': report['run_summary']['model_calls'], 'tool_attempts': report['run_summary']['tool_calls'],
        'execution_mode': 'scripted_control_only', 'quality_status': 'excluded_scripted',
        'role_steps': dict(counts), 'rework_rounds': report['rework_rounds'],
        'tasks': [{k: task.get(k) for k in ('role', 'goal', 'status', 'challenge_id', 'model_calls', 'tool_calls', 'evidence_ids')}
                  for task in report['task_results']],
        'drafts': report['drafts'], 'reviews': report['reviews'], 'output': report['output'],
        'events': [{k: v for k, v in event.items() if k in event_keys} for event in report['events']
                   if event['type'] not in {'call_start', 'call_end', 'model_output'}]}


def expected_behavior(report, key):
    checks = score_report(report, {'necessary_checks': []})['mechanical_checks']
    expected = {'reviewed_report': report['status'] == 'completed' and report['review_status'] == 'passed'}
    if key == 'history':
        expected.update(knowledge_retrieval=any(t['role'] == 'knowledge' and t['tool_calls'] > 0 for t in report['task_results']),
            rejected_historical_candidate=any(h['status'] == 'refuted' for h in report['output']['hypotheses']))
    elif key == 'scope':
        codes = {e['payload'].get('error_code') for e in report['evidence']}
        expected.update(conflicting_sources_preserved={'PROBE_OK', 'STORAGE_PERMISSION_DENIED', 'DISK_FULL'} <= codes,
            old_instance_candidate_rejected=any(h['status'] == 'refuted' for h in report['output']['hypotheses']))
    else:
        expected.update(one_rework=report['rework_rounds'] == 1,
            two_reviews=len(report['reviews']) == 2,
            changed_cause=len(report['drafts']) == 2 and
                report['drafts'][0]['draft']['hypotheses'][0]['cause'] != report['drafts'][1]['draft']['hypotheses'][0]['cause'],
            actual_rework_read=any(t.get('challenge_id') and t['tool_calls'] > 0 for t in report['task_results']))
    return checks, expected


def render_page(summary):
    esc = lambda value: html.escape(str(value), quote=True)
    footer = ('<footer>五个角色共享预算和证据目录，分别承担调度、取证、知识检索、诊断与复核。修复须走独立审批执行器。'
        '每条演示预算为16模型步骤/8工具尝试；完整记录可核对引用、计数及草稿版本。应用版本 '
        + esc(summary['implementation_sha256_prefix'])
        + '。<a href="summary.json">查看本批次验证记录</a></footer></main></body></html>')
    cards = []
    for demo in summary['demonstrations']:
        roles = ''.join(f'<span class="role">{esc(role)} · {count}</span>' for role, count in demo['role_steps'].items())
        hypotheses = ''.join(f'<li><span class="state">{esc(h["status"])}</span> {esc(h["cause"])}</li>'
                             for h in demo['output']['hypotheses'])
        draft_blocks = ''.join('<div class="revision"><b>草稿 ' + str(n + 1) + '</b><ul>' +
            ''.join('<li>' + esc(h['cause']) + '</li>' for h in draft['draft']['hypotheses']) + '</ul></div>'
            for n, draft in enumerate(demo['drafts']))
        review_blocks = ''.join('<p><b>复核 ' + str(n + 1) + '</b> ' +
            esc('；'.join(a['target_id'] + ' ' + a['verdict'] + '：' + a['reason'] for a in row['review']['assessments'])) +
            '</p>' + ('<p class="challenge">补查：' + esc(row['review']['request_evidence']['proposed_check']) + '</p>'
            if row['review'].get('request_evidence') else '') for n, row in enumerate(demo['reviews']))
        timeline = ''.join('<li><b>' + esc(event.get('role', 'workflow')) + '</b> ' +
            esc(event['type']) + ' ' + esc(event.get('name', event.get('action', event.get('code', '')))) + '</li>'
            for event in demo['events'] if event['type'] in {'phase', 'supervisor_decision', 'tool_result',
                'review_completed', 'rework_requested', 'workflow_completed'})
        cards.append(f'<section id="{esc(demo["key"])}"><div class="eyebrow">合成场景 · 脚本控制</div>'
            f'<h2>{esc(demo["name"])}</h2><p class="question">{esc(demo["question"])}</p>'
            f'<p>{esc(demo["capability"])}</p><p class="watch">讲解重点：{esc(demo["watch"])}</p>'
            f'<div>{roles}</div><p class="stats">{esc(demo["status"])} / {esc(demo["review_status"])}'
            f' · 脚本模型步骤 {demo["model_steps"]} · 工具尝试 {demo["tool_attempts"]} · 补查 {demo["rework_rounds"]} 轮</p>'
            f'<ul>{hypotheses}</ul><details><summary>诊断草稿与逐项复核</summary>{draft_blocks}{review_blocks}</details>'
            f'<details><summary>实际节点与工具轨迹</summary><ol>{timeline}</ol></details>'
            f'<p class="links"><a href="{esc(demo["key"])}.md">诊断报告</a> · <a href="{esc(demo["key"])}.json">完整合成运行记录</a></p></section>')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IT 故障 Agent · 面试演示</title><style>
*{box-sizing:border-box}body{margin:0;background:#f0f4f8;color:#183149;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1040px;margin:auto;padding:38px 24px 60px}header{padding:30px;background:#112d43;color:#fff;border-radius:18px}
h1{font-size:32px;margin:5px 0 14px}h2{font-size:24px;margin:4px 0 10px}p{margin:12px 0}.eyebrow{font-size:13px;letter-spacing:1px;color:#478580}
header .eyebrow{color:#87d6c6}.notice{background:#e3f3ee;border-left:4px solid #218879;padding:14px 20px;margin:20px 0;border-radius:6px}
nav{display:flex;gap:18px;flex-wrap:wrap;margin:18px 0}a{color:#087b72;text-decoration:underline;text-underline-offset:3px}
section{background:#fff;border:1px solid #dce5ed;border-radius:15px;padding:26px;margin:24px 0}.question{font-size:19px;font-weight:600}
.watch{padding:12px 16px;background:#f3f7fb;border-radius:8px}.role{display:inline-block;font-size:13px;background:#e8eef5;border-radius:20px;padding:3px 12px;margin:3px 4px 3px 0}
.state{font:12px monospace;color:#176a61;background:#e3f3ee;padding:3px 7px;border-radius:4px}.stats,.links{font-size:14px;color:#526779}
details{border-top:1px solid #e4ebf0;padding:12px 0}summary{cursor:pointer;font-weight:600}.revision{background:#f6f8fb;padding:10px 15px;margin-top:12px;border-radius:6px}
.challenge{border-left:3px solid #bc8a26;padding-left:12px}li{margin:5px 0}footer{font-size:14px;color:#526779}
@media(max-width:600px){main{padding:16px}header,section{padding:20px}h1{font-size:25px}}
</style></head><body><main><header><div class="eyebrow">LANGGRAPH × HARNESS</div><h1>IT 故障协作诊断</h1>
<p>三个可复现的开发演示：从历史线索、冲突证据，到复核驱动的补查。</p></header>
<div class="notice"><b>演示类型：真实工作流 + 脚本角色响应 + 合成观测。</b><br>所有选查、原因候选与复核意见均按演示脚本预设；展示工程协作机制，不是自主模型成绩。
付费调用 0，真实修复写操作 0。单 Agent 也能查询这些来源，本页不提供准确率或胜率结论。</div>
<nav><a href="#history">历史经验核查</a><a href="#scope">冲突证据</a><a href="#rework">复核补查</a></nav>''' + ''.join(cards) + footer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/interview-demo')
    args = parser.parse_args(argv)
    verify_freeze()  # Hash verification only; no answer content is loaded.
    directory = args.output.resolve() / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    limits = Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20)
    principal = Principal('interview_synthetic_demo', 'cli_reader')
    demos = []
    for scenario in SCENARIOS:
        provider = FixtureProvider if scenario['key'] == 'rework' else TargetedProvider
        report = {**capture(scenario['case'], 'multi', lambda _: interview_models(scenario['key']),
            principal, limits, provider_factory=provider), 'execution_mode': 'scripted_control_only',
            'model': 'authored_interview_control', 'quality_status': 'excluded_scripted',
            'provenance': {'implementation_sha256_prefix': implementation_version(),
                'fixture_sha256': digest(ROOT / ('fixtures/incident' if scenario['key'] == 'rework'
                    else 'fixtures/incident_targeted') / (scenario['case'] + '.json'))}}
        path = directory / (scenario['key'] + '.json')
        write_json(path, report)
        path.with_suffix('.md').write_text(render(report), encoding='utf-8')
        mechanical, behavior = expected_behavior(report, scenario['key'])
        demos.append({**scenario, **project_interview(report), 'mechanical_checks': mechanical,
            'expected_behavior': behavior, 'report_sha256': digest(path)})
    passed = all(all(d['mechanical_checks'].values()) and all(d['expected_behavior'].values()) for d in demos)
    summary = {'schema_version': 'interview-demo-v1', 'execution_mode': 'scripted_control_only',
        'quality_status': 'excluded_scripted', 'paid_calls': 0, 'real_business_writes': 0,
        'implementation_sha256_prefix': implementation_version(), 'passed': passed, 'demonstrations': demos}
    write_json(directory / 'summary.json', summary)
    (directory / 'index.html').write_text(render_page(summary), encoding='utf-8')
    print(json.dumps({'directory': str(directory), 'passed': passed, 'paid_calls': 0,
        'demonstrations': [{k: d[k] for k in ('key', 'status', 'model_steps', 'tool_attempts', 'rework_rounds', 'expected_behavior')}
                           for d in demos]}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
