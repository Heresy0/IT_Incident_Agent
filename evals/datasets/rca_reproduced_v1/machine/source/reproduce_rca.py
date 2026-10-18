"""Run bounded local RCA mechanism experiments without a model or application."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evals.rca_experiments import CASE_IDS, run_experiments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--cases', nargs='+', choices=CASE_IDS, default=list(CASE_IDS))
    args = parser.parse_args()
    result = run_experiments(args.output, args.repeats, args.cases)
    print(json.dumps({'status': result['status'], 'model_calls': 0,
        'human_verified': False, 'is_gold': False,
        'manifest': str(args.output / 'manifest.json'),
        'cases': [{key: item[key] for key in ('case_id', 'status', 'sha256')}
                  for item in result['cases']]}, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
