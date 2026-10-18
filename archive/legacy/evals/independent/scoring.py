"""Offline provenance checks and human semantic scoring of saved reports only."""
import json
from evals.independent.suite import SUITE_ROOT, digest, read_json, verify_freeze, select_cases
from evals.incident.comparison import score_report, targets
from agents.investigation import validate_output
from agents.contracts import DiagnosisDraft
from evidence.contracts import Evidence, InvestigationOutput


def semantic_template(report, report_hash, *, suite_root=SUITE_ROOT, target_builder=targets):
    answer = read_json(suite_root / 'answers' / f"{report['incident_id']}.json")
    rubric = read_json(suite_root / 'rubric.json')
    # Deliberately omit workflow and internal Reviewer verdicts in this worksheet.
    return {'run_id': report['run_id'], 'case_id': report['incident_id'], 'report_sha256': report_hash,
            'reviewer': '', 'approved': False, 'rationale': '',
            'scores': {key: None for key in rubric['dimensions']},
            'unsafe_advice': None, 'fabricated_evidence': None,
            'reference_answer': answer, 'claim_assessments': target_builder(report)}


def evaluate(bundle, directory, reviews=None, *, suite_root=SUITE_ROOT, data_root=None, target_builder=targets):
    from evals.independent.provider import DATA_ROOT
    data_root = DATA_ROOT if data_root is None else data_root
    lock = verify_freeze(suite_root=suite_root, data_root=data_root)
    config = bundle['configuration']
    selected = select_cases(config['split'], config['cases'], config.get('release_holdout', False), suite_root=suite_root)
    if bundle['provenance']['dataset_freeze_sha256'] != digest(suite_root / 'freeze.json') \
            or bundle['provenance']['dataset_version'] != lock['dataset_version']:
        raise ValueError('Dataset provenance mismatch')
    expected_fixtures = {cid: lock['files'][(data_root / f'{cid}.json').relative_to(suite_root.parents[1]).as_posix()]
                         for cid in selected}
    if bundle['provenance'].get('fixture_sha256') != expected_fixtures:
        raise ValueError('Fixture provenance mismatch')
    reports, scores = {}, []
    pairs = set()
    for entry in bundle['runs']:
        path = directory / entry['report_file']
        if path.parent.resolve() != directory.resolve() or path.suffix != '.json' or digest(path) != entry['report_sha256']:
            raise ValueError('Saved report path/hash mismatch')
        report = read_json(path)
        cid, flow = report['incident_id'], report['workflow']
        if cid not in selected or flow not in ('single', 'multi') or report['run_id'] in reports \
                or (cid, flow) in pairs or cid != entry['case_id'] or flow != entry['workflow']:
            raise ValueError('Unknown/duplicate report or arm mismatch')
        if report['provenance'] != bundle['provenance'] or report['model'] != config['model'] \
                or report['execution_mode'] != config['execution_mode'] \
                or report['run_summary']['limits'] != config['limits']:
            raise ValueError('Unequal configuration/provenance')
        if digest(suite_root / 'answers' / f'{cid}.json') != entry['answer_sha256']:
            raise ValueError('Reference answer mismatch')
        # Common snapshot, references, budgets, event and schema checker only.
        # No mandatory fields, tool order or model-internal passed signal in quality score.
        row = score_report(report, {'necessary_checks': []})
        # An honest report may contain no observations when every source is unavailable.
        # Empty evidence has no hashes to corrupt; an empty valid finding list is not a forged ref.
        if not report['evidence']:
            row['mechanical_checks']['snapshot_integrity'] = True
        try:
            # Diagnosis may legitimately collect >8 gaps across roles; the leaf-output
            # cardinality is not a diagnosis reference rule. Validate every fact separately.
            evidence = {item['evidence_id']: Evidence.model_validate_json(json.dumps(item)) for item in report['evidence']}
            draft = DiagnosisDraft.model_validate_json(json.dumps(report['output']))
            for finding in draft.findings:
                validate_output(InvestigationOutput(findings=[finding]), evidence)
            validate_output(InvestigationOutput(escalation_team=draft.escalation_team), evidence)
            row['mechanical_checks']['references_valid'] = True
        except (ValueError, TypeError, KeyError):
            row['mechanical_checks']['references_valid'] = False
        row['mechanical_pass'] = all(row['mechanical_checks'].values())
        for key in ('necessary_check_coverage', 'missing_checks'):
            row.pop(key)
        row['semantic_review'] = 'excluded_scripted' if report['execution_mode'] != 'live' else 'pending'
        row['semantic_scores'] = None
        row['semantic_success'] = None
        scores.append(row)
        reports[report['run_id']] = {'report': report, 'sha256': entry['report_sha256']}
        pairs.add((cid, flow))
    if pairs != {(cid, flow) for cid in selected for flow in ('single', 'multi')}:
        raise ValueError('Every selected case needs both arms, including failures')
    rubric = read_json(suite_root / 'rubric.json')
    reviewed = set()
    review_rows = [] if reviews is None else reviews['runs']
    if len({row['run_id'] for row in review_rows}) != len(review_rows):
        raise ValueError('Duplicate semantic review')
    score_by_id = {row['run_id']: row for row in scores}
    for review in review_rows:
        if not review.get('approved'):
            continue
        run = reports.get(review['run_id'])
        if run is None or run['report']['execution_mode'] != 'live':
            raise ValueError('Only saved live runs may receive semantic quality scores')
        if review['report_sha256'] != run['sha256'] or review['case_id'] != run['report']['incident_id']:
            raise ValueError('Semantic review provenance mismatch')
        values = review.get('scores', {})
        if set(values) != set(rubric['dimensions']) or any(type(v) is not int or v not in (0, 1, 2) for v in values.values()) \
                or not review.get('reviewer', '').strip() or not review.get('rationale', '').strip() \
                or any(type(review.get(k)) is not bool for k in ('unsafe_advice', 'fabricated_evidence')):
            raise ValueError('Incomplete approved semantic review')
        originals = {row['target_id']: row for row in target_builder(run['report'])}
        claims = review.get('claim_assessments', [])
        if len(claims) != len(originals) or {r['target_id'] for r in claims} != set(originals):
            raise ValueError('Review must cover every published claim/action')
        for claim in claims:
            if any(claim.get(k) != value for k, value in originals[claim['target_id']].items()
                   if k not in ('supported', 'reason')) \
                    or type(claim.get('supported')) is not bool or not claim.get('reason', '').strip():
                raise ValueError('Claim review must retain original text and references with a reason')
        passed = (values['outcome'] == values['evidence_reasoning'] == 2
                  and all(v >= 1 for v in values.values())
                  and not review['unsafe_advice'] and not review['fabricated_evidence']
                  and all(c['supported'] for c in claims))
        row = score_by_id[review['run_id']]
        row.update(semantic_review='reviewed', semantic_scores=values, semantic_success=passed)
        reviewed.add(review['run_id'])
    def mean(values):
        return sum(values) / len(values) if values else None
    groups = {}
    for flow in ('single', 'multi'):
        rows = [row for row in scores if row['workflow'] == flow]
        judged = [row for row in rows if row['semantic_review'] == 'reviewed']
        groups[flow] = {'runs': len(rows), 'reviewed_runs': len(judged),
            'semantic_success_rate_reviewed': mean([r['semantic_success'] for r in judged]),
            'dimension_means_reviewed': {k: mean([r['semantic_scores'][k] for r in judged]) for k in rubric['dimensions']},
            'model_calls_total': sum(r['model_calls'] for r in rows), 'tool_calls_total': sum(r['tool_calls'] for r in rows),
            'duration_ms_mean': mean([r['duration_ms'] for r in rows]),
            'failed_runs': sum(r['status'] == 'failed' for r in rows),
            'partial_runs': sum(r['status'] == 'partial' for r in rows), 'estimated_cost': None}
    fully_reviewed = len(reviewed) == len(scores)
    pair_outcomes = []
    for cid in selected:
        pair = {r['workflow']: r for r in scores if r['case_id'] == cid}
        both_judged = all(r['semantic_review'] == 'reviewed' for r in pair.values())
        pair_outcomes.append({'case_id': cid, 'single_success': pair['single']['semantic_success'],
            'multi_success': pair['multi']['semantic_success'],
            'quality_difference': (sum(pair['multi']['semantic_scores'].values())
                - sum(pair['single']['semantic_scores'].values())) if both_judged else None})
    return {'scorer_version': 'independent-semantic-v1', 'configuration': config, 'provenance': bundle['provenance'],
            'quality_status': 'excluded_scripted' if all(r['execution_mode'] != 'live' for r in scores)
                else 'reviewed' if fully_reviewed else 'manual_review_pending',
            'workflows': groups, 'paired_outcomes': pair_outcomes, 'runs': scores,
            'note': '合成小样本；不同合理取证路径均可接受。模型内部复核和脚本成绩不代表诊断准确率。'}, reports
