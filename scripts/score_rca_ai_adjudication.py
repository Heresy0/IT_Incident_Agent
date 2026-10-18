"""Score completed, explicitly AI-origin RCA judgments without human attestation.

Uses the existing scoring formula and frozen reports. Human review imports and
their formal metrics are intentionally separate and remain unchanged.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.rca_scoring import (ROOT_FIELDS, IDENTIFIABILITY, aggregate_case_metrics,
    mechanical_references_valid, read_json, require, score_run, sha_file, write_json)
from scripts.review_rca import load_bundles, visible_items
from scripts.summarize_rca_integration import citation_observation, load_bundle

RELATIONS = {'supported', 'partial', 'contradicted', 'insufficient'}
JUDGMENT_FIELDS = ROOT_FIELDS + ('established_assertions_supported',)


def verify_citations(document, native_records, diagnostic_records=(), *, artifact_root=ROOT):
    """Check saved evidence paths/hashes and bind decisive native references."""
    root = Path(artifact_root).resolve()
    count, files = 0, set()
    reports = {r['entry']['report_id']: Path(r['report_path']).resolve() for r in native_records}

    def check(citation, expected=None):
        nonlocal count
        key = 'report_path' if 'report_path' in citation else 'path'
        target = (root / citation[key]).resolve()
        require(target.is_relative_to(root) and target.is_file(), 'CITATION_FILE_MISSING_OR_ESCAPED')
        if key == 'report_path':
            require(expected is not None and target == expected, 'DECISIVE_CITATION_REPORT_MISMATCH')
        else:
            require(citation.get('sha256') == sha_file(target), 'CITATION_HASH_MISMATCH')
        path = citation.get('json_path')
        if target.suffix == '.json':
            require(isinstance(path, str) and re.fullmatch(r'\$(?:\.[\w-]+|\[-?\d+\]|\[\*\])*', path),
                'CITATION_JSON_PATH_INVALID')
            values = [read_json(target)]
            for name, index in re.findall(r'\.([\w-]+)|\[(-?\d+|\*)\]', path[1:]):
                selected = []
                for value in values:
                    try:
                        if name:
                            selected.append(value[name])
                        elif index == '*':
                            selected.extend(value.values() if isinstance(value, dict) else value)
                        else:
                            selected.append(value[int(index)])
                    except (KeyError, TypeError, IndexError, ValueError):
                        continue
                require(bool(selected), 'CITATION_JSON_PATH_MISSING')
                values = selected
        count += 1
        files.add(str(target))
        return target

    def walk(value, expected=None):
        if isinstance(value, dict):
            if ('path' in value or 'report_path' in value) and 'json_path' in value:
                check(value, expected)
            for child in value.values():
                walk(child, expected)
        elif isinstance(value, list):
            for child in value:
                walk(child, expected)

    for case in document['cases']:
        walk(case['truth'])
        for judgment in case['native_runs']:
            expected = reports.get(judgment['report_id'])
            require(expected is not None, 'UNKNOWN_CITATION_REPORT')
            decisive = judgment.get('decisive_citations', [])
            require(any((root / c.get('report_path', c.get('path', ''))).resolve() == expected
                for c in decisive), 'DECISIVE_REPORT_CITATION_REQUIRED')
            walk(judgment, expected)
        for judgment in case['diagnostic_runs']:
            matching = next((r for r in diagnostic_records if r['report']['report_id'] == judgment['report_id']), None)
            expected = Path(matching['report_path']).resolve() if matching is not None else None
            walk(judgment, expected)
    return {'status': 'passed', 'citations_checked': count, 'distinct_files': len(files)}


def _origin(value):
    require(value.get('reviewer_type') == 'ai', 'AI_ORIGIN_REQUIRED')
    require(value.get('human_verified') is False and value.get('is_gold') is False,
        'AI_CANNOT_ATTEST_HUMAN_OR_GOLD')
    require(value.get('nonblind') is True, 'NONBLIND_REVIEW_MUST_BE_DISCLOSED')


def _ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
        'rate': numerator / denominator if denominator else None}


def score_adjudication(document, native_records, diagnostic_records, *, repeats):
    """All report identities and complete judgments must be present exactly once."""
    _origin(document)
    require(document.get('schema_version') == 'rca-ai-adjudication-v1', 'WRONG_ADJUDICATION_SCHEMA')
    require(document.get('status') == 'completed', 'ADJUDICATION_NOT_COMPLETE')
    cases = document.get('cases', [])
    ids = [c['case_id'] for c in cases]
    require(len(ids) == len(set(ids)), 'DUPLICATE_CASE')
    native = {r['entry']['report_id']: r for r in native_records}
    diagnostic = {r['report']['report_id']: r for r in diagnostic_records}
    require(bool(native), 'NATIVE_REPORTS_REQUIRED')
    require(len(native) == len(native_records) and len(diagnostic) == len(diagnostic_records),
        'DUPLICATE_SOURCE_REPORT')
    require(set(ids) == {r['entry']['case_id'] for r in native_records}, 'CASE_COVERAGE_MISMATCH')
    rows, reviewed, semantic, reviewed_diagnostic = [], set(), [], set()
    for case in cases:
        _origin(case)
        require(case.get('schema_version') == 'rca-ai-case-adjudication-v1', 'WRONG_CASE_SCHEMA')
        truth = case['truth']
        kind = truth['identifiability']
        require(kind in IDENTIFIABILITY and bool(truth.get('rationale'))
            and bool(truth.get('citations')), 'TRUTH_JUDGMENT_INCOMPLETE')
        for judgment in case['native_runs']:
            rid = judgment['report_id']
            require(rid in native and rid not in reviewed, 'UNKNOWN_OR_DUPLICATE_NATIVE_JUDGMENT')
            record = native[rid]
            entry, report = record['entry'], record['report']
            require(judgment['report_sha256'] == entry['report_sha256'], 'NATIVE_REPORT_HASH_MISMATCH')
            require(all(judgment.get(k) == entry.get(k) for k in ('case_id', 'stage', 'repeat'))
                and entry['case_id'] == case['case_id'], 'NATIVE_JUDGMENT_IDENTITY_MISMATCH')
            flags = judgment['judgment']
            require(set(flags) == set(JUDGMENT_FIELDS)
                and all(type(flags[k]) is bool for k in JUDGMENT_FIELDS), 'COMPLETE_BINARY_JUDGMENT_REQUIRED')
            require(all(isinstance(judgment.get('rationale_by_field', {}).get(k), str)
                and judgment['rationale_by_field'][k].strip() for k in JUDGMENT_FIELDS)
                and bool(judgment.get('decisive_citations')), 'JUDGMENT_RATIONALE_REQUIRED')
            metrics = score_run({}, flags, kind, has_output=bool(visible_items(report)),
                delivery_failed=bool(report.get('execution_error') or report.get('harness_failure')
                    or report.get('native_report', {}).get('execution_error')),
                mechanical_references_valid=mechanical_references_valid(report),
                established_assertions_supported=flags['established_assertions_supported'])
            rows.append({'report_id': rid, 'condition': entry['system_id'], 'mode': entry['stage'],
                'case_id': entry['case_id'], 'repeat': entry['repeat'], 'identifiability': kind,
                'judgment': flags, 'metrics': metrics})
            reviewed.add(rid)
        for judgment in case['diagnostic_runs']:
            rid = judgment['report_id']
            require(rid in diagnostic and rid not in reviewed_diagnostic, 'UNKNOWN_OR_DUPLICATE_DIAGNOSTIC_JUDGMENT')
            record = diagnostic[rid]
            report = record['report']
            require(judgment['report_sha256'] == record['sha256'], 'DIAGNOSTIC_REPORT_HASH_MISMATCH')
            require(report['case_id'] == case['case_id'] and all(judgment.get(k) == report.get(k)
                for k in ('group_id', 'condition', 'repeat')), 'DIAGNOSTIC_JUDGMENT_IDENTITY_MISMATCH')
            observations = citation_observation(report)
            require(len(observations['primary_review']) == len(observations['final_review']) == 1,
                'SINGLE_CONTROL_REVIEW_REQUIRED')
            primary, final = observations['primary_review'][0], observations['final_review'][0]
            require(judgment['primary_relation'] == primary['relation']
                and judgment['final_relation'] == final['relation'], 'RECORDED_VERDICT_MISMATCH')
            eligible = judgment['eligible_for_semantic_accuracy']
            require(type(eligible) is bool and bool(judgment.get('rationale'))
                and bool(judgment.get('citations')), 'SEMANTIC_JUDGMENT_INCOMPLETE')
            acceptable = judgment.get('acceptable_relations', [])
            if report['group_id'].startswith('legacy_') and report['condition'] == 'wording_v1':
                require(eligible is False, 'AMBIGUOUS_LEGACY_CONTROL_MUST_BE_EXCLUDED')
            if eligible:
                require(bool(acceptable) and len(acceptable) == len(set(acceptable))
                    and set(acceptable) <= RELATIONS and judgment.get('expected_relation') in acceptable,
                    'EXPECTED_RELATION_REQUIRED')
            else:
                require(bool(judgment.get('exclusion_reason')), 'EXCLUSION_REASON_REQUIRED')
            semantic.append({'report_id': rid, 'case_id': case['case_id'], 'group_id': report['group_id'],
                'condition': report['condition'], 'repeat': report['repeat'], 'eligible': eligible,
                'exclusion_reason': judgment.get('exclusion_reason'), 'acceptable_relations': acceptable,
                'primary_relation': primary['relation'], 'final_relation': final['relation'],
                'primary_correct': primary['protocol_status'] == 'valid' and primary['relation'] in acceptable if eligible else None,
                'final_correct': final['protocol_status'] == 'valid' and final['relation'] in acceptable if eligible else None})
            reviewed_diagnostic.add(rid)
    require(reviewed == set(native), 'NATIVE_REVIEW_COVERAGE_INCOMPLETE')
    require(reviewed_diagnostic == set(diagnostic), 'DIAGNOSTIC_REVIEW_COVERAGE_INCOMPLETE')
    require(all(n == repeats for n in Counter((r['group_id'], r['condition']) for r in semantic).values()),
        'INCOMPLETE_DIAGNOSTIC_REPEAT_BLOCK')
    metrics = aggregate_case_metrics(rows, expected_repeats=repeats)
    eligible = [r for r in semantic if r['eligible']]
    groups = defaultdict(list)
    for row in eligible:
        groups[(row['group_id'], row['condition'])].append(row)
    def accuracy(selected):
        return {phase: _ratio(sum(r[phase + '_correct'] for r in selected), len(selected))
            for phase in ('primary', 'final')}
    next_step = {}
    for stage in sorted({r['mode'] for r in rows}):
        selected = [r for r in rows if r['mode'] == stage and r['identifiability'] != 'root_identifiable']
        next_step[stage] = _ratio(sum(r['judgment']['discriminating_next_step_proposed'] for r in selected), len(selected))
    return {'schema_version': 'rca-ai-scorecard-v1', 'status': 'scored',
        'adjudication_source': 'ai', 'reviewer_type': 'ai', 'nonblind': True,
        'human_verified': False, 'is_gold': False, 'human_verified_formal_metrics': None,
        'candidate_case_count': len(cases), 'native_reports_scored': len(rows),
        'ai_adjudicated_metrics': metrics, 'native_run_scores': rows,
        'discriminating_next_step_on_incomplete_root_cases': next_step,
        'diagnostic_semantics': {'reviewed_reports': len(semantic), 'eligible_reports': len(eligible),
            'excluded_reports': len(semantic) - len(eligible),
            'excluded_reasons': dict(Counter(r['exclusion_reason'] for r in semantic if not r['eligible'])),
            'accuracy': accuracy(eligible),
            'groups': [{'group_id': key[0], 'condition': key[1], 'runs': len(group), **accuracy(group)}
                for key, group in sorted(groups.items())], 'run_scores': semantic},
        'production_accuracy': None, 'independent_holdout_accuracy': None,
        'limitations': ['Completed AI adjudication; not a human attestation or independent human gold.',
            'Three development cases with three correlated repetitions; no production accuracy claim.',
            'Strict root, mechanism and unknown-boundary success have separate eligible denominators.',
            'Historical ambiguous wording is retained but excluded from semantic accuracy.',
            'Root scoring uses final published assertions; draft/review errors are recorded separately.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adjudication', type=Path, required=True)
    parser.add_argument('--native-bundle', type=Path, required=True)
    parser.add_argument('--diagnostic-bundle', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    bundles, dataset, records, sources, _ = load_bundles([args.native_bundle], args.dataset)
    native = bundles[0]
    require(not native.get('scheduled_pending') and len(records) == native['plan']['planned_runs'],
        'NATIVE_EXECUTION_INCOMPLETE')
    diagnostic, reports = load_bundle(args.diagnostic_bundle)
    require(not diagnostic.get('scheduled_pending'), 'DIAGNOSTIC_EXECUTION_INCOMPLETE')
    require(native['plan']['repeats'] == diagnostic['plan']['repeats'], 'SOURCE_REPEAT_MISMATCH')
    require(native['execution_mode'] == diagnostic['execution_mode'] == 'live_observation_only',
        'LIVE_REPORTS_REQUIRED')
    entries = {r['report_id']: r for r in diagnostic['runs']}
    diagnostic_records = [{'report': report, 'sha256': entries[report['report_id']]['report_sha256'],
        'report_path': str(args.diagnostic_bundle.resolve().parent / entries[report['report_id']]['report_file'])}
        for report in reports]
    document = read_json(args.adjudication)
    integrity = verify_citations(document, records, diagnostic_records)
    result = score_adjudication(document, records, diagnostic_records,
        repeats=native['plan']['repeats'])
    for field in ('model', 'model_settings', 'limits', 'provider_configuration_sha256'):
        require(native['plan'][field] == diagnostic['plan'][field], 'SOURCE_PLAN_MISMATCH:' + field)
    result.update(created_at_utc=datetime.now(timezone.utc).isoformat(),
        dataset_fingerprint=dataset['fingerprint'], model=native['plan']['model'],
        inputs={'adjudication': {'path': args.adjudication.as_posix(), 'sha256': sha_file(args.adjudication)},
            'native_bundle': sources[0], 'diagnostic_bundle': {'path': args.diagnostic_bundle.as_posix(),
                'sha256': sha_file(args.diagnostic_bundle)},
            'scoring_source': {'path': 'evals/rca_scoring.py', 'sha256': sha_file(ROOT / 'evals/rca_scoring.py')},
            'scoring_entrypoint': {'path': 'scripts/score_rca_ai_adjudication.py', 'sha256': sha_file(__file__)}},
        citation_integrity=integrity, new_evaluated_provider_calls=0)
    write_json(args.output, result)
    print({'output': str(args.output), 'status': result['status'],
        'native_reports_scored': result['native_reports_scored'], 'human_verified': False})


if __name__ == '__main__':
    main()
