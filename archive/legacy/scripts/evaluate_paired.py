"""Prepare frozen experiments or summarize saved pairs offline. Never calls models."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from evals.paired_metrics import prepare_experiment, summarize_pairs, markdown
from evals.independent.suite import read_json
from evals.common import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Read configuration and freeze a free experiment plan')
    prepare.add_argument('--experiment-id', required=True)
    prepare.add_argument('--repeats', type=int, choices=(1, 2), default=1)
    prepare.add_argument('--model')
    prepare.add_argument('--cases', nargs='+', help='Freeze only these validation cases; rubric and budgets stay unchanged')
    prepare.add_argument('--output', type=Path, required=True, help='New experiment.json path')
    summary = commands.add_parser('summarize', help='Only read immutable saved bundles and optional human reviews')
    summary.add_argument('--experiment-file', type=Path, required=True)
    summary.add_argument('--bundle', type=Path, action='append', required=True)
    summary.add_argument('--reviews', type=Path)
    summary.add_argument('--historical', action='store_true', help='Partition legacy versions; never count them as current results')
    summary.add_argument('--output', type=Path, required=True, help='New result directory')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            if args.output.exists():
                raise ValueError('Experiment file exists; preserved')
            # Intentional config read, but no auth file, credential validation or model initialization.
            from config.incident import IncidentConfig
            config = IncidentConfig.from_file()
            if args.model:
                config = replace(config, model=args.model)
            result = prepare_experiment(config, args.experiment_id, repeats=args.repeats, cases=args.cases)
            write_json(args.output, result)
            print(json.dumps({'experiment_file': str(args.output.resolve()), 'model': result['model'],
                'planned_runs': result['planned_runs'], 'maximum_model_calls': result['maximum_model_calls'],
                'paid_requests_sent': 0}, ensure_ascii=False, indent=2))
        else:
            names = ('metrics.json', 'metrics.md', 'human-reviews.template.json')
            if any((args.output / name).exists() for name in names):
                raise ValueError('Output files exist; choose a new directory')
            result, worksheet = summarize_pairs(args.bundle, read_json(args.experiment_file),
                read_json(args.reviews) if args.reviews else None, historical=args.historical)
            write_json(args.output / names[0], result)
            write_json(args.output / names[2], worksheet)
            (args.output / names[1]).write_text(markdown(result), encoding='utf-8')
            print(json.dumps({'runs': result['runs_total'], 'cohorts': len(result['cohorts']),
                'historical_only': result['historical_only'],
                'quality_status': [g['quality_status'] for g in result['cohorts']],
                'report': str((args.output / names[1]).resolve())}, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
