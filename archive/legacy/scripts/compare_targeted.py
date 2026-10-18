"""Targeted pairs using the existing bounded runner; default is free planning."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]
from scripts import compare_independent as runner
from evals.targeted import suite, scoring
from evals.targeted.provider import TargetedProvider


def parse_args(argv=None):
    return runner.parse_args(argv, suite=suite)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--output' not in argv and not any(a.startswith('--output=') for a in argv):
        argv += ['--output', str(ROOT / 'output/targeted-comparison')]
    return runner.main(argv, suite=suite, provider_factory=TargetedProvider, scoring=scoring)


if __name__ == '__main__':
    raise SystemExit(main())
