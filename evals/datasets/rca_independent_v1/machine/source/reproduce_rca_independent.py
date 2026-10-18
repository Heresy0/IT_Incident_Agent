"""Run independent local mechanisms without importing application code or models."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.rca_independent_cases import execute_experiments, publish_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dataset-output', type=Path)
    parser.add_argument('--repeats', type=int, choices=(1, 3), default=3)
    args = parser.parse_args()
    manifest = execute_experiments(args.output, args.repeats)
    if manifest['status'] != 'passed':
        raise SystemExit('Retained experiment failure; no silent rerun or publication')
    if args.dataset_output:
        data = publish_dataset(args.output / 'manifest.json', args.dataset_output)
        print(data['validation'])
    print({'status': manifest['status'], 'cases': len(manifest['cases']), 'model_calls': 0})


if __name__ == '__main__':
    main()
