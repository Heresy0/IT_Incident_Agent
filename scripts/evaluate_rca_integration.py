"""Public, paired native-review diagnostics; never supplies semantic gold.

Each task changes either the author's registered references or the wording of
one sentence. The original reviewer still sees every actually acquired source.
No application source, prompt, budget policy or worker is modified.
"""
import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.rca_contracts import canonical_sha, read_json, sha_file, validate_public, validate_public_diagnostics
from scripts.evaluate_rca import (MODEL_SETTINGS, RUN_LIMITS, MemoryPaidBudget,
    RcaBroker, application_freeze, evaluation_freeze, execute_worker, safe_error, write_json)

TASK_SCHEMA = 'rca-integration-tasks-v1'
CONDITIONS = {'reference_ablation': ('full_refs', 'no_refs', 'wrong_field'),
              'wording_pair': ('wording_v1', 'wording_v2')}
TASK_FIELDS = {'task_id', 'group_id', 'case_id', 'comparison_kind', 'condition',
               'public_path', 'source_queries', 'statement', 'level', 'selectors'}


def require(value, message):
    if not value:
        raise ValueError(message)


def load_tasks(path):
    path = Path(path).resolve()
    document = read_json(path)
    require(set(document) == {'schema_version', 'tasks'} and document['schema_version'] == TASK_SCHEMA,
            'PUBLIC_TASK_SCHEMA_REQUIRED')
    require(isinstance(document['tasks'], list) and bool(document['tasks']), 'PUBLIC_TASKS_REQUIRED')
    tasks, groups = [], defaultdict(list)
    for original in document['tasks']:
        require(isinstance(original, dict) and set(original) == TASK_FIELDS, 'PUBLIC_TASK_ALLOWLIST_REQUIRED')
        row = deepcopy(original)
        for key in ('task_id', 'group_id', 'case_id'):
            require(isinstance(row[key], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,96}', row[key]),
                    'INVALID_TASK_IDENTITY')
        require(row['comparison_kind'] in CONDITIONS and row['condition'] in CONDITIONS[row['comparison_kind']],
                'INVALID_TASK_CONDITION')
        public_path = (path.parent / row['public_path']).resolve()
        public = read_json(public_path)
        validate_public(public)
        diagnostic = {'schema_version': public['schema_version'], 'source_queries': row['source_queries'],
            'fixed_controls': [{'control_id': 'public_task', 'statement': row['statement'],
                                'level': row['level'], 'selectors': row['selectors']}]}
        validate_public_diagnostics(diagnostic)
        require(1 <= len(row['source_queries']) <= RUN_LIMITS['tool_calls'], 'PREFIX_EXCEEDS_TOOL_BUDGET')
        require(isinstance(row['selectors'], list) and len(row['selectors']) <= 8 and all(
            isinstance(ref, list) and len(ref) == 2 and all(isinstance(part, str) and part for part in ref)
            for ref in row['selectors']), 'INVALID_PUBLIC_SELECTORS')
        row.update(public_path=str(public_path), public_sha256=sha_file(public_path), public=public)
        tasks.append(row)
        groups[row['group_id']].append(row)
    require(len({row['task_id'] for row in tasks}) == len(tasks), 'DUPLICATE_TASK')
    for group in groups.values():
        first = group[0]
        kind = first['comparison_kind']
        require(len(group) == len(CONDITIONS[kind]) and {r['condition'] for r in group} == set(CONDITIONS[kind]),
                'INCOMPLETE_OR_DUPLICATE_CONDITION_GROUP')
        shared = ['case_id', 'comparison_kind', 'public_sha256', 'source_queries', 'level']
        shared.append('statement' if kind == 'reference_ablation' else 'selectors')
        require(all(all(row[key] == first[key] for key in shared) for row in group),
                'PAIRED_PUBLIC_INPUTS_DIFFER')
        if kind == 'reference_ablation':
            by_condition = {r['condition']: r for r in group}
            require(by_condition['full_refs']['selectors'] and by_condition['wrong_field']['selectors']
                    and not by_condition['no_refs']['selectors'], 'INVALID_REFERENCE_ABLATION')
            full, wrong = by_condition['full_refs']['selectors'], by_condition['wrong_field']['selectors']
            require(len(full) == len(wrong) and [r[0] for r in full] == [r[0] for r in wrong]
                    and all(a[1] != b[1] for a, b in zip(full, wrong)), 'WRONG_FIELD_MUST_KEEP_SOURCE_ROWS')
        else:
            require(len({r['statement'] for r in group}) == 2, 'WORDING_PAIR_MUST_CHANGE_SENTENCE')
    return {'path': str(path), 'sha256': sha_file(path), 'tasks': tasks}


def task_spec(task):
    """Only registered public observations, reads and one original sentence."""
    return {key: deepcopy(task['public'][key]) for key in ('world', 'capability_metadata')} | {
        'initial_checks': deepcopy(task['source_queries']), 'review_controls': [{
            'target_id': 'C1', 'kind': 'factual_relation', 'statement': task['statement'],
            'original_text': task['statement'], 'level': task['level'],
            'selectors': deepcopy(task['selectors'])}]}


def make_schedule(tasks, repeats):
    groups = defaultdict(list)
    for task in tasks:
        groups[task['group_id']].append(task)
    for repeat in range(1, repeats + 1):
        for index, name in enumerate(sorted(groups)):
            rows = sorted(groups[name], key=lambda r: CONDITIONS[r['comparison_kind']].index(r['condition']))
            shift = (repeat - 1 + index) % len(rows)
            for row in rows[shift:] + rows[:shift]:
                yield {key: row[key] for key in ('task_id', 'group_id', 'case_id', 'comparison_kind', 'condition')} | {
                    'repeat': repeat, 'stage': 'fixed_review', 'system_id': 'native_current'}


def public_freeze(tasks):
    return {row['task_id']: {'public_path': row['public_path'], 'public_sha256': row['public_sha256'],
        'world_sha256': canonical_sha(row['public']['world']),
        'capability_sha256': canonical_sha(row['public']['capability_metadata']),
        'task_sha256': canonical_sha({key: row[key] for key in TASK_FIELDS})} for row in tasks}


def make_plan(args, data, provider_sha):
    rows = list(make_schedule(data['tasks'], args.repeats))
    plan = {'schema_version': 'rca-integration-plan-v1', 'application': application_freeze(args.application_root),
        'task_file': {'path': data['path'], 'sha256': data['sha256']},
        'public_freezes': public_freeze(data['tasks']), 'evaluation_source_sha256': evaluation_freeze(),
        'model': 'qwen3.7-flash', 'model_settings': deepcopy(MODEL_SETTINGS),
        'provider_configuration_sha256': provider_sha, 'limits': deepcopy(RUN_LIMITS),
        'repeats': args.repeats, 'schedule': rows, 'planned_runs': len(rows),
        'maximum_model_calls': len(rows) * RUN_LIMITS['model_calls'], 'jobs': 1,
        'pairing_policy': 'same_public_bytes_prefix_and_actual_evidence_content; rotate_condition_order',
        'native_reviewer_sees_all_acquired_sources': True,
        'missing_author_refs_are_not_semantic_failure': True,
        'cross_request_or_time_ablation': 'not_applicable_no_registered_observation',
        'business_sources_modified': False, 'runtime_budget_adapter': None,
        'human_verified': False, 'is_gold': False, 'formal_metrics': None,
        'human_semantic_gate': 'pending', 'formal_acceptance_eligible': False}
    if args.prespecified_plan:
        require(read_json(args.prespecified_plan) == plan, 'EXECUTION_DIFFERS_FROM_FROZEN_PLAN')
    return plan


def verify_inputs(plan, tasks_path, declaration=None):
    data = load_tasks(tasks_path)
    require(data['path'] == plan['task_file']['path'] and data['sha256'] == plan['task_file']['sha256'],
            'TASK_FILE_CHANGED')
    require(public_freeze(data['tasks']) == plan['public_freezes'], 'PUBLIC_INPUTS_CHANGED')
    require(application_freeze(plan['application']['source_root']) == plan['application'], 'APPLICATION_CHANGED')
    require(evaluation_freeze() == plan['evaluation_source_sha256'], 'EVALUATION_SOURCE_CHANGED')
    if declaration:
        require(read_json(declaration) == plan, 'FROZEN_DECLARATION_CHANGED')
    return data


def acquired_content_sha(report):
    native = report.get('native_report', {})
    if native.get('status') == 'harness_failed' or 'evidence' not in native:
        return None
    sources = [{key: value for key, value in row.items() if key != 'retrieved_at'}
               for row in native['evidence']]
    return canonical_sha(sorted(sources, key=lambda r: r['evidence_id']))


def acquired_pack_sha(report):
    content = acquired_content_sha(report)
    return canonical_sha({'content': content, 'collection': report['native_report'].get('collection', [])}) if content else None


def run_collection(args, plan, data, *, model=None, budget=None):
    directory = Path(args.output) / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / 'plan.json', plan)
    tasks = {row['task_id']: row for row in data['tasks']}
    runs, pending = [], []
    mode = 'live_observation_only' if args.live else 'scripted_control_only'
    declaration_sha = sha_file(args.prespecified_plan) if args.prespecified_plan else None
    def save():
        write_json(directory / 'bundle.json', {'schema_version': 'rca-integration-bundle-v1',
            'plan': plan, 'runs': runs, 'scheduled_pending': pending,
            'execution_mode': mode, 'prespecified_plan_sha256': declaration_sha,
            'paid_budget': budget.summary() if budget else None, 'human_verified': False,
            'is_gold': False, 'human_semantic_gate': 'pending', 'formal_metrics': None})
    save()
    for offset, entry in enumerate(plan['schedule']):
        try:
            verify_inputs(plan, args.tasks, args.prespecified_plan)
            require(not args.prespecified_plan or sha_file(args.prespecified_plan) == declaration_sha,
                    'FROZEN_DECLARATION_BYTES_CHANGED')
        except Exception:
            pending.extend({**row, 'reason': 'FROZEN_INPUT_CHANGED'} for row in plan['schedule'][offset:])
            save()
            raise
        if budget and (budget.snapshot().get('admission_closed') or budget.session_requests >= budget.max_calls
                       or any(value <= 0 for value in budget.remaining().values())):
            reason = budget.snapshot().get('admission_close_reason', 'GLOBAL_BUDGET_ADMISSION_CLOSED')
            pending.extend({**row, 'reason': reason} for row in plan['schedule'][offset:])
            break
        chat = model
        if not args.live:
            from workflow.v2.scripted import scripted_model
            chat = scripted_model(None)
        broker = RcaBroker(chat, budget, plan['model'] if args.live else 'scripted-wiring', run_identity=entry)
        task = tasks[entry['task_id']]
        try:
            report = execute_worker(task_spec(task), plan['application'], 'fixed_review', broker)
        except Exception as exc:
            report = {'native_report': {'status': 'harness_failed', 'execution_error': safe_error(exc)},
                      'normalized': {}, 'run_summary': {}, 'worker_wall_seconds': None}
        identifier = 'RCI_' + canonical_sha({'bundle': directory.name, **entry})[:24]
        report.update(entry, report_id=identifier, application=plan['application'],
            execution_mode=mode, public_freeze=plan['public_freezes'][entry['task_id']],
            task={key: deepcopy(task[key]) for key in TASK_FIELDS},
            model_settings=plan['model_settings'], provider_configuration_sha256=plan['provider_configuration_sha256'],
            model_call_log=sorted(broker.calls, key=lambda r: r['request_id']),
            decision_trace=sorted(broker.decision_trace, key=lambda r: r['request_id']),
            human_verified=False, is_gold=False, human_semantic_gate='pending', formal_metrics=None)
        report['acquired_content_sha256'] = acquired_content_sha(report)
        report['acquired_pack_sha256'] = acquired_pack_sha(report)
        name = identifier + '.json'
        write_json(directory / name, report)
        runs.append({**entry, 'report_id': identifier, 'report_file': name,
                     'report_sha256': sha_file(directory / name)})
        save()
        print(json.dumps({**entry, 'status': report['native_report'].get('status'),
            'paid_requests': sum(row['charged_request'] for row in broker.calls),
            'directory': str(directory)}, ensure_ascii=False), flush=True)
    save()
    verify_inputs(plan, args.tasks, args.prespecified_plan)
    require(not args.prespecified_plan or sha_file(args.prespecified_plan) == declaration_sha,
            'FROZEN_DECLARATION_BYTES_CHANGED')
    return directory


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--scripted', action='store_true')
    modes.add_argument('--live', action='store_true')
    parser.add_argument('--tasks', type=Path, required=True)
    parser.add_argument('--application-root', type=Path, required=True)
    parser.add_argument('--repeats', type=int, choices=(3, 5), default=3)
    parser.add_argument('--collect-pending', action='store_true')
    parser.add_argument('--prespecified-plan', type=Path)
    parser.add_argument('--budget-ledger', type=Path)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.live and (not args.collect_pending or any(value is None for value in
            (args.prespecified_plan, args.budget_ledger, args.max_model_calls))):
        parser.error('Live needs --collect-pending, immutable plan, ledger and request cap')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    try:
        sys.path.insert(0, str(ROOT / 'app'))
        from config.incident import IncidentConfig
        config = IncidentConfig.from_file()
        data = load_tasks(args.tasks)
        provider_sha = canonical_sha({'base_url': config.base_url, 'extra_body': config.extra_body})
        plan = make_plan(args, data, provider_sha)
        if not args.live and not args.scripted:
            args.output.mkdir(parents=True, exist_ok=True)
            write_json(args.output / 'plan.json', plan)
            print(json.dumps({'plan_only': True, 'paid_requests_sent': 0,
                'planned_runs': plan['planned_runs'], 'maximum_model_calls': plan['maximum_model_calls'],
                'plan_file': str(args.output / 'plan.json')}))
            return 0
        budget, model = None, None
        if args.live:
            require(config.api_key and config.api_key != 'test-only', 'CONFIGURED_CREDENTIALS_REQUIRED')
            require(1 <= args.max_model_calls <= plan['maximum_model_calls'], 'INVALID_REQUEST_CAP')
            from agents.models import build_model
            model = build_model(config.api_key, plan['model'], base_url=config.base_url,
                extra_body=config.extra_body, max_output_tokens=MODEL_SETTINGS['max_output_tokens'],
                request_timeout=MODEL_SETTINGS['request_timeout'])
            budget = MemoryPaidBudget(args.budget_ledger, args.max_model_calls)
        directory = run_collection(args, plan, data, model=model, budget=budget)
        print(json.dumps({'directory': str(directory), 'formal_metrics': None,
            'paid_requests': budget.session_requests if budget else 0}))
        return 0
    except Exception as exc:
        print(json.dumps({'evaluation_error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
