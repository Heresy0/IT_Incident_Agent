"""Reuse provenance and semantic review; never equate internal passed with quality."""
from evals.independent import scoring as common
from evals.targeted.scoring import published_targets
from evals.showcase_revision.suite import SUITE_ROOT, DATA_ROOT


def semantic_template(report, report_hash):
    return common.semantic_template(report, report_hash, suite_root=SUITE_ROOT,
                                    target_builder=published_targets)


def evaluate(bundle, directory, reviews=None):
    result, reports = common.evaluate(bundle, directory, reviews, suite_root=SUITE_ROOT,
                                      data_root=DATA_ROOT, target_builder=published_targets)
    result['note'] = '定向设计的开发展示集，真实模型、合成IT观测；不是独立测试或普遍优势证明。'
    return result, reports
