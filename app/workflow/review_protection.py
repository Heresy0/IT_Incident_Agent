"""Conservative verdict downgrades; source presence is not semantic proof."""

POLICY = 'review_protection_v1'
BOUNDED_POLICY = 'bounded_refutation_v1'
DEFAULT_REVIEW_PROTECTION = BOUNDED_POLICY
MODES = {'strict_v1', 'conservative_v1', BOUNDED_POLICY}


def conservative(mode):
    return mode != 'strict_v1'


def protect_refutations(review, evidence, event, *, bounded=False, scope=None):
    """A missing current citation cannot justify displaying a cause as refuted.

    IDs must already be validated by the caller. A cited observation still
    needs semantic review; this guard never certifies a contradiction.
    """
    checked = []
    for assessment in review.assessments:
        code = None
        if assessment.target_id.startswith('H') and assessment.verdict == 'not_supported':
            observations = [evidence[eid] for eid in assessment.evidence_ids
                            if evidence[eid].kind == 'observation']
            basis = assessment.refutation_basis
            if not observations:
                code = 'NO_CURRENT_COUNTEREVIDENCE'
            elif bounded:
                if basis is None:
                    code = 'MISSING_REFUTATION_BASIS'
                elif basis.kind != 'direct_contradiction':
                    code = 'WEAKENING_IS_NOT_REFUTATION' if basis.kind == 'weakens_explanation' else 'ABSENCE_IS_NOT_REFUTATION'
                elif scope is None or any(e.service != scope.service or e.environment != scope.environment
                        or e.observed_from < scope.start or e.observed_to > scope.end
                        for e in observations):
                    code = 'COUNTEREVIDENCE_SCOPE_MISMATCH'
                else:
                    # A model label is not proof of semantic incompatibility.
                    event('refutation_basis_retained', 'reviewer', policy=BOUNDED_POLICY,
                          target_id=assessment.target_id, basis=basis.model_dump(mode='json'),
                          evidence_ids=assessment.evidence_ids, semantic_proof_verified=False)
        if code:
            event('review_protection_applied', 'reviewer', policy=BOUNDED_POLICY if bounded else POLICY,
                  code=code, target_id=assessment.target_id,
                  original=assessment.model_dump(mode='json'), verdict='uncertain',
                  source_presence_is_semantic_proof=False)
            assessment = assessment.model_copy(update={'verdict': 'uncertain',
                'reason': ('程序保护：反证依据不足或范围不匹配，保留为未确认；原复核理由：' + assessment.reason)[:500]})
        checked.append(assessment)
    return review.model_copy(update={'assessments': checked})
