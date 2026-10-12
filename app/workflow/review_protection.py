"""Conservative verdict downgrades; source presence is not semantic proof."""

POLICY = 'review_protection_v1'
MODES = {'strict_v1', 'conservative_v1'}


def protect_refutations(review, evidence, event):
    """A missing current citation cannot justify displaying a cause as refuted.

    IDs must already be validated by the caller. A cited observation still
    needs semantic review; this guard never certifies a contradiction.
    """
    checked = []
    for assessment in review.assessments:
        if (assessment.target_id.startswith('H') and assessment.verdict == 'not_supported'
                and not any(evidence[eid].kind == 'observation' for eid in assessment.evidence_ids)):
            event('review_protection_applied', 'reviewer', policy=POLICY,
                  code='NO_CURRENT_COUNTEREVIDENCE', target_id=assessment.target_id,
                  original=assessment.model_dump(mode='json'), verdict='uncertain',
                  source_presence_is_semantic_proof=False)
            assessment = assessment.model_copy(update={'verdict': 'uncertain',
                'reason': ('程序保护：没有当前观测反证引用，保留为未确认；原复核理由：' + assessment.reason)[:500]})
        checked.append(assessment)
    return review.model_copy(update={'assessments': checked})
