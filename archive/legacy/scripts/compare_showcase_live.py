"""Bounded genuine model comparison on explicitly curated synthetic scenarios."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts import compare_independent as runner
from evals.showcase_live import suite, scoring
from evals.showcase_live.provider import ShowcaseProvider


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--output' not in argv and not any(a.startswith('--output=') for a in argv):
        argv += ['--output', str(ROOT / 'output/showcase-live-comparison')]
    return runner.main(argv, suite=suite, provider_factory=ShowcaseProvider, scoring=scoring)


if __name__ == '__main__':
    raise SystemExit(main())
