"""Versioned, intentionally favorable showcase cases, separate from blind evaluation."""
from pathlib import Path
from evals.independent import suite as common
from evals.showcase_revision.provider import DATA_ROOT, ShowcaseRevisionProvider

SUITE_ROOT = Path(__file__).resolve().parent
DEFAULT_SPLIT = 'development'
EVALUATION_FILES = ('scripts/compare_showcase_revision.py', 'evals/showcase_revision/provider.py',
                    'evals/showcase_revision/suite.py', 'evals/showcase_revision/scoring.py')
read_json, digest = common.read_json, common.digest


def verify_freeze():
    return common.verify_freeze(suite_root=SUITE_ROOT, data_root=DATA_ROOT)


def select_cases(split=DEFAULT_SPLIT, cases=None, release_holdout=False):
    return common.select_cases(split, cases, release_holdout, suite_root=SUITE_ROOT)


def check_suite():
    return common.check_suite(suite_root=SUITE_ROOT, data_root=DATA_ROOT,
                             provider_factory=ShowcaseRevisionProvider, answer_split=DEFAULT_SPLIT)
