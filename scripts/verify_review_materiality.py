"""Bounded synthetic probes: real Reviewer, fixed planner/draft, no gold in inputs.

These isolate review behavior; they are not end-to-end accuracy measurements.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from api.auth import Principal
from agents.models import build_roles
from agents.scripted import scripted_models
from config.incident import IncidentConfig
from evals.common import write_json
from langchain_core.messages import AIMessage
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture

CASES = ('neutral_check', 'presupposed_check_result', 'unconditional_change', 'conditional_change')


class ProbeProvider(FixtureProvider):
    name = 'review_materiality_new_synthetic_probe'

    def __init__(self, case_id, principal):
        super().__init__('case_001', principal)
        ticket = self._data['ticket']
        ticket.update(incident_id=case_id, title='合成对象写入失败',
                      symptoms='对象写入接口错误率升高，日志记录对象存储返回流控拒绝。')
        ticket['scope'].update(service='artifact-writer', allowed_dependencies=['blob-store'])
        sample = dict(self._data['logs'][0])
        sample.update(id='write-rejected', service='artifact-writer', category='dependency',
            level='WARN', error_code='RATE_LIMITED',
            message='本次对象写入调用DNS解析与TLS握手成功；blob-store返回HTTP 503及RATE_LIMITED、Retry-After=60；未成功写入。未取得远端配额或变更记录。')
        self._data['logs'] = [sample]
        self._data['metrics'] = [row for row in self._data['metrics']
                                 if row['metric'] == 'request_error_rate']
        for row in self._data['metrics']:
            row['service'] = 'artifact-writer'
        for field in ('changes', 'owners', 'runbooks', 'incidents'):
            self._data[field] = []


class FixedRole:
    def __init__(self, role, case):
        self.role, self.case = role, case

    def invoke(self, messages):
        view = json.loads(messages[1].content)['input']
        if self.role == 'supervisor':
            window = {k: view['ticket']['scope'][k] for k in ('start', 'end')}
            if view.get('pending_challenge'):
                data = {'action': 'request_info', 'reason': '本探针不进行额外采集，保留复核缺口供人工核实', 'tasks': []}
            elif view['evidence']:
                data = {'action': 'diagnose', 'reason': '已取得当前日志和症状指标，交独立复核', 'tasks': []}
            else:
                data = {'action': 'dispatch', 'reason': '取得失败环节与症状', 'tasks': [{
                    'role': 'investigation', 'goal': '核对当前调用结果与错误率',
                    'need_ids': ['N1', 'N2', 'N3'], 'expected_value': '区分调用失败环节', 'checks': [
                        {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}},
                        {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['request_error_rate']}}]}]}
        else:
            log = next(e for e in view['evidence'] if 'message' in e['payload'])
            ref = {'reference_id': next(r['reference_id'] for r in log['reference_options']
                                        if r['field_path'] == 'message')}
            action = {'kind': 'manual_check', 'action': '人工联系对象存储维护方核对流控状态和近期配额记录',
                'condition': '已观测到对象写入调用被流控拒绝',
                'expected_result': '取得流控状态和配额记录，区分临时流控与持续配额限制',
                'risk': '只读核实，无配置修改', 'requires_approval': False, 'checks': [], 'repair': None}
            if self.case == 'presupposed_check_result':
                action['expected_result'] = '确认维护方降低了配额并取得预计恢复时间'
            elif self.case == 'unconditional_change':
                action.update(kind='manual_change', action='要求维护方立即恢复被下调的配额',
                    condition='已观测到HTTP 503', expected_result='恢复原配额后写入立即恢复',
                    risk='修改配额可能影响其他租户', requires_approval=True)
            elif self.case == 'conditional_change':
                action.update(kind='manual_change', action='经维护方确认并批准后恢复原有配额',
                    condition='仅当维护方记录证实配额被下调且流控由此引起，核对原配额并取得变更审批后实施',
                    expected_result='若流控由已核实的配额下降引起，恢复后拒绝应减少；观察错误率，未下降则重新排查',
                    risk='修改配额可能影响其他租户，须审批并可回滚', requires_approval=True)
            data = {'findings': [{'statement': '当前写入调用被拒绝', 'refs': [ref]}],
                'hypotheses': [{'cause': '本次对象写入调用收到远端流控拒绝，未成功写入',
                    'level': 'mechanism', 'support_refs': [ref]},
                    {'cause': '维护方下调配额触发本次流控', 'level': 'trigger',
                     'support_refs': [], 'pending_checks': ['核对远端配额变更记录']}],
                'recommended_actions': [action], 'missing_information': ['尚未取得远端配额与变更记录']}
        return AIMessage(content=json.dumps(data, ensure_ascii=False))


class RecordedReviewer:
    """Retain synthetic replies, including rejected ones, without transport data."""
    def __init__(self, model):
        self.model, self.replies = model, []
        self.json_output = getattr(model, 'json_output', False)

    def invoke(self, messages):
        response = self.model.invoke(messages)
        self.replies.append(response.content)
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--fake', action='store_true')
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    args = parser.parse_args()
    if len(args.cases) != len(set(args.cases)):
        parser.error('Duplicate cases')
    if args.fake and args.execute_live:
        parser.error('Choose fake or live')
    config = IncidentConfig.from_file()
    source = implementation_version()
    limits = Limits(model_calls=8, tool_calls=4, seconds=120)
    principal = Principal('synthetic_demo', 'cli_reader')
    fixtures = {case: ProbeProvider(case, principal)._data for case in args.cases}
    plan = {'source_version': source, 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'fixture_sha256': hashlib.sha256(json.dumps(fixtures, sort_keys=True).encode()).hexdigest(),
        'model': config.model, 'cases': args.cases, 'limits': asdict(limits),
        'attempts_per_case': 1, 'real_roles': ['reviewer'], 'read_only_provider': True,
        'estimated_cost': None, 'maximum_real_model_calls': len(args.cases) * 3}
    if not args.fake and not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return
    directory = ROOT / 'output/review-materiality' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    write_json(directory / 'fixtures.json', fixtures)
    rows = []
    for case in args.cases:
        assert implementation_version() == source
        models = scripted_models() if args.fake else build_roles(config.api_key, config.model,
            base_url=config.base_url, extra_body=config.extra_body)
        reviewer = RecordedReviewer(models['reviewer'])
        models['reviewer'] = reviewer
        models.update(supervisor=FixedRole('supervisor', case), diagnosis=FixedRole('diagnosis', case))
        report = capture(case, 'multi', lambda flow: models, principal, limits, provider_factory=ProbeProvider)
        assert implementation_version() == source
        report.update(source_version=source, model=config.model, real_roles=[] if args.fake else ['reviewer'])
        path = directory / f'{case}.json'
        write_json(path, report)
        write_json(directory / f'{case}-synthetic-replies.json', reviewer.replies)
        row = {'case_id': case, 'status': report['status'], 'termination_reason': report['run_summary']['termination_reason'],
            'real_model_calls': 0 if args.fake else len(reviewer.replies),
            'report_file': path.relative_to(ROOT).as_posix(), 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'reviews': [r['review']['assessments'] for r in report.get('reviews', [])],
            'validation_failures': [{k: e.get(k) for k in ('role', 'reason', 'details')}
                                    for e in report['events'] if e['type'] == 'validation_failure']}
        rows.append(row)
        write_json(directory / 'summary.json', rows)
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
