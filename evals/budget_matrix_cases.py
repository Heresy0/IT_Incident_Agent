"""Observation-only cases and scoring boundaries for the Flash budget matrix.

This reuses existing worlds, channel descriptions and fixed-prefix statements.
Only the evaluator receives acquisition targets or source-authored references;
neither is part of a model's runtime observation payload.
"""
from copy import deepcopy

from evals.frozen_version_cases import (
    audit_rubric as _source_audit_rubric,
    blind_audit_schema as _source_blind_audit_schema,
    build_case_specs as _source_case_specs,
    fingerprint,
    freeze_case_spec as _source_freeze_case_spec,
)


VERSION = 'flash-four-arm-budget-cases-v1'
ARMS = {
    'm12_r2': {'model_calls': 12, 'rework_new_reads': 2},
    'm12_r4': {'model_calls': 12, 'rework_new_reads': 4},
    'm16_r2': {'model_calls': 16, 'rework_new_reads': 2},
    'm16_r4': {'model_calls': 16, 'rework_new_reads': 4},
}
E2E_CASE_IDS = (
    'fixture_case_001', 'anonymous_case_001',
    'fixture_case_003', 'anonymous_case_003',
    'fixture_case_004', 'anonymous_case_004',
    'fixture_case_006', 'anonymous_case_006',
    'world_temporal_counterevidence', 'world_followup_available',
    'world_followup_unavailable',
)
# These are the two actual fixed_rework cases from the frozen role study.
# pool_probe_ambiguity was a review-only control there, not a fixed rework case.
FIXED_REWORK_CASE_IDS = ('world_followup_available', 'world_followup_unavailable')
STAGE_CASE_IDS = {'e2e': E2E_CASE_IDS, 'fixed_rework': FIXED_REWORK_CASE_IDS}
_PREFIX_OBSERVATIONS = {
    'world_followup_available': ('generic-failure',),
    'world_followup_unavailable': ('local-timeout',),
}


def _fixed_draft(controls):
    """Keep exact source-authored text; resolve selectors after real prefix reads."""
    claims, actions = [], []
    for control in controls:
        if control['kind'] == 'factual_relation':
            claims.append({'statement': control['statement'],
                'selectors': deepcopy(control['selectors']), 'counter_selectors': [],
                'level': control['level']})
        elif control['kind'] == 'check_appropriateness':
            actions.append({'action': control['action'], 'condition': control['condition'],
                'selectors': deepcopy(control['selectors']), 'kind': 'manual_check'})
        else:
            raise ValueError('Unknown existing source control kind')
    return {'claims': claims, 'next_steps': actions, 'gaps': []}


def _stage_acquisition(spec, initial):
    keys = list(spec['key_observation_ids'])
    initial_set = set(initial)
    expected_new = [key for key in keys if key not in initial_set]
    eligible = spec['eligible_for_positive_acquisition_rate']
    return {
        'initial_observation_ids': list(initial),
        'initial_key_observation_ids': [key for key in keys if key in initial_set],
        'expected_new_key_observation_ids': expected_new,
        'eligible_for_positive_acquisition_rate': eligible,
        'eligible_for_positive_new_acquisition_rate': eligible and bool(expected_new),
        'reference_kind': 'source_constructed_provisional_not_human_gold',
        'unknown_is_legitimate': not eligible and spec['world_kind'] == 'source_world',
    }


def build_case_specs(root=None, *, source_cases=None):
    """Return JSON-ready specs with identical worlds across the four budget arms.

    E2E starts without any prefix reads. Fixed rework repeats the earlier role
    study's real prefix, original draft, reviewer and challenger before rework.
    Initial review decisions are not invented or preloaded by this module.
    """
    existing = _source_case_specs(root, source_cases=source_cases)
    result = {}
    for case_id in E2E_CASE_IDS:
        spec = deepcopy(existing[case_id])
        fixed = case_id in FIXED_REWORK_CASE_IDS
        spec['stages'] = ['e2e', 'fixed_rework'] if fixed else ['e2e']
        spec['stage_acquisition'] = {'e2e': _stage_acquisition(spec, ())}
        spec['initial_observation_ids'] = list(_PREFIX_OBSERVATIONS.get(case_id, ()))
        if fixed:
            spec['stage_acquisition']['fixed_rework'] = _stage_acquisition(
                spec, spec['initial_observation_ids'])
            spec['fixed_draft'] = _fixed_draft(spec['review_controls'])
            spec['review_targets'] = deepcopy(spec['review_controls'])
        else:
            # Prefix checks and reference controls are not used in E2E.
            spec['initial_checks'] = []
            spec['review_controls'] = []
            spec['review_targets'] = []
        spec['budget_study_version'] = VERSION
        result[case_id] = spec
    return result


# Descriptive alias for callers which already use build_case_specs elsewhere.
build_budget_case_specs = build_case_specs


def freeze_case_spec(spec):
    frozen = _source_freeze_case_spec(spec)
    frozen.update({
        'budget_study_version': VERSION,
        'stage_acquisition': deepcopy(spec['stage_acquisition']),
        'initial_observation_ids': list(spec['initial_observation_ids']),
        'fixed_draft_sha256': fingerprint(spec.get('fixed_draft')),
        'fixed_prefix_sha256': fingerprint(spec['initial_checks']),
        'review_targets_sha256': fingerprint(spec['review_targets']),
        'budget_reference_sha256': fingerprint({key: spec[key] for key in (
            'stage_acquisition', 'initial_observation_ids', 'review_targets')}),
    })
    return frozen


def study_design(repeats=3):
    """Descriptive unsealed draft; root still freezes source/settings and paid cap."""
    if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 5:
        raise ValueError('Repeats must be an integer from one to five')
    stages = {stage: {'cases': list(cases), 'repeats': repeats,
        'runs': len(cases) * len(ARMS) * repeats,
        'maximum_model_requests': len(cases) * repeats * sum(
            arm['model_calls'] for arm in ARMS.values())}
        for stage, cases in STAGE_CASE_IDS.items()}
    return {'version': VERSION, 'arms': deepcopy(ARMS), 'stages': stages,
        'planned_runs': sum(stage['runs'] for stage in stages.values()),
        'maximum_model_requests': sum(stage['maximum_model_requests'] for stage in stages.values()),
        'model': 'qwen3.7-flash', 'same_prompt_source_required': True,
        'same_model_settings_required': True,
        'unchanged_limits': {'tool_calls': 8, 'seconds': 180, 'input_chars': 48000},
        'analysis_gate': 'hold_pending_paired_data_and_non_gold_semantic_audit',
        'is_final_prespecified_plan': False, 'paid_requests_sent': 0}


def audit_rubric():
    rubric = _source_audit_rubric()
    rubric['version'] = 'flash-budget-matrix-source-audit-v1'
    rubric['natural_outputs'] = 'Audit final hypotheses, published recommendations and pending recommendations, including failed/partial runs. Program-derived findings are excluded from the semantic item denominator but retained as raw acquired-source material. No final claim means no_claims, not semantic correctness. Never transfer labels from original fixed claims to newly generated wording.'
    rubric['fixed_controls'] = 'All four arms use the current native reviewer. Compare supported/partial/contradicted/insufficient factual relations against the whole unchanged original target and preserve native fields. Whole-target binary acceptance requires supported (or appropriate for an action); label mismatch, wrong acceptance and unassessed status remain separate. There is no legacy contract mapping.'
    rubric['budget_matrix'] = {
        'primary_acquisition': 'Score source-authored key observation IDs only when available. In fixed_rework subtract the actual prefix observation set before counting new acquisition; never count an already-read key as a successful follow-up.',
        'coverage': 'Keep fixture observation coverage separate from source-key acquisition. Identifier-renamed variants share their original world and are not independent incidents.',
        'unavailable': 'A source-unavailable world has no positive new-key denominator. Score boundary preservation and relevant attempted checks separately; additional calls or an invented cause are not success.',
        'repeated_decisions': 'Count raw model decision signatures separately from executed tool-query signatures, covered-query feedback, and budget stops. A covered repeated query is not a new observation. Do not infer repetition solely from an unchanged evidence count.',
        'semantic_controls': 'Only original fixed-prefix targets use existing source-authored relations. Preserve primary, post-challenge and final stances separately; revised wording requires a new source audit and never inherits original labels.',
        'causal_readout': 'Budget arms change allowance, not guaranteed consumption. Report unused allowance, premature stops and exact reasons. An absent rework due to review gating is not investigation-model failure by itself.',
        'cost_latency': 'Report per-run actual model usage and end-to-end wall time for matched stage/case/repeat blocks; unknown token usage remains unknown. Price noncached estimated cost with a dated external rate card, not provider billing.',
        'decision': 'Do not promote a default budget based on completion alone. Describe key acquisition, repetition, semantic candidates, cost and latency jointly; three repeats and authored worlds do not establish production accuracy.',
    }
    return rubric


def blind_audit_schema():
    schema = _source_blind_audit_schema()
    schema['version'] = 'flash-budget-matrix-blind-audit-packet-v1'
    schema['forbidden_identity_fields'] = list(dict.fromkeys([
        *schema['forbidden_identity_fields'], 'model_calls', 'rework_new_reads',
        'budget_arm', 'budget_limits', 'repeat', 'stage',
    ]))
    schema['audit_item_stability'] = 'Exact text plus condition hashes bind the judgment. Budget identity and initial/revised/published native stance remain in the private map.'
    return schema
