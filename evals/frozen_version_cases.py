"""Observation-only version comparisons and a predeclared, non-gold audit rubric.

This module imports only the standard library at import time. Workers receive
``world`` and ``capability_metadata`` data, never reference labels or audit
decisions. Original reports retain each engine's native contracts and verdicts.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


VERSION = 'frozen-native-version-comparison-cases-v1'
RUBRIC_VERSION = 'frozen-native-source-audit-v1'
FIXTURE_IDS = ('case_001', 'case_003', 'case_004', 'case_006')
E2E_SOURCE_IDS = ('temporal_counterevidence', 'followup_available', 'followup_unavailable')
FIXTURE_COVERAGE_IDS = {
    'case_001': ('checkout-timeout', 'upstream-health'),
    'case_003': ('pool-error', 'traffic-change'),
    'case_004': ('log-0', 'change-1'),
    'case_006': ('log-0', 'change-1'),
}

# Input descriptions state quantities and observation boundaries, not causes.
_METRICS = {
    'db_cpu': ('Processor utilization observed at the database host.', 'database host processor'),
    'dependency_error_rate': ('Fraction of outgoing dependency requests that failed.', 'outgoing dependency requests'),
    'pool_usage': ('Occupied connections as a fraction of application connection-pool capacity.', 'application connection pool'),
    'pool_wait': ('Time spent waiting to acquire an application connection-pool slot.', 'connection acquisition queue'),
    'request_error_rate': ('Fraction of inbound requests that ended with an error.', 'service inbound requests'),
    'request_latency': ('Observed duration of inbound request processing.', 'service inbound request duration'),
    'request_rate': ('Inbound requests processed per unit time.', 'service inbound request volume'),
}
_LOGS = {
    'dependency': 'Events at the outgoing request boundary, including response outcomes and health controls.',
    'configuration': 'Events from local configuration loading and validation.',
    'resource': 'Events concerning local process capacity, queues and resource acquisition.',
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def opaque(prefix, value):
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]


def _anonymous_worlds(worlds):
    """Same identifier transformation as the earlier FrozenWorld comparison."""
    services, metrics, aliases = set(), set(), set()
    for world in worlds.values():
        scope = world['ticket']['scope']
        services.update([scope['service'], *scope.get('allowed_dependencies', [])])
        for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
            services.update(row['service'] for row in world[kind])
        metrics.update(row['metric'] for row in world['metrics'])
        aliases.update(alias for row in world['owners'] for alias in row.get('aliases', []))
    service_map = {name: opaque('service_', name) for name in sorted(services)}
    metric_map = {name: opaque('signal_', name) for name in sorted(metrics)}
    alias_map = {name: service_map.get(name, opaque('alias_', name)) for name in sorted(aliases)}
    replacements = {**alias_map, **service_map, **metric_map}

    def rename(value):
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename(item) for item in value]
        if not isinstance(value, str):
            return value
        if value in replacements:
            return replacements[value]
        for original in sorted(replacements, key=len, reverse=True):
            pattern = re.escape(original).replace(r'\-', r'[- _]')
            value = re.sub(r'(?<![\w])' + pattern + r'(?![\w])', replacements[original],
                           value, flags=re.IGNORECASE)
        return value

    result = {}
    for cid, original in worlds.items():
        definitions = {}
        for row in original['metrics']:
            description, observed_object = _METRICS.get(row['metric'], ('unknown', 'unknown'))
            definitions[metric_map[row['metric']]] = {
                'description': description, 'observed_object': observed_object,
                'unit': row.get('unit', ''), 'aggregation': row.get('aggregation', 'unknown'),
                'sampling': row.get('granularity', 'unknown'),
                'source': 'evaluation input quantity description', 'version': 'anonymous-world-v1'}
        world = rename(original)
        world['ticket']['incident_id'] = opaque('incident_', original['ticket']['incident_id'])
        world['ticket']['title'] = 'Synthetic incident with renamed observation identifiers'
        for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
            for old, new in zip(original[kind], world[kind]):
                new['id'] = opaque('row_', old['id'])
        result[cid] = (world, {
            'prometheus': {'synthetic_world': True}, 'loki': {'synthetic_world': True},
            'metrics': definitions, 'log_queries': {name: 'synthetic_partition' for name in _LOGS},
            'log_metadata': {name: {'description': text, 'version': 'anonymous-world-v1'}
                             for name, text in _LOGS.items()}})
    return result


def _source_metadata(world):
    metrics = {row['metric']: {'unit': row['unit'], 'aggregation': row['aggregation'],
        'description': row.get('quantity_description', 'unknown'),
        'observed_object': row.get('observed_object', 'unknown'),
        'source': 'closed-loop-source-controls-v1'} for row in world['metrics']}
    return {'prometheus': {'synthetic_world': True}, 'loki': {'synthetic_world': True},
        'metrics': metrics, 'log_queries': {name: 'synthetic_partition' for name in
                                           ('dependency', 'configuration', 'resource')},
        'log_metadata': {
            'dependency': {'description': 'Outgoing request outcomes and route-specific probes.'},
            'resource': {'description': 'Local worker, queue and resource acquisition events.'},
            'configuration': {'description': 'Local configuration parsing and loading events.'}}}


def _fixture_worlds(root):
    folder = Path(root) / 'fixtures' / 'incident'
    catalog = json.loads((folder / 'catalog.json').read_text(encoding='utf-8'))
    public = {row['case_id']: row for row in catalog if row.get('visibility') == 'public_synthetic'}
    worlds, file_hashes = {}, {}
    for cid in FIXTURE_IDS:
        path = folder / public[cid]['file']
        if path.resolve().parent != folder.resolve() or path.suffix != '.json':
            raise ValueError('Unsafe public observation fixture registration')
        raw = path.read_bytes()
        world = json.loads(raw)
        if world.get('synthetic') is not True or {'gold', 'cause', 'correct_answer'} & world.keys():
            raise ValueError('Observation-only public synthetic world required')
        worlds[cid], file_hashes[cid] = world, hashlib.sha256(raw).hexdigest()
    return worlds, file_hashes


def _controls(case):
    rows = []
    for index, item in enumerate(case.review_claims, 1):
        rows.append({'target_id': f'C{index}', 'kind': 'factual_relation',
            'statement': item['statement'], 'original_text': item['statement'],
            'original_text_sha256': hashlib.sha256(item['statement'].encode()).hexdigest(),
            'level': item['level'], 'selectors': [list(pair) for pair in item['selectors']],
            'expected_accept': item['relation'] == 'supported',
            'reference_relation': item['relation'],
            'reference_kind': 'source_constructed_provisional_not_human_gold'})
    for index, item in enumerate(case.review_actions, 1):
        rows.append({'target_id': f'A{index}', 'kind': 'check_appropriateness',
            'action': item['action'], 'condition': item['condition'], 'original_text': item['action'],
            'original_text_sha256': hashlib.sha256(item['action'].encode()).hexdigest(),
            'selectors': [list(pair) for pair in item['selectors']],
            'expected_accept': item['decision'] == 'appropriate',
            'reference_relation': item['decision'],
            'reference_kind': 'source_constructed_provisional_not_human_gold'})
    return rows


def build_case_specs(root=None, *, source_cases=None):
    """Build plain JSON data once in the parent; workers need no current app import.

    ``source_cases`` permits a caller to inject the already-built observation
    controls. The optional default imports the existing evaluation data lazily;
    summary/audit modules should consume the serialized frozen specs instead.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    if source_cases is None:
        from evals.closed_loop_cases import build_cases
        source_cases = build_cases()
    worlds, file_hashes = _fixture_worlds(root)
    anonymous = _anonymous_worlds(worlds)
    specs = {}
    for cid in FIXTURE_IDS:
        for anonymized in (False, True):
            identity = ('anonymous_' if anonymized else 'fixture_') + cid
            world, metadata = anonymous[cid] if anonymized else (deepcopy(worlds[cid]), {})
            keys = [opaque('row_', value) if anonymized else value for value in FIXTURE_COVERAGE_IDS[cid]]
            specs[identity] = {'case_id': identity, 'source_case_id': cid,
                'world_kind': 'anonymous_fixture' if anonymized else 'original_fixture',
                'matched_world_group': cid, 'stages': ['e2e'], 'world': world,
                'capability_metadata': metadata, 'initial_checks': [], 'review_controls': [],
                'key_observation_ids': keys, 'key_metric_kind': 'prespecified_observation_coverage',
                'key_reference_meaning': 'Recorded failure and scope/temporal contrast; not a gold root cause.',
                'eligible_for_positive_acquisition_rate': False,
                'eligible_for_observation_coverage_rate': True,
                'source_file_sha256': file_hashes[cid],
                'scenario_origin': 'existing_development_fixture_identifier_perturbation' if anonymized
                                   else 'existing_development_fixture',
                'external_blind_test': False}
    for cid, case in source_cases.items():
        identity, world = 'world_' + cid, deepcopy(case.observations)
        specs[identity] = {'case_id': identity, 'source_case_id': cid, 'world_kind': 'source_world',
            'matched_world_group': cid, 'stages': ['review', 'e2e'] if cid in E2E_SOURCE_IDS else ['review'],
            'world': world, 'capability_metadata': _source_metadata(world),
            'initial_checks': deepcopy(list(case.initial_checks)), 'review_controls': _controls(case),
            'key_observation_ids': list(case.key_observation_ids),
            'key_metric_kind': 'source_key_acquisition',
            'key_reference_meaning': 'Source-constructed observation IDs; not a gold root cause.',
            'eligible_for_positive_acquisition_rate': bool(case.key_observation_ids) and case.acquisition_available,
            'eligible_for_observation_coverage_rate': bool(case.key_observation_ids),
            'scenario_origin': case.scenario_origin, 'external_blind_test': False}
    for spec in specs.values():
        known = {row['id'] for kind in ('logs', 'metrics', 'changes') for row in spec['world'][kind]}
        if not set(spec['key_observation_ids']) <= known:
            raise ValueError('Prespecified observation ID is missing from its world')
    return specs


def freeze_case_spec(spec):
    reference = {key: spec[key] for key in ('initial_checks', 'review_controls', 'key_observation_ids',
        'key_metric_kind', 'eligible_for_positive_acquisition_rate', 'eligible_for_observation_coverage_rate')}
    return {key: deepcopy(spec[key]) for key in ('case_id', 'source_case_id', 'world_kind',
        'matched_world_group', 'stages', 'key_observation_ids', 'key_metric_kind',
        'eligible_for_positive_acquisition_rate', 'eligible_for_observation_coverage_rate')} | {
        'world_sha256': fingerprint(spec['world']),
        'metadata_sha256': fingerprint(spec['capability_metadata']),
        'reference_sha256': fingerprint(reference),
        'reference_kind': 'source_constructed_provisional_not_human_gold',
        'human_verified': False, 'external_blind_test': False}


def audit_rubric():
    """Freeze before live execution; original final statements are never rewritten."""
    return {'version': RUBRIC_VERSION, 'human_verified': False, 'is_gold': False,
        'support_judgments': {
            'fully_entailed': 'Every assertion is supported within the supplied acquired-source scope.',
            'partially_entailed': 'Some assertions are supported; name the specific unsupported remainder.',
            'contradicted': 'Name a specific assertion and a source observation positively conflicting with it. Missing support or possible alternatives are not counterevidence.',
            'insufficient': 'The acquired sources cannot determine the assertion; permit explicit unknown.',
            'abstain': 'Auditor cannot assess the item; retain an explicit reason rather than count it correct.'},
        'precedence': 'For mixed true and positively contradicted subclaims, use contradicted and record supported subclaims separately. Binary acceptance still requires the whole original statement.',
        'fixed_controls': 'Compare whole-original-target binary acceptance. Legacy supported/full and current supported or appropriate are native approvals; preserve all native verdicts and scope. Wrong four-class labels are an appendix, not a common legacy metric.',
        'natural_outputs': 'Audit all final observations, hypotheses, published and pending recommendations, including failed/partial runs. No final claim means no_claims, not semantic correctness. Do not transfer labels from original fixed claims to newly generated wording.',
        'source_scope': 'Use only actually acquired observations, cited fields, uncited acquired sources, and query quality. A synthetic ticket is a question, not proof. Do not infer from an unacquired hidden record or a prior known case answer.',
        'mechanism': 'Separate symptom/error restatement, observed failure stage, bounded local mechanism, trigger, and unique root cause. level=mechanism and status=completed are not semantic evidence. Missing root cause is legitimate when observations do not expose it.',
        'key_use': 'Record acquisition by frozen observation ID, final hypothesis reference use by evidence ID, and semantic use in the exact current claim separately. Program-derived source facts and aggregate used_evidence_ids do not prove use in a causal claim.',
        'manual_check': 'Judge relevance, scope, authorization condition and feasibility. Missing future check results do not justify rejection; appropriate advice does not mean the checked hypothesis is established.',
        'scope_pitfalls': ['A health probe does not establish whole-component health or business-path latency.',
            'No captured response or unavailable source does not prove no response, remote crash, health or cause.',
            'A later change cannot trigger an earlier recorded failure, but an earlier change alone does not prove causality.',
            'Correlation, pool size and traffic alone do not establish a sole configuration cause.',
            'An AUTH_REJECTED code does not identify a specific audience mismatch without evidence of that mechanism.',
            'Request HTTP 502 outcome does not identify which component emitted the response.'],
        'engineering': 'Keep syntax/shape/reference/policy model failures, recovered repairs, source errors and budget stops separate. Own-version completion is descriptive; never reproject legacy reports through the current completion classifier.',
        'blind_protocol': 'Before semantic audit remove arm, model, version, paths, run IDs, native target prefixes and native approval/verdict/completion labels. Keep exact statement text, cited field values, full acquired-source scope and query quality. Randomize report/target/source IDs. First commit AI decisions and input hashes, then join to original native stance. Source/style inference remains possible; report the practical blinding limitation.',
        'coverage': 'Every scheduled report and item must have reviewed, abstained, report_unavailable or no_claims coverage. Unassessed outcomes never silently enter the correct numerator.',
        'interpretation': 'Source-based AI judgments and authored controls are provisional, not human gold or production diagnostic accuracy. Original and anonymized cases share worlds; repeated targets are correlated.'}


def blind_audit_schema():
    """A data contract for a blind analytical projection, never an engine result."""
    return {'version': 'native-version-blind-audit-packet-v1',
        'packet_fields': ['opaque_report_id', 'ticket_question', 'acquired_sources', 'query_quality', 'items'],
        'item_fields': ['opaque_item_id', 'item_kind', 'original_text', 'condition', 'support_refs', 'counter_refs'],
        'forbidden_identity_fields': ['arm', 'model', 'engine', 'version', 'source_version', 'report_path',
            'run_id', 'trace_id', 'native_target_id', 'status', 'review_verdict', 'published_stance'],
        'judgment_fields': ['opaque_report_id', 'opaque_item_id', 'audit_status', 'source_support',
            'supported_subclaims', 'uncovered_assertion', 'source_ids', 'claim_nature',
            'local_mechanism_use', 'unknown_boundary_preserved', 'reason'],
        'audit_status_values': ['reviewed', 'abstained', 'report_unavailable', 'no_claims'],
        'source_support_values': ['fully_entailed', 'partially_entailed', 'contradicted', 'insufficient', 'abstain'],
        'claim_nature_values': ['observation', 'symptom_or_error_restatement', 'observed_failure_stage',
            'temporal_exclusion', 'bounded_local_mechanism', 'trigger', 'unique_root_cause',
            'manual_check', 'unassessed'],
        'joining': 'Keep the identity map outside blind packets. After frozen judgments join opaque IDs to case_id/arm/repeat/report_sha256/native_id/statement_sha256/native_stance; preserve original native reports unchanged.',
        'human_verified': False, 'is_gold': False}
