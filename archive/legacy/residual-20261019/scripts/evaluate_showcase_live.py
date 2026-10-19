"""Offline review of a saved curated batch. Never loads model keys or starts runs."""
import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from evals.common import write_json
from evals.independent.suite import read_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('showcase_live', 'showcase_revision'), required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    scoring = importlib.import_module(f'evals.{args.suite}.scoring')
    summary, _ = scoring.evaluate(read_json(args.bundle), args.bundle.parent,
                                 read_json(args.reviews) if args.reviews else None)
    write_json(args.output, summary)
    print(summary['quality_status'])
    return 0 if all(row['mechanical_pass'] for row in summary['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
