"""Frozen paired metrics, reusing saved-report validators; no paid execution."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from evals.independent import scoring, suite
from evals.incident.comparison import targets
from evals.run_statistics import canonical, distribution, engineering, number, rate

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'evals/metrics_protocol_v1.json'
VERSION = 'paired-metrics-v1'
MODEL_SETTINGS = {'temperature': 0, 'max_tokens': 2200, 'max_retries': 0,
                  'request_timeout': 30, 'transport': 'openai_chat_completions',
                  'response_format': {'type': 'json_object'}}
EVALUATION_FILES = (
    'scripts/compare_incident.py', 'scripts/compare_independent.py',
    'scripts/evaluate_paired.py', 'evals/paired_metrics.py', 'evals/run_statistics.py',
    'evals/metrics_protocol_v1.json', 'evals/independent/provider.py',
    'evals/independent/control.py', 'evals/independent/suite.py',
    'evals/independent/scoring.py', 'evals/incident/comparison.py',
    'evals/incident/acceptance.py', 'requirements.txt')


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def evaluation_hashes():
    return {name: suite.digest(ROOT / name) for name in EVALUATION_FILES}


def request_fingerprint(config, model):
    # Only the digest leaves this function. Never persist credentials or raw extra_body.
    return fingerprint({'model': model, 'base_url': config.base_url,
                        'extra_body': {**config.extra_body, 'response_format': {'type': 'json_object'}},
                        'settings': MODEL_SETTINGS})


def read_protocol(path=PROTOCOL):
    protocol = suite.read_json(Path(path))
    cases = suite.select_cases('validation')
    if (protocol['cases'] != cases or protocol['split'] != 'validation'
            or protocol['dataset_version'] != 'independent-synthetic-v1'
            or sorted(sum(protocol['batches'], [])) != sorted(cases)
            or any(not 1 <= len(batch) <= 2 for batch in protocol['batches'])
            or set(protocol['critical_checks']) != set(cases)):
        raise ValueError('Invalid metrics protocol inventory')
    for checks in protocol['critical_checks'].values():
        if not checks or len({c['check_id'] for c in checks}) != len(checks) or any(
                not c['requirement'].strip() or c['availability'] not in {'available', 'unavailable'} for c in checks):
            raise ValueError('Invalid frozen critical checks')
    return protocol


def experiment_batches(protocol, cases):
    """Keep the protocol's batch numbers/order, dropping unselected cases only."""
    result = []
    for index, batch in enumerate(protocol['batches'], 1):
        selected = [cid for cid in batch if cid in cases]
        if selected:
            result.append((index, selected))
    return result


def prepare_experiment(config, experiment_id, *, repeats=1, protocol_path=PROTOCOL, cases=None):
    from dataclasses import asdict
    from runtime.context import Limits
    from runtime.version import implementation_version
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', experiment_id) or type(repeats) is not int or repeats not in (1, 2):
        raise ValueError('Experiment requires a safe ID and one or two repeats')
    protocol = read_protocol(protocol_path)
    selected = suite.select_cases('validation', cases)
    selected = [cid for cid in protocol['cases'] if cid in selected]
    lock = suite.verify_freeze()
    limits = asdict(Limits(model_calls=protocol['model_budget'], tool_calls=protocol['tool_budget'],
                           reserve_model_calls=3, reserve_seconds=20))
    batches = []
    for repeat in range(1, repeats + 1):
        for index, batch_cases in experiment_batches(protocol, selected):
            order = {cid: arm_order(cid, (repeat - 1) % 2) for cid in batch_cases}
            batches.append({'repeat_id': repeat, 'batch': index, 'cases': batch_cases,
                            'order_offset': (repeat - 1) % 2, 'arm_order': order,
                            'runs': len(batch_cases) * 2,
                            'maximum_model_calls': len(batch_cases) * 2 * limits['model_calls'],
                            'maximum_tool_calls': len(batch_cases) * 2 * limits['tool_calls']})
    count = len(selected) * repeats * 2
    return {'version': VERSION, 'experiment_id': experiment_id, 'protocol': protocol,
            **({'selected_cases': selected} if cases is not None else {}),
            'protocol_sha256': fingerprint(protocol), 'repeat_ids': list(range(1, repeats + 1)),
            'application_version': implementation_version(), 'evaluation_code_sha256': evaluation_hashes(),
            'dataset_version': lock['dataset_version'], 'dataset_freeze_sha256': suite.digest(suite.SUITE_ROOT / 'freeze.json'),
            'fixture_sha256': {cid: suite.digest(suite.DATA_ROOT / f'{cid}.json') for cid in selected},
            'model': config.model, 'model_settings': deepcopy(MODEL_SETTINGS),
            'request_config_sha256': request_fingerprint(config, config.model), 'limits': limits,
            'planned_runs': count, 'planned_pairs': count // 2, 'unique_cases': len(selected),
            'maximum_model_calls': count * limits['model_calls'],
            'maximum_tool_calls': count * limits['tool_calls'], 'batches': batches,
            'paid_requests_sent': 0, 'estimated_cost': None,
            'note': '冻结计划，不验证凭据、不调用模型；live仍须逐批显式授权。'}


def arm_order(case_id, offset=0):
    return ['single', 'multi'] if (int(case_id.rsplit('_', 1)[1]) + offset) % 2 else ['multi', 'single']


def validate_manifest(manifest):
    protocol = manifest['protocol']
    selected = manifest.get('selected_cases', protocol['cases'])
    if (not isinstance(selected, list) or not selected or not all(isinstance(c, str) for c in selected)
            or selected != [cid for cid in protocol['cases'] if cid in selected]):
        raise ValueError('Invalid frozen case selection')
    expected = {(r, c, f) for r in manifest['repeat_ids'] for c in selected for f in ('single', 'multi')}
    if (manifest['version'] != VERSION or fingerprint(protocol) != manifest['protocol_sha256']
            or manifest['planned_runs'] != len(expected)
            or manifest['planned_pairs'] != len(expected) // 2
            or manifest['unique_cases'] != len(selected)
            or set(manifest['fixture_sha256']) != set(selected)
            or manifest['repeat_ids'] not in ([1], [1, 2])):
        raise ValueError('Invalid experiment manifest')
    # Definitions and their availability were fixed before the run, never chosen per arm.
    if protocol != read_protocol():
        raise ValueError('Protocol differs from versioned specification')
    from dataclasses import asdict
    from runtime.context import Limits
    expected_limits = asdict(Limits(model_calls=protocol['model_budget'], tool_calls=protocol['tool_budget'],
                                    reserve_model_calls=3, reserve_seconds=20))
    expected_batches = [(r, i, cases) for r in manifest['repeat_ids']
                        for i, cases in experiment_batches(protocol, selected)]
    if (manifest['limits'] != expected_limits or manifest['model_settings'] != MODEL_SETTINGS
            or manifest['maximum_model_calls'] != len(expected) * expected_limits['model_calls']
            or manifest['maximum_tool_calls'] != len(expected) * expected_limits['tool_calls']
            or [(b['repeat_id'], b['batch'], b['cases']) for b in manifest['batches']] != expected_batches):
        raise ValueError('Manifest budgets or batch schedule changed')
    for batch in manifest['batches']:
        offset = (batch['repeat_id'] - 1) % 2
        if batch['order_offset'] != offset or batch['arm_order'] != {cid: arm_order(cid, offset) for cid in batch['cases']}:
            raise ValueError('Manifest execution order changed')
    return expected


def validate_batch(manifest, config, cases, limits, *, repeat_id=1, order_offset=0):
    from runtime.version import implementation_version
    validate_manifest(manifest)
    batch = next((b for b in manifest['batches'] if b['repeat_id'] == repeat_id and b['cases'] == cases), None)
    if not batch or batch['order_offset'] != order_offset or manifest['limits'] != limits:
        raise ValueError('Batch cases, repeat, order or limits differ from frozen plan')
    if (config.model != manifest['model'] or request_fingerprint(config, config.model) != manifest['request_config_sha256']
            or implementation_version() != manifest['application_version']
            or evaluation_hashes() != manifest['evaluation_code_sha256']
            or suite.digest(suite.SUITE_ROOT / 'freeze.json') != manifest['dataset_freeze_sha256']):
        raise ValueError('Application, evaluation, dataset or model configuration changed; do not execute frozen experiment')
    return batch


def qualified_targets(report):
    result = targets(report)
    originals = {f'{prefix}{index}': raw for prefix, field in (
        ('F', 'findings'), ('H', 'hypotheses'), ('A', 'recommended_actions'))
        for index, raw in enumerate(report['output'][field], 1)}
    for row in result:
        # Preserve qualifiers/status and the whole action (conditions/risks/checks), not just its title.
        row['published_content'] = deepcopy(originals[row['target_id']])
    return result


def claim_attempt(manifest_path, batch, run_directory):
    """Exclusive local receipt prevents re-paying a frozen batch after interruption."""
    manifest_path = Path(manifest_path)
    path = manifest_path.parent / 'attempts' / f"repeat-{batch['repeat_id']}-batch-{batch['batch']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'status': 'claimed', 'run_directory': str(Path(run_directory).resolve()),
               'manifest_sha256': fingerprint(suite.read_json(manifest_path)),
               'repeat_id': batch['repeat_id'], 'batch': batch['batch']}
    try:
        with path.open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    except FileExistsError:
        raise ValueError('Frozen batch was already attempted; inspect its receipt/run-index, never auto-replay') from None
    return path, receipt


def review_entry(report, digest, protocol):
    entry = scoring.semantic_template(report, digest, target_builder=qualified_targets)
    entry.update(reviewer_type='human', critical_checks=[
        {**deepcopy(check), 'status': None, 'reason': '', 'evidence_ids': []}
        for check in protocol['critical_checks'][report['incident_id']]])
    return entry


def validate_review(review, run, protocol):
    report = run['report']
    if (review.get('report_sha256') != run['sha256'] or review.get('case_id') != report['incident_id']
            or type(review.get('approved')) is not bool or review.get('reviewer_type') not in {'human', 'assistant'}):
        raise ValueError('Review identity/hash/type mismatch')
    if not review['approved'] or review['reviewer_type'] != 'human':
        return False
    if report['execution_mode'] != 'live':
        raise ValueError('Scripted control cannot receive formal human quality scores')
    checks = review.get('critical_checks', [])
    expected = protocol['critical_checks'][report['incident_id']]
    if len(checks) != len(expected) or {c['check_id'] for c in checks} != {c['check_id'] for c in expected}:
        raise ValueError('Review must cover frozen critical checks exactly once')
    definitions = {c['check_id']: c for c in expected}
    evidence = {e['evidence_id'] for e in report['evidence'] if e['kind'] == 'observation'}
    for check in checks:
        definition = definitions[check['check_id']]
        if any(check.get(k) != v for k, v in definition.items()) or not check.get('reason', '').strip():
            raise ValueError('Critical check definition changed or reason missing')
        allowed = {'observed', 'missed'} if definition['availability'] == 'available' else {'unavailable'}
        refs = check.get('evidence_ids')
        if check.get('status') not in allowed or not isinstance(refs, list) or len(refs) != len(set(refs)):
            raise ValueError('Invalid critical check status/references')
        if (check['status'] == 'observed' and not refs) or not set(refs) <= evidence:
            raise ValueError('Observed checks require actual current evidence; unknown references rejected')
        if check['status'] != 'observed' and refs:
            raise ValueError('Unobserved check must not carry supporting references')
    return True


def cohort_identity(bundle):
    config, prov = bundle['configuration'], bundle['provenance']
    return {k: deepcopy(v) for k, v in {
        'model': config['model'], 'execution_mode': config['execution_mode'], 'limits': config['limits'],
        'model_settings': config.get('model_settings'), 'request_config_sha256': config.get('request_config_sha256'),
        'application_version': prov.get('implementation_sha256_prefix'),
        'dataset_version': prov.get('dataset_version'), 'dataset_freeze_sha256': prov.get('dataset_freeze_sha256'),
        'evaluation_code_sha256': prov.get('evaluation_code_sha256'),
        'experiment_id': prov.get('metrics_experiment', {}).get('experiment_id'),
        'manifest_sha256': prov.get('metrics_experiment', {}).get('manifest_sha256')}.items()}


def _validate_frozen_bundle(bundle, manifest):
    config, prov = bundle['configuration'], bundle['provenance']
    meta = prov.get('metrics_experiment', {})
    batch = next((b for b in manifest['batches'] if b['repeat_id'] == meta.get('repeat_id')
                  and b['cases'] == config['cases']), None)
    if (not batch or meta.get('order_offset') != batch['order_offset']
            or meta.get('experiment_id') != manifest['experiment_id']
            or meta.get('manifest_sha256') != fingerprint(manifest)
            or config['limits'] != manifest['limits']
            or config.get('model_settings') != manifest['model_settings']
            or config.get('request_config_sha256') != manifest['request_config_sha256']
            or prov.get('implementation_sha256_prefix') != manifest['application_version']
            or prov.get('dataset_freeze_sha256') != manifest['dataset_freeze_sha256']
            or prov.get('evaluation_code_sha256') != manifest['evaluation_code_sha256']
            or (config['execution_mode'] == 'live' and config['model'] != manifest['model'])
            or (config['execution_mode'] == 'scripted_control_only' and config['model'] != 'scripted')
            or config['execution_mode'] not in {'live', 'scripted_control_only'}):
        raise ValueError('Bundle does not belong to the frozen experiment')
    order = [(cid, flow) for cid in batch['cases'] for flow in batch['arm_order'][cid]]
    if [(row['case_id'], row['workflow']) for row in bundle['runs']] != order:
        raise ValueError('Saved execution order differs from plan')


def summarize_pairs(bundle_paths, manifest, reviews=None, *, historical=False):
    """Re-score immutable reports; partition history and keep incomplete/unknown explicit."""
    planned = validate_manifest(manifest)
    review_rows = (reviews or {}).get('runs', [])
    if len({r['run_id'] for r in review_rows}) != len(review_rows):
        raise ValueError('Duplicate review run_id')
    review_map = {r['run_id']: r for r in review_rows}
    if reviews is not None and (reviews.get('version') != VERSION
            or reviews.get('protocol_sha256') != manifest['protocol_sha256']):
        raise ValueError('Review worksheet protocol/version mismatch')
    groups, all_ids, worksheets, all_rows = {}, set(), [], []
    for bundle_path in bundle_paths:
        bundle_path = Path(bundle_path)
        bundle = suite.read_json(bundle_path)
        if not historical:
            _validate_frozen_bundle(bundle, manifest)
        identity = cohort_identity(bundle)
        if not identity['application_version']:
            raise ValueError('Unversioned historical bundle cannot establish comparable cohort')
        # Reuse all original dataset, snapshot, contract, reference and accounting checks.
        _, reports = scoring.evaluate(bundle, bundle_path.parent, target_builder=qualified_targets)
        accepted = []
        for rid, run in reports.items():
            if rid in all_ids:
                raise ValueError('Duplicate run_id across bundles')
            all_ids.add(rid)
            if run['report']['incident_id'] not in {slot[1] for slot in planned}:
                raise ValueError('Only frozen experiment cases may enter these metrics')
            if rid in review_map and validate_review(review_map[rid], run, manifest['protocol']):
                accepted.append(review_map[rid])
            worksheets.append(review_entry(run['report'], run['sha256'], manifest['protocol']))
        scored, _ = scoring.evaluate(bundle, bundle_path.parent, {'runs': accepted}, target_builder=qualified_targets)
        key = fingerprint(identity)
        group = groups.setdefault(key, {'identity': identity, 'records': [], 'slots': set(), 'bundles': []})
        repeat = bundle['provenance'].get('metrics_experiment', {}).get('repeat_id', 1)
        group['bundles'].append(str(bundle_path.resolve()))
        for row in scored['runs']:
            rid = row['run_id']
            slot = (repeat, row['case_id'], row['workflow'])
            if slot in group['slots']:
                raise ValueError('Duplicate case/arm/repeat; never select the best retry')
            group['slots'].add(slot)
            record = {**row, 'repeat_id': repeat, 'report_sha256': reports[rid]['sha256'],
                      'report_file': str((bundle_path.parent / next(e['report_file'] for e in bundle['runs']
                          if e['report_sha256'] == reports[rid]['sha256'])).resolve()),
                      'engineering': engineering(reports[rid]['report']),
                      'human_review': review_map[rid] if rid in {r['run_id'] for r in accepted} else None}
            group['records'].append(record)
            all_rows.append(record)
    if not groups:
        raise ValueError('At least one complete paired bundle required')
    if not historical and len(groups) != 1:
        raise ValueError('Mixed configuration or execution modes; split experiments instead')
    if set(review_map) - all_ids:
        raise ValueError('Review names an unknown run')
    summaries = []
    for key, group in groups.items():
        rows, slots = group['records'], group['slots']
        complete = slots == planned and not historical
        reviewed = all(r['semantic_review'] == 'reviewed' for r in rows)
        live = group['identity']['execution_mode'] == 'live'
        ready = complete and reviewed and live
        arms = {}
        for flow in ('single', 'multi'):
            selected = [r for r in rows if r['workflow'] == flow]
            judged = [r for r in selected if r['human_review'] is not None]
            checks = [c for r in judged for c in r['human_review']['critical_checks']]
            actions = [c for r in judged for c in r['human_review']['claim_assessments'] if c['target_id'].startswith('A')]
            qualified = sum(r['mechanical_pass'] and r.get('semantic_success') is True and r['status'] != 'failed' for r in judged)
            es = [r['engineering'] for r in selected]
            usages = [e['token_usage'] for e in es if e['token_usage'].get('status') == 'known'
                      and e['token_usage'].get('known_calls') == e['model_calls']
                      and all(number(e['token_usage'].get(k)) for k in ('input_tokens', 'output_tokens'))]
            arms[flow] = {
                'runs': len(selected), 'human_reviewed_runs': len(judged),
                'statuses': dict(Counter(r['status'] for r in selected)),
                'mechanical_passes': sum(r['mechanical_pass'] for r in selected),
                'qualified_reports': {'numerator': qualified if ready else None,
                                      'denominator': manifest['planned_pairs'],
                                      'rate': qualified / manifest['planned_pairs'] if ready else None},
                'reviewed_subset_qualified': rate(qualified, len(judged)),
                'critical_check_omission': rate(sum(c['status'] == 'missed' for c in checks),
                                               sum(c['status'] in {'observed', 'missed'} for c in checks)),
                'critical_checks_unavailable': sum(c['status'] == 'unavailable' for c in checks),
                'advice_supported': rate(sum(c['supported'] for c in actions), len(actions)),
                'dimension_means_reviewed': {k: (sum(r['semantic_scores'][k] for r in judged) / len(judged)
                                                if judged else None) for k in ('outcome', 'evidence_reasoning', 'uncertainty', 'actions', 'handoff')},
                'model_calls': distribution(e['model_calls'] for e in es),
                'tool_calls': distribution(e['tool_calls'] for e in es),
                'duration_ms': distribution(e['duration_ms'] for e in es),
                'token_usage_complete_runs': len(usages),
                'token_usage_completeness': rate(len(usages), len(selected)),
                'known_input_tokens': sum(e['token_usage']['input_tokens'] for e in es if number(e['token_usage'].get('input_tokens'))),
                'known_output_tokens': sum(e['token_usage']['output_tokens'] for e in es if number(e['token_usage'].get('output_tokens'))),
                'complete_total_tokens': sum(u['input_tokens'] + u['output_tokens'] for u in usages) if len(usages) == len(es) else None,
                'estimated_cost': None}
        pairs = []
        for repeat, cid in sorted({(r['repeat_id'], r['case_id']) for r in rows}):
            pair = {r['workflow']: r for r in rows if (r['repeat_id'], r['case_id']) == (repeat, cid)}
            both = all(r['semantic_review'] == 'reviewed' for r in pair.values())
            tokens = {}
            for flow, row in pair.items():
                usage = row['token_usage']
                tokens[flow] = (usage['input_tokens'] + usage['output_tokens'] if usage.get('status') == 'known'
                    and usage.get('known_calls') == row['model_calls']
                    and all(number(usage.get(k)) for k in ('input_tokens', 'output_tokens')) else None)
            pairs.append({'case_id': cid, 'repeat_id': repeat,
                          'total_tokens_difference_multi_minus_single': tokens['multi'] - tokens['single']
                              if all(v is not None for v in tokens.values()) else None,
                          'quality_score_difference_multi_minus_single':
                              sum(pair['multi']['semantic_scores'].values()) - sum(pair['single']['semantic_scores'].values()) if both else None,
                          **{name + '_difference_multi_minus_single': pair['multi'][name] - pair['single'][name]
                             if number(pair['multi'].get(name)) and number(pair['single'].get(name)) else None
                             for name in ('duration_ms', 'model_calls', 'tool_calls')}})
        difference = {}
        for name in ('duration_ms', 'model_calls', 'tool_calls'):
            single, multi = (arms[f][name]['mean'] for f in ('single', 'multi'))
            difference[name + '_relative_change_multi_vs_single'] = (multi - single) / single if single and multi is not None else None
        difference['qualified_rate_difference_percentage_points'] = (
            100 * (arms['multi']['qualified_reports']['rate'] - arms['single']['qualified_reports']['rate']) if ready else None)
        single_tokens, multi_tokens = (arms[f]['complete_total_tokens'] for f in ('single', 'multi'))
        difference['total_tokens_relative_change_multi_vs_single'] = (
            (multi_tokens - single_tokens) / single_tokens if single_tokens and multi_tokens is not None else None)
        summaries.append({'cohort_id': key, 'identity': group['identity'], 'historical_only': historical,
                          'planned_runs': manifest['planned_runs'], 'observed_runs': len(rows),
                          'missing_slots': [list(s) for s in sorted(planned - slots)],
                          'complete': complete, 'unique_cases_observed': len({r['case_id'] for r in rows}),
                          'quality_status': 'excluded_scripted' if not live else 'historical_review_only' if historical
                              else 'reviewed' if ready else 'incomplete_or_manual_review_pending',
                          'workflows': arms, 'paired_outcomes': pairs, 'comparison': difference, 'bundles': group['bundles']})
    worksheet = {'version': VERSION, 'protocol_sha256': manifest['protocol_sha256'],
                 'instruction': '人工核对原报告、引用和限定条件后逐项填写；助手评价标assistant，不计正式质量。',
                 'runs': sorted(worksheets, key=lambda r: r['run_id'])}
    return {'version': VERSION, 'experiment_id': manifest['experiment_id'],
            'manifest_sha256': fingerprint(manifest), 'historical_only': historical,
            'scoring_code_sha256': evaluation_hashes(), 'runs_total': len(all_rows),
            'cohorts': summaries, 'runs': all_rows,
            'notes': manifest['protocol']['limitations'] + [
                '主合格率仅完整同版本live实验且全部人工审阅时公布；其余为null。',
                'reviewed_subset指标只描述已审阅子集，不替代主成绩。',
                '相对变化为(multi-single)/single，正数表示多角色消耗或耗时更高。',
                '历史报告以当前评分代码离线复核，分组保留原版本，不冒充当前实验。']}, worksheet


def markdown(summary):
    lines = ['# 配对诊断指标', '', f"实验：{summary['experiment_id']}；运行：{summary['runs_total']}；历史复用：{summary['historical_only']}。", '']
    def show(item):
        if item['rate'] is None:
            return f"未形成成绩（分母 {item['denominator']}）"
        return f"{item['numerator']}/{item['denominator']}（{item['rate']:.1%}）"
    for cohort in summary['cohorts']:
        lines += [f"## {cohort['cohort_id'][:12]}", '',
                  f"源码：{cohort['identity']['application_version']}；模型：{cohort['identity']['model']}；状态：{cohort['quality_status']}。",
                  f"已保存 {cohort['observed_runs']}/{cohort['planned_runs']} 次运行，{cohort['unique_cases_observed']} 个不同案例。", '',
                  '| 指标 | single | multi |', '| --- | --- | --- |']
        for label, name in (('主合格报告率', 'qualified_reports'), ('关键检查遗漏率（已审阅子集）', 'critical_check_omission'),
                            ('建议有据率（已审阅子集）', 'advice_supported'), ('Token用量完整率', 'token_usage_completeness')):
            lines.append('| ' + label + ' | ' + ' | '.join(show(cohort['workflows'][f][name]) for f in ('single', 'multi')) + ' |')
        for label, name in (('运行耗时ms', 'duration_ms'), ('模型调用', 'model_calls'), ('工具调用', 'tool_calls')):
            lines.append('| ' + label + ' 均值 / 中位数 | ' + ' | '.join(
                f"{cohort['workflows'][f][name]['mean']} / {cohort['workflows'][f][name]['p50']}" for f in ('single', 'multi')) + ' |')
        lines += ['', '各臂状态：' + canonical({f: cohort['workflows'][f]['statuses'] for f in ('single', 'multi')}), '']
    lines += ['## 口径', ''] + ['- ' + note for note in summary['notes']]
    return '\n'.join(lines) + '\n'
