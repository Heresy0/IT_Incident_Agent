"""Offline report semantics and capability differences; no keyword quality judge."""
from evals.independent import scoring as common
from evals.incident.comparison import targets
from evals.targeted.suite import SUITE_ROOT, DATA_ROOT, read_json


def published_targets(report):
    """Grade claims as expressed, including refutation, conditions and pending items."""
    rows = targets(report)
    for row in rows:
        prefix, index = row['target_id'][0], int(row['target_id'][1:]) - 1
        field = {'F': 'findings', 'H': 'hypotheses', 'A': 'recommended_actions'}[prefix]
        # Preserve the full original assertion, not just a cause/action sentence.
        row['claim_context'] = report['output'][field][index]
        row['published_state'] = 'report_output'
    for index, action in enumerate(report.get('pending_actions', []), 1):
        rows.append({'target_id': f'P{index}', 'text': action['action'], 'references': [],
            'counter_refs': [], 'claim_context': action, 'published_state': 'pending_not_executable',
            'supported': None, 'reason': ''})
    return rows


def semantic_template(report, report_hash):
    row = common.semantic_template(report, report_hash, suite_root=SUITE_ROOT, target_builder=published_targets)
    row['claim_support_meaning'] = '评价原表述及其状态是否合理；refuted评价排除是否正确，pending不当成获批动作。'
    row['capability_assessments'] = {key: {'score': None, 'reason': ''}
                                   for key in row['reference_answer']['capability_criteria']}
    return row


def evaluate(bundle, directory, reviews=None):
    result, reports = common.evaluate(bundle, directory, reviews, suite_root=SUITE_ROOT,
                                      data_root=DATA_ROOT, target_builder=published_targets)
    return annotate(result, reports, reviews)


def annotate(result, reports, reviews=None):
    manifest = read_json(SUITE_ROOT / 'manifest.json')
    case_meta = manifest['case_metadata']
    judged = {}
    for review in (reviews or {}).get('runs', []):
        if not review.get('approved'):
            continue
        answer = read_json(SUITE_ROOT / 'answers' / f"{review['case_id']}.json")
        criteria = review.get('capability_assessments', {})
        if not isinstance(criteria, dict) or set(criteria) != set(answer['capability_criteria']) or any(
            not isinstance(item, dict) or type(item.get('score')) is not int or item['score'] not in (0, 1, 2)
            or not isinstance(item.get('reason'), str) or not item['reason'].strip()
            for item in criteria.values()):
            raise ValueError('Approved probe review needs every capability score and evidence-based reason')
        judged[review['run_id']] = criteria
    for row in result['runs']:
        row.update(case_group=case_meta[row['case_id']]['group'],
                   family=case_meta[row['case_id']]['family'],
                   capability_assessments=judged.get(row['run_id']))
    by_case = {}
    for row in result['runs']:
        by_case.setdefault(row['case_id'], {})[row['workflow']] = row
    for pair in result['paired_outcomes']:
        arms = by_case[pair['case_id']]
        complete = all(row['capability_assessments'] is not None for row in arms.values())
        pair.update(case_group=case_meta[pair['case_id']]['group'],
                    family=case_meta[pair['case_id']]['family'],
                    capability_differences={key: arms['multi']['capability_assessments'][key]['score']
                        - arms['single']['capability_assessments'][key]['score']
                        for key in arms['single']['capability_assessments']} if complete else None)
    def group_summary(group):
        pairs = [p for p in result['paired_outcomes'] if p['case_group'] == group]
        reviewed = [p for p in pairs if p['quality_difference'] is not None]
        return {'pairs': len(pairs), 'reviewed_pairs': len(reviewed),
            'multi_wins': sum(p['quality_difference'] > 0 for p in reviewed),
            'ties': sum(p['quality_difference'] == 0 for p in reviewed),
            'single_wins': sum(p['quality_difference'] < 0 for p in reviewed),
            'mean_quality_difference': sum(p['quality_difference'] for p in reviewed) / len(reviewed) if reviewed else None}
    result['case_groups'] = {group: group_summary(group) for group in ('targeted', 'control')}
    # Predeclared descriptive threshold, not a statistical claim. Never mix controls into it.
    probe_pairs = [p for p in result['paired_outcomes'] if p['case_group'] == 'targeted']
    families = {p['family'] for p in probe_pairs}
    review_lookup = {r['run_id']: r for r in (reviews or {}).get('runs', []) if r.get('approved')}
    qualified = set()
    all_reviewed = bool(probe_pairs) and all(p['quality_difference'] is not None for p in probe_pairs)
    if all_reviewed:
        for pair in probe_pairs:
            arms = by_case[pair['case_id']]
            multi_review = review_lookup[arms['multi']['run_id']]
            if (pair['quality_difference'] >= 2 and pair['multi_success']
                and arms['multi']['mechanical_pass']
                and arms['multi']['semantic_scores']['outcome'] >= arms['single']['semantic_scores']['outcome']
                and not multi_review['unsafe_advice'] and not multi_review['fabricated_evidence']):
                qualified.add(pair['family'])
    result['advantage_signal'] = {
        'status': 'excluded_scripted' if result['quality_status'] == 'excluded_scripted'
            else 'manual_review_pending' if not all_reviewed else 'reviewed',
        'eligible_families': sorted(families), 'qualifying_families': sorted(qualified),
        'threshold_met': len(qualified) >= 2 if all_reviewed and len(families) == 3 else None,
        'meaning': '至少两个专项类别出现总分差>=2、结论不退步且多Agent语义通过；仅为本批小样本信号。'}
    result['scorer_version'] = 'targeted-semantic-v1'
    result['note'] = '专项压力集与普通对照分别报告；不保证多Agent胜出，不代表普遍准确率。共享程序门禁不计独有收益。'
    return result, reports


def evaluate_batches(batches, reviews=None):
    """Combine bounded batches, rejecting repeats and any changed model/code/budget."""
    if not batches:
        raise ValueError('At least one saved batch required')
    summaries, reports, cases, signature = [], {}, [], None
    all_review_rows = (reviews or {}).get('runs', [])
    if len({r['run_id'] for r in all_review_rows}) != len(all_review_rows):
        raise ValueError('Duplicate semantic review')
    for bundle, directory in batches:
        config, provenance = bundle['configuration'], bundle['provenance']
        current = ({k: v for k, v in config.items() if k != 'cases'},
                   {k: v for k, v in provenance.items() if k != 'fixture_sha256'})
        if signature is not None and signature != current:
            raise ValueError('Cannot combine changed implementation/model/budget/split batches')
        signature = current
        if set(cases).intersection(config['cases']):
            raise ValueError('Repeated cases cannot be selected as best attempts')
        # Validate hashes/paths before reading IDs or accepting any supplied review.
        if reviews is None:
            summary, saved = evaluate(bundle, directory)
        else:
            _, validated = common.evaluate(bundle, directory, suite_root=SUITE_ROOT,
                                           data_root=DATA_ROOT, target_builder=published_targets)
            run_ids = set(validated)
            subset = {'runs': [r for r in all_review_rows if r['run_id'] in run_ids]}
            summary, saved = evaluate(bundle, directory, subset)
        if set(reports).intersection(saved):
            raise ValueError('Duplicate saved run')
        summaries.append(summary)
        reports.update(saved)
        cases.extend(config['cases'])
    if any(r.get('approved') and r['run_id'] not in reports for r in all_review_rows):
        raise ValueError('Unknown approved semantic review')
    rows = [r for summary in summaries for r in summary['runs']]
    def mean(values):
        return sum(values) / len(values) if values else None
    dimensions = read_json(SUITE_ROOT / 'rubric.json')['dimensions']
    groups = {}
    for flow in ('single', 'multi'):
        selected = [r for r in rows if r['workflow'] == flow]
        judged = [r for r in selected if r['semantic_review'] == 'reviewed']
        groups[flow] = {'runs': len(selected), 'reviewed_runs': len(judged),
            'semantic_success_rate_reviewed': mean([r['semantic_success'] for r in judged]),
            'dimension_means_reviewed': {k: mean([r['semantic_scores'][k] for r in judged]) for k in dimensions},
            'model_calls_total': sum(r['model_calls'] for r in selected),
            'tool_calls_total': sum(r['tool_calls'] for r in selected),
            'duration_ms_mean': mean([r['duration_ms'] for r in selected]),
            'failed_runs': sum(r['status'] == 'failed' for r in selected),
            'partial_runs': sum(r['status'] == 'partial' for r in selected), 'estimated_cost': None}
    merged = {**summaries[0], 'configuration': {**signature[0], 'cases': cases},
        'provenance': {**signature[1], 'fixture_sha256': {cid: bundle['provenance']['fixture_sha256'][cid]
            for bundle, _ in batches for cid in bundle['configuration']['cases']}},
        'batch_count': len(batches), 'runs': rows, 'workflows': groups,
        'paired_outcomes': [p for s in summaries for p in s['paired_outcomes']],
        'quality_status': 'excluded_scripted' if all(r['execution_mode'] != 'live' for r in rows)
            else 'reviewed' if all(r['semantic_review'] == 'reviewed' for r in rows) else 'manual_review_pending'}
    return annotate(merged, reports, reviews)
