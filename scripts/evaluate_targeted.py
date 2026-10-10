"""Score saved targeted pairs offline; no model, credentials or reruns."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from evals.targeted.scoring import evaluate_batches
from evals.targeted.suite import read_json
from evals.common import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--bundle', type=Path)
    selection.add_argument('--bundles', type=Path, nargs='+')
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    paths = args.bundles or [args.bundle]
    summary, _ = evaluate_batches([(read_json(path), path.parent) for path in paths],
                                 read_json(args.reviews) if args.reviews else None)
    write_json(args.output, summary)
    print(summary['quality_status'])
    return 0 if all(row['mechanical_pass'] for row in summary['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
