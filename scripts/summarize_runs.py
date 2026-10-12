"""Generate offline JSON/Markdown statistics and a hash-bound human review worksheet."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_statistics import load_reports, summarize, review_template, markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='append', type=Path, default=[])
    parser.add_argument('--bundle', action='append', type=Path, default=[])
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--pricing', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='New output directory; existing files are preserved')
    args = parser.parse_args(argv)
    try:
        loaded = load_reports(args.report, args.bundle)
        read = lambda path: json.loads(path.read_text(encoding='utf-8')) if path else None
        summary = summarize(loaded, read(args.reviews), read(args.pricing))
        outputs = {'statistics.json': json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
                   'statistics.md': markdown(summary),
                   'human-reviews.template.json': json.dumps(review_template(loaded), ensure_ascii=False, indent=2) + '\n'}
        if any((args.output / name).exists() for name in outputs):
            raise ValueError('Output files already exist; choose a new output directory')
        args.output.mkdir(parents=True, exist_ok=True)
        for name, content in outputs.items():
            (args.output / name).write_text(content, encoding='utf-8')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(f"Offline statistics: {summary['runs_total']} runs, {len(summary['groups'])} groups")
    print(args.output.resolve() / 'statistics.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
