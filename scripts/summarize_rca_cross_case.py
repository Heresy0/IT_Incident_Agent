"""Summarize frozen cross-mechanism observations and disclosed AI judgments."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.rca_contracts import read_json, sha_file
from evals.rca_scoring import write_json
from evals.rca_cross_case_scoring import build_scorecard
from scripts.review_rca import load_inputs
from scripts.summarize_rca_integration import reconcile_accounting
from scripts.evaluate_rca import verify_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--adjudication', type=Path)
    parser.add_argument('--rate-card', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bundle, dataset, records = load_inputs(args.bundle, args.dataset)
    verify_inputs(bundle['plan'], args.dataset, args.bundle.parent / 'plan.json')
    accounting = reconcile_accounting(bundle, [r['report'] for r in records])
    adjudication = read_json(args.adjudication) if args.adjudication else None
    result = build_scorecard(bundle, dataset, records, adjudication, read_json(args.rate_card))
    result['source_bindings'] = {key: {'path': str(path), 'sha256': sha_file(path)}
        for key, path in (('bundle', args.bundle), ('dataset', args.dataset), ('rate_card', args.rate_card))}
    if args.adjudication:
        result['source_bindings']['adjudication'] = {'path': str(args.adjudication), 'sha256': sha_file(args.adjudication)}
    result['ledger_reconciliation'] = accounting
    write_json(args.output, result)
    print({'output': str(args.output), 'recorded_runs': len(records),
        'adjudication_source': 'ai' if adjudication else 'pending', 'human_verified': False})


if __name__ == '__main__':
    main()
