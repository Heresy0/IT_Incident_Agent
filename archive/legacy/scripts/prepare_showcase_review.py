"""Verify a historical paired fixture run and prepare an offline review packet.

Original reports remain unchanged. The public export contains metrics/hashes,
not raw evidence, prompts or model prose. Human judgments remain unfilled.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from evals.common import write_json
from evals.incident.comparison import evaluate, review_entry, targets


METRIC_KEYS = ('case_id', 'run_id', 'workflow', 'execution_mode', 'status', 'review_status',
               'mechanical_checks', 'mechanical_pass', 'necessary_check_coverage', 'missing_checks',
               'invalid_tool_results', 'empty_queries', 'truncated_queries', 'rework_rounds',
               'model_calls', 'tool_calls', 'duration_ms', 'token_usage', 'estimated_cost', 'termination_reason')


def project_history(bundle, directory, assistant_review=None):
    summary, reports = evaluate(bundle, directory)
    if bundle['configuration']['execution_mode'] != 'live':
        raise ValueError('Historical model comparison requires an exact live bundle')
    for run in reports.values():
        report = run['report']
        if report['data_source'] != 'synthetic_fixture' or any(
                e['provider'] != 'synthetic_fixture' or not e['data_version'].startswith('synthetic-')
                for e in report['evidence']):
            raise ValueError('Public comparison accepts synthetic fixture observations only')
    reviewed = {}
    if assistant_review is not None:
        if assistant_review.get('reviewer_kind') != 'assistant' or assistant_review.get('human_review_status') != 'pending':
            raise ValueError('Assistant analysis must not impersonate a human review')
        for row in assistant_review['runs']:
            if row['run_id'] in reviewed:
                raise ValueError('Duplicate auxiliary review')
            run = reports.get(row['run_id'])
            if run is None or row['report_sha256'] != run['sha256']:
                raise ValueError('Auxiliary review report hash mismatch')
            original_ids = {t['target_id'] for t in targets(run['report'])}
            claims = row['claim_assessments']
            if len(claims) != len(original_ids) or {c['target_id'] for c in claims} != original_ids:
                raise ValueError('Auxiliary review must cover all original claims')
            if any(c.get('verdict') not in {'supported', 'qualified', 'insufficient'} or not c.get('reason') for c in claims):
                raise ValueError('Every auxiliary claim needs a verdict and explanation')
            reviewed[row['run_id']] = row
        if set(reviewed) != set(reports):
            raise ValueError('Auxiliary review must cover every paired run')
    rows = []
    for score in summary['runs']:
        run = reports[score['run_id']]
        rows.append({**{k: score[k] for k in METRIC_KEYS}, 'report_sha256': run['sha256'],
                     'prompt_version': run['report']['prompt_version'],
                     'hypothesis_count': len(run['report']['output']['hypotheses']),
                     'action_count': len(run['report']['output']['recommended_actions']),
                     'auxiliary_review': reviewed.get(score['run_id'])})
    exported = {
        'schema_version': 'historical-showcase-v1', 'historical_only': True,
        'current_version_model_quality_verified': False,
        'data_source': 'synthetic_fixture', 'sample_pairs': len(rows) // 2,
        'configuration': summary['configuration'], 'provenance': summary['provenance'],
        'bundle_sha256': hashlib.sha256((directory / 'bundle.json').read_bytes()).hexdigest(),
        'runs': rows, 'human_review_status': 'pending',
        'semantic_accuracy': None, 'estimated_cost': None,
        'notes': ['机械检查与辅助语义分析分开；辅助分析由 Codex 完成，不计入人工评分。',
                  '历史版本的一组开发案例不能证明当前版本质量或总体准确率。',
                  '保留 partial 和失败，不自动提高预算或重新调用模型。'],
    }
    return exported, reports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--assistant-review', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/showcase-review')
    parser.add_argument('--public-output', type=Path, help='Explicit opt-in export of the safe metrics projection')
    args = parser.parse_args(argv)
    directory = args.bundle.resolve().parent
    if args.bundle.name != 'bundle.json':
        parser.error('Select the original bundle.json; original files are not edited')
    bundle = json.loads(args.bundle.read_text(encoding='utf-8'))
    review = json.loads(args.assistant_review.read_text(encoding='utf-8')) if args.assistant_review else None
    exported, reports = project_history(bundle, directory, review)
    destination = args.output.resolve() / str(uuid4())
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / 'comparison.json', exported)
    entries = []
    for run in reports.values():
        report = run['report']
        gold = json.loads((ROOT / 'evals/incident/gold' / f"{report['incident_id']}.json").read_text(encoding='utf-8'))
        entries.append(review_entry(report, run['sha256'], gold))
    write_json(destination / 'human-review-template.json', {'reviewer_kind': 'human', 'runs': entries})
    if args.public_output:
        if args.public_output.resolve().exists():
            parser.error('Public output already exists; choose a new file to preserve existing artifacts')
        write_json(args.public_output.resolve(), exported)
    print(json.dumps({'directory': str(destination), 'sample_pairs': exported['sample_pairs'],
                      'mechanical_passes': sum(r['mechanical_pass'] for r in exported['runs']),
                      'auxiliary_reviewed_runs': sum(r['auxiliary_review'] is not None for r in exported['runs']),
                      'human_review_status': 'pending', 'paid_calls': 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
