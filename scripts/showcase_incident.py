"""One free showcase: actual graph, scripted models, fixtures and existing controls.

No live switch, dotenv, credentials, database or container operations. Model-call
counts below count scripted invocations, not paid requests or reasoning quality.
"""
import argparse
from contextlib import redirect_stdout
import hashlib
import html
import io
import json
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from agents.scripted import scripted_models
from evidence.render import render
from evals.common import implementation_version, write_json
from evals.incident.comparison import score_report
from runtime.context import Limits
from scripts.compare_incident import capture, main as compare_main
from scripts.demo_remediation import main as remediation_main


CONTROLS = {
    'scope': [
        'tests.tools.test_incident_tools.IncidentToolsTests.test_role_unknown_tools_and_extra_fields',
        'tests.tools.test_incident_tools.IncidentToolsTests.test_window_timezone_service_and_scope_expansion'],
    'references': [
        'tests.workflow.test_diagnosis_references.DiagnosisReferenceTests.test_persistent_invalid_reference_stops_after_one_repair_with_public_error_location'],
    'structure': [
        'tests.workflow.test_incident_collaboration.IncidentCollaborationTests.test_schema_repair_allowance_is_shared_across_leaf_and_reviewer'],
    'retry_and_dedup': [
        'tests.tools.test_incident_tools.IncidentToolsTests.test_retry_count_error_sanitization_and_limit',
        'tests.tools.test_incident_tools.IncidentToolsTests.test_duplicate_no_double_count_and_snapshot_immutability'],
    'budget': [
        'tests.workflow.test_incident_collaboration.IncidentCollaborationTests.test_small_budget_keeps_review_and_refuses_unaffordable_rework'],
}


def run_controls():
    """Run the existing boundary tests once and retain actual results per test."""
    rows = []
    for category, names in CONTROLS.items():
        for name in names:
            stream = io.StringIO()
            suite = unittest.defaultTestLoader.loadTestsFromName(name)
            result = unittest.TextTestRunner(stream=stream).run(suite)
            rows.append({'category': category, 'test': name, 'passed': result.wasSuccessful(),
                         'tests_run': result.testsRun})
            if not result.wasSuccessful():
                print(stream.getvalue(), file=sys.stderr)
    return rows


def project_demo(report):
    """Summary for this synthetic showcase; refuse real-source/model reports."""
    if report.get('execution_mode') != 'scripted_control_only' or report.get('data_source') != 'synthetic_fixture':
        raise ValueError('Only scripted fixture demonstrations may enter this export')
    summary = report['run_summary']
    event_keys = ('seq', 'type', 'role', 'node', 'name', 'tool', 'action', 'status', 'reason',
                  'code', 'revision', 'challenge_id', 'query_profile', 'evidence_ids')
    return {
        'run_id': report['run_id'], 'case_id': report['incident_id'],
        'execution_mode': report['execution_mode'], 'data_source': report['data_source'],
        'status': report['status'], 'review_status': report['review_status'],
        'model_calls': summary['model_calls'], 'tool_calls': summary['tool_calls'],
        'limits': summary['limits'], 'termination_reason': summary['termination_reason'],
        'rework_rounds': report['rework_rounds'], 'structural_repairs': report['repairs'],
        'evidence_count': len(report['evidence']),
        'tasks': [{k: task.get(k) for k in ('role', 'goal', 'status', 'challenge_id', 'model_calls', 'tool_calls')}
                  for task in report['task_results']],
        'hypotheses': [{'cause': row['cause'], 'status': row['status'],
                        'support_evidence_ids': sorted({r['evidence_id'] for r in row['support_refs']})}
                       for row in report['output']['hypotheses']],
        'missing_information': report['output']['missing_information'],
        'events': [{k: event[k] for k in event_keys if k in event} for event in report['events']
                   if event['type'] not in ('call_start', 'call_end', 'model_output')],
    }


def render_page(summary):
    sections = []
    for demo in summary['demonstrations']:
        trail = html.escape(json.dumps(demo, ensure_ascii=False, indent=2))
        sections.append(f"<section><h2>{html.escape(demo['name'])}</h2>"
                        f"<p>{demo['status']} / {demo['review_status']} · 模拟模型 {demo['model_calls']} 次 / 工具 {demo['tool_calls']} 次"
                        f" · {demo['termination_reason']}</p>"
                        f"<p><a href=\"{html.escape(demo.get('key', 'report'))}.md\">诊断报告</a> · "
                        f"<a href=\"{html.escape(demo.get('key', 'report'))}.json\">完整合成证据与轨迹</a></p>"
                        f"<details><summary>任务、证据引用与节点轨迹</summary>"
                        f"<pre>{trail}</pre></details></section>")
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Agent 编排与 Harness 演示</title>'
            '<style>body{max-width:1000px;margin:40px auto;padding:0 20px;font:16px/1.65 system-ui;color:#182b3a}'
            'section{padding:16px 24px;margin:20px 0;background:#f3f6f8;border-radius:12px}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer}</style>'
            '<h1>Agent 编排与 Harness 离线演示</h1><p>真实 LangGraph 控制流＋脚本模型＋合成观测。'
            '付费调用 0，真实修复写操作 0；不评价模型推理质量。预算不足示例的 partial 是预期结果。</p>'
            + ''.join(sections)
            + '<p>summary.json 包含机械检查、同预算模拟对照、五类约束测试和模拟审批闭环。'
            '真实业务案例及历史模型对照见 examples/showcase/README.md。</p></html>')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/showcase')
    args = parser.parse_args(argv)
    directory = args.output.resolve() / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    principal = Principal('synthetic_demo', 'cli_reader')
    scenarios = [
        ('normal', '正常诊断', 'case_001', 8, 8),
        ('rework', '复核触发补查', 'case_003', 16, 8),
        ('bounded_failure', '预算不足受控退出', 'case_003', 8, 5),
    ]
    demos = []
    for key, name, case, model_budget, tool_budget in scenarios:
        limits = Limits(model_calls=model_budget, tool_calls=tool_budget, reserve_model_calls=3, reserve_seconds=20)
        report = {**capture(case, 'multi', lambda _: scripted_models(), principal, limits),
                  'execution_mode': 'scripted_control_only', 'model': 'scripted'}
        write_json(directory / f'{key}.json', report)
        (directory / f'{key}.md').write_text(render(report), encoding='utf-8')
        # Gold is evaluator-only and is loaded after the workflow has finished.
        gold = json.loads((ROOT / 'evals/incident/gold' / f'{case}.json').read_text(encoding='utf-8'))
        checks = score_report(report, gold)
        if key == 'bounded_failure':
            expected = report['status'] == 'partial' and report['run_summary']['termination_reason'] == 'BUDGET_EXCEEDED' and report['rework_rounds'] == 0
        else:
            expected = report['status'] == 'completed' and report['review_status'] == 'passed' and report['rework_rounds'] == (1 if key == 'rework' else 0)
        if key == 'rework':
            expected = expected and report['task_results'][-1]['tool_calls'] > 0 and len(report['reviews']) == 2
        demos.append({**project_demo(report), 'key': key, 'name': name,
                      'report_sha256': hashlib.sha256((directory / f'{key}.json').read_bytes()).hexdigest(),
                      'mechanical_checks': checks['mechanical_checks'], 'expected_behavior_pass': bool(expected)})
    output = io.StringIO()
    with redirect_stdout(output):
        compare_status = compare_main(['--fake', '--model-budget', '16', '--tool-budget', '8', '--output', str(directory / 'comparison')])
    comparison = json.loads(output.getvalue())
    output = io.StringIO()
    with redirect_stdout(output):
        remediation_main()
    remediation = json.loads(output.getvalue())
    controls = run_controls()
    passed = (all(d['expected_behavior_pass'] and all(d['mechanical_checks'].values()) for d in demos)
              and compare_status == 0 and all(c['passed'] for c in controls)
              and remediation['status'] == 'verified' and remediation['simulated_writes'] == 1)
    summary = {'schema_version': 'showcase-v1', 'execution_mode': 'scripted_control_only',
               'business_quality_status': 'excluded_scripted', 'paid_calls': 0, 'live_writes': 0,
               'implementation_sha256_prefix': implementation_version(),
               'passed': passed, 'demonstrations': demos, 'comparison': comparison,
               'harness_controls': controls, 'simulated_remediation': remediation}
    write_json(directory / 'summary.json', summary)
    (directory / 'index.html').write_text(render_page(summary), encoding='utf-8')
    print(json.dumps({'directory': str(directory), 'passed': passed, 'demonstrations': [
        {k: d[k] for k in ('key', 'status', 'review_status', 'model_calls', 'tool_calls', 'termination_reason', 'expected_behavior_pass')}
        for d in demos], 'controls_passed': sum(c['passed'] for c in controls), 'controls_total': len(controls),
        'comparison_quality': comparison['quality_status'], 'paid_calls': 0, 'live_writes': 0}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
