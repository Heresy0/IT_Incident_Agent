"""Bounded paired live runs plus a labeled seeded editing probe; default is plan only."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from threading import Lock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from langchain_core.messages import AIMessage
from agents.models import build_roles
from agents.scripted import scripted_models
from api.auth import Principal
from config.incident import IncidentConfig
from evidence.render import render
from evals.common import write_json
from providers.fixtures import FixtureProvider
from runtime.context import ExecutionError, Limits
from runtime.version import implementation_version
from scripts.compare_incident import capture
from scripts.run_fresh_validation import FreshProvider


LIMITS = Limits(model_calls=12, tool_calls=8, seconds=300, reserve_model_calls=2, reserve_seconds=20)
PAID_CAP = 50
CASES = ('fresh_001', 'fresh_003')


class PaidBudget:
    def __init__(self, maximum=PAID_CAP):
        if type(maximum) is not int or not 1 <= maximum <= PAID_CAP:
            raise ValueError('Invalid paid request cap')
        self.maximum = maximum
        self.used = 0
        self.lock = Lock()

    def claim(self):
        with self.lock:
            if self.used >= self.maximum:
                raise ExecutionError('BUDGET_EXCEEDED')
            self.used += 1


class RecordedModel:
    json_output = True
    blind_review = False

    def __init__(self, model, budget):
        self.model, self.budget, self.replies = model, budget, []
        self.attempts = 0

    def invoke(self, messages):
        self.budget.claim()
        self.attempts += 1
        try:
            response = self.model.invoke(messages)
        except Exception as exc:
            self.replies.append({'error_code': getattr(exc, 'code', type(exc).__name__)})
            raise
        self.replies.append({'content': response.content})
        return response


class SeedThenLive:
    """Only the first turn is seeded; later turns and failures are retained unchanged."""
    json_output = True
    blind_review = False

    def __init__(self, seed, live, mutate):
        self.seed, self.live, self.mutate, self.calls = seed, live, mutate, 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls > 1:
            return self.live.invoke(messages)
        data = json.loads(self.seed.invoke(messages).content)
        return AIMessage(content=json.dumps(self.mutate(data, messages), ensure_ascii=False))


def probe_models(live, variant='wording'):
    if variant not in {'wording', 'trigger_unknown', 'unsupported_mechanism'}:
        raise ValueError('Unknown revision probe variant')
    models = scripted_models()
    def plan(data, messages):
        scope = json.loads(messages[1].content)['input']['ticket']['scope']
        window = {key: scope[key] for key in ('start', 'end')}
        for task in data.get('tasks', []):
            task.update(checks=[{'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}},
                               {'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['dependency_error_rate']}}],
                        expected_value='Observe current symptoms and failure boundary')
        return data
    def draft(data, messages):
        data['recommended_actions'][0].update(kind='manual_check', checks=[], repair=None,
            action='要求维护方确认已经下调的远端策略', condition='当前依赖请求失败',
            expected_result='核实当前配置和协议记录', risk='只读核实', requires_approval=False)
        if variant == 'trigger_unknown':
            data['hypotheses'].append({'cause': '为何开始发生尚未确认，不能推定远端策略变更',
                'level': 'trigger', 'support_refs': [], 'counter_refs': [],
                'pending_checks': ['人工取得远端变更与运行记录']})
            data['missing_information'].append('远端触发来源未知，需人工核实')
        elif variant == 'unsupported_mechanism':
            data['hypotheses'][0].update(cause='inventory-api维护方主动下调账户配额，导致当前请求失败',
                                       level='mechanism')
        return data
    def review(data, messages):
        next(item for item in data['assessments'] if item['target_id'] == 'A1').update(
            verdict='uncertain', reason='建议预设未证实的远端变更，需改成中性核查')
        data['revision_requests'] = [{'target_id': 'A1', 'issue_type': 'expression',
            'reason': '建议预设维护方已经下调策略，当前来源没有证明',
            'required_change': '基于现有证据核对当前协议与配置，不预设远端发生变更；保留未知'}]
        data['request_evidence'] = None
        data['follow_up_reason'] = '只需修正建议表达，不需要新增观测'
        if variant == 'trigger_unknown':
            next(item for item in data['assessments'] if item['target_id'] == 'H2').update(
                verdict='uncertain', evidence_ids=[], reason='触发来源未知仍保留')
            next(item for item in data['need_assessments'] if item['need_id'] == 'N3').update(
                verdict='not_required', evidence_ids=[], reason='种子边界设置：触发未知不是竞争机制；最终模型独立重评')
        return data
    models['supervisor'] = SeedThenLive(models['supervisor'], live['supervisor'], plan)
    models['diagnosis'] = SeedThenLive(models['diagnosis'], live['diagnosis'], draft)
    models['reviewer'] = SeedThenLive(models['reviewer'], live['reviewer'], review)
    return models


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--previous-summary', type=Path,
        help='Carry the paid cap across a new implementation batch; keep every prior failure')
    args = parser.parse_args(argv)
    lock_path = ROOT / 'evals/fresh_validation/freeze.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    for relative, expected in lock['files'].items():
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    config = IncidentConfig.from_file()
    source = implementation_version()
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    prior_attempts = 0
    prior_summary = None
    if args.previous_summary:
        previous = json.loads(args.previous_summary.read_text(encoding='utf-8'))
        prior_attempts = previous['paid_attempts']
        if type(prior_attempts) is not int or not 0 <= prior_attempts < PAID_CAP:
            raise ValueError('No remaining paid allowance or invalid previous summary')
        if previous['plan']['maximum_paid_attempts'] != PAID_CAP or previous['plan']['model'] != config.model:
            raise ValueError('Previous cap/model does not match')
        recorded = previous['plan'].get('prior_paid_attempts', 0) + sum(row['paid_attempts'] for row in previous['runs'])
        if recorded != prior_attempts:
            raise ValueError('Previous paid counts are inconsistent')
        prior_summary = {'path': str(args.previous_summary.resolve()),
                         'sha256': hashlib.sha256(args.previous_summary.read_bytes()).hexdigest()}
    plan = {'source_version': source, 'script_sha256': script_hash, 'model': config.model,
            'cases': list(CASES), 'arms': ['legacy', 'review_driven'], 'limits': asdict(LIMITS),
            'maximum_paid_attempts': PAID_CAP, 'attempts_per_case_arm': 1,
            'prior_paid_attempts': prior_attempts, 'remaining_paid_attempts': PAID_CAP - prior_attempts,
            'previous_summary': prior_summary,
            'probe': 'seeded_wording_feedback_then_live_revision_and_review',
            'synthetic_observations': True, 'live_repair_actions': 0,
            'independent_quality_score': False, 'estimated_cost': None,
            'dataset_freeze_sha256': hashlib.sha256(lock_path.read_bytes()).hexdigest()}
    if not args.execute_live:
        print(json.dumps({'plan_only': True, **plan}, ensure_ascii=False))
        return 0
    if not config.api_key or config.api_key == 'test-only':
        raise ValueError('Configured live model credentials required')
    directory = ROOT / 'output/review-driven' / str(uuid4())
    write_json(directory / 'plan.json', plan)
    budget, summaries = PaidBudget(), []
    budget.used = prior_attempts
    jobs = [(case, arm, False) for index, case in enumerate(CASES)
            for arm in (('legacy', 'review_driven') if index % 2 == 0 else ('review_driven', 'legacy'))]
    jobs.append(('case_001', 'review_driven', True))
    for case, arm, probe in jobs:
        assert implementation_version() == source, 'Application changed during validation'
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == script_hash
        print(json.dumps({'starting': case, 'arm': arm, 'seeded_probe': probe,
                          'directory': str(directory)}, ensure_ascii=False), flush=True)
        live = {role: RecordedModel(model, budget) for role, model in build_roles(
            config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body).items()}
        models = probe_models(live) if probe else live
        paid_before = budget.used
        report = capture(case, 'multi', lambda flow: models, Principal('synthetic_demo', 'cli_reader'), LIMITS,
            provider_factory=FixtureProvider if probe else FreshProvider,
            investigation_mode='programmatic', knowledge_mode='programmatic', blind_review=False,
            orchestration=arm)
        report.update(source_version=source, model=config.model, execution_mode='live_seeded_probe' if probe else 'live',
                      experiment_arm=arm, case_id=case, seeded_feedback=probe)
        name = 'seeded-revision' if probe else f'{case}-{arm}'
        path = directory / f'{name}.json'
        write_json(path, report)
        write_json(directory / f'{name}-replies.json', {role: model.replies for role, model in live.items()})
        if 'output' in report:
            path.with_suffix('.md').write_text(render(report), encoding='utf-8')
        summary = {'case_id': case, 'arm': arm, 'seeded_probe': probe,
            'status': report['status'], 'stop': report['run_summary']['termination_reason'],
            'model_calls': report['run_summary']['model_calls'], 'paid_attempts': budget.used - paid_before,
            'tool_calls': report['run_summary']['tool_calls'], 'duration_ms': report['run_summary']['duration_ms'],
            'pure_revisions': report.get('pure_revision_rounds', 0), 'reworks': report.get('rework_rounds', 0),
            'role_paid_attempts': {role: model.attempts for role, model in live.items()},
            'handoff_reason': report.get('handoff', {}).get('reason'),
            'validation_failures': [e.get('reason') for e in report.get('events', []) if e['type'] == 'validation_failure'],
            'report_file': path.relative_to(ROOT).as_posix(),
            'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        summaries.append(summary)
        write_json(directory / 'summary.json', {'plan': plan, 'paid_attempts': budget.used, 'runs': summaries})
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        assert implementation_version() == source
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
