"""Separate frozen probes, with reserved runs disabled by default."""
from pathlib import Path
from evals.independent import suite as common
from evals.targeted.provider import DATA_ROOT, TargetedProvider

SUITE_ROOT = Path(__file__).resolve().parent
DEFAULT_SPLIT = 'development'
EVALUATION_FILES = ('scripts/compare_targeted.py', 'scripts/evaluate_targeted.py',
                    'evals/targeted/suite.py', 'evals/targeted/provider.py', 'evals/targeted/scoring.py')
read_json, digest = common.read_json, common.digest


def verify_freeze():
    return common.verify_freeze(suite_root=SUITE_ROOT, data_root=DATA_ROOT)


def select_cases(split=DEFAULT_SPLIT, cases=None, release_holdout=False):
    return common.select_cases(split, cases, release_holdout, suite_root=SUITE_ROOT)


def check_suite():
    return common.check_suite(suite_root=SUITE_ROOT, data_root=DATA_ROOT,
                             provider_factory=TargetedProvider, answer_split=DEFAULT_SPLIT)
