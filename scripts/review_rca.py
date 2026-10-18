"""Export two-step human RCA panels and strictly bind imported judgments.

Candidates remain candidates until real humans attest provenance and compare
the exact output with acquired evidence. This utility never calls a model.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import secrets
import hashlib
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.rca_scoring import (EVIDENCE_FIELDS, ROOT_FIELDS, IDENTIFIABILITY,
    canonical_sha, sha_file, read_json, write_json, require, human_header)

ATTESTATION = 'I personally reviewed this material; this is my human judgment.'


def load_inputs(bundle_path, dataset_path=None):
    from evals.rca_contracts import load_dataset, DEFAULT_MANIFEST
    bundle_path = Path(bundle_path).resolve()
    bundle = read_json(bundle_path)
    dataset = load_dataset(dataset_path or DEFAULT_MANIFEST)
    cases = {row['case_id']: row for row in dataset['cases']}
    reports, identities = [], set()
    for entry in bundle['runs']:
        target = (bundle_path.parent / entry['report_file']).resolve()
        require(target.is_relative_to(bundle_path.parent), 'REPORT_PATH_ESCAPED_BUNDLE')
        require(target.is_file() and sha_file(target) == entry['report_sha256'], 'REPORT_HASH_MISMATCH')
        report = read_json(target)
        for name in ('report_id', 'case_id', 'repeat', 'system_id', 'stage', 'control_id'):
            require(report.get(name) == entry.get(name), 'REPORT_IDENTITY_MISMATCH:' + name)
        require(entry['report_id'] not in identities, 'DUPLICATE_REPORT_ID')
        identities.add(entry['report_id'])
        case = cases[entry['case_id']]
        frozen = report['world_freeze']
        require(frozen.get('public_sha256') == case['public_sha256']
            and frozen.get('truth_sha256') == case['truth_sha256']
            and frozen.get('diagnostics_sha256') == case['diagnostics_sha256'], 'CASE_OR_TRUTH_HASH_MISMATCH')
        require(report.get('dataset_fingerprint') == dataset['fingerprint'], 'DATASET_FINGERPRINT_MISMATCH')
        require(bundle['plan'].get('case_freezes', {}).get(entry['case_id']) == frozen, 'PLAN_CASE_FREEZE_MISMATCH')
        for field in ('application', 'model_settings', 'provider_configuration_sha256'):
            require(report.get(field) == bundle['plan'].get(field), 'REPORT_PLAN_MISMATCH:' + field)
        require(report.get('execution_mode') == bundle.get('execution_mode'), 'REPORT_EXECUTION_MODE_MISMATCH')
        if not report.get('harness_failure'):
            require(report.get('native_arm') == bundle['plan'].get('native_arm') == 'current', 'NATIVE_ARM_MISMATCH')
            require(report.get('observed_world_sha256') == canonical_sha(case['public']['world']), 'OBSERVED_WORLD_MISMATCH')
            require(report.get('observed_capability_sha256') == canonical_sha(case['public']['capability_metadata']), 'OBSERVED_CAPABILITY_MISMATCH')
        for call in report.get('model_call_log', []):
            expected_model = bundle['plan']['model'] if report.get('execution_mode') != 'scripted_control_only' else 'scripted-wiring'
            require(call.get('model') == expected_model, 'ACTUAL_MODEL_MISMATCH')
            if call.get('response_model_name'):
                require(call['response_model_name'] == expected_model, 'RESPONSE_MODEL_MISMATCH')
        reports.append({'entry': entry, 'report': report, 'report_path': str(target)})
    return bundle, dataset, reports


def load_bundles(bundle_paths, dataset_path=None):
    bundles, reports, dataset, sources = [], [], None, []
    conditions, run_keys = set(), set()
    reference_schedule = None
    for path in bundle_paths:
        bundle, current, rows = load_inputs(path, dataset_path)
        if dataset is not None:
            require(current['fingerprint'] == dataset['fingerprint'], 'PAIRED_DATASET_MISMATCH')
            for field in ('model', 'model_settings', 'limits', 'jobs', 'concurrency_policy',
                          'provider_configuration_sha256', 'native_arm', 'runtime_budget_adapter'):
                require(bundle['plan'].get(field) == bundles[0]['plan'].get(field), 'PAIRED_PLAN_MISMATCH:' + field)
        dataset = current
        schedule = {(r['case_id'], r['stage'], r['repeat'], r.get('control_id')) for r in bundle['plan'].get('schedule', [])}
        require(len(schedule) == len(bundle['plan'].get('schedule', [])), 'DUPLICATE_PLAN_SCHEDULE')
        if reference_schedule is not None:
            require(schedule == reference_schedule, 'PAIRED_CASE_STAGE_REPEAT_MISMATCH')
        reference_schedule = schedule
        systems = {r['entry']['system_id'] for r in rows} | {r['system_id'] for r in bundle.get('scheduled_pending', [])}
        if not systems and bundle['plan'].get('system_id'):
            systems.add(bundle['plan']['system_id'])
        require(len(systems) == 1 and not systems.intersection(conditions), 'DISTINCT_SYSTEM_ID_REQUIRED_PER_BUNDLE')
        conditions.update(systems)
        for row in rows:
            entry = row['entry']
            key = (entry['system_id'], entry['case_id'], entry['stage'], entry['repeat'], entry.get('control_id'))
            require(key not in run_keys, 'DUPLICATE_SYSTEM_CASE_RUN')
            require(key[1:] in schedule, 'UNSCHEDULED_REPORT')
            run_keys.add(key)
        bundles.append(bundle)
        reports.extend(rows)
        sources.append({'path': str(Path(path).resolve()), 'sha256': sha_file(path)})
    require(bool(bundles), 'NO_BUNDLES')
    comparability = {'status': 'paired_inputs_verified' if len(bundles) > 1 else 'single_system_baseline',
        'systems': sorted(conditions), 'same_world_model_limits_settings_schedule': True,
        'application_versions': [b['plan'].get('application') for b in bundles],
        'application_and_prompt_changes_must_be_interpreted_together': True,
        'inference': 'descriptive_case_paired_no_significance_or_production_claim'}
    return bundles, dataset, reports, sources, comparability


def bundle_binding(sources):
    return sources[0]['sha256'] if len(sources) == 1 else canonical_sha([s['sha256'] for s in sources])


def _header(panel, packet_hash):
    return {'schema_version': 'rca-human-review-v1', 'panel': panel,
        'packet_sha256': packet_hash, 'reviewer_type': None, 'reviewer_id': None,
        'reviewed_at': None, 'attestation': None, 'ai_generated_gold': False,
        'review_role': 'independent', 'adjudication_of': [],
        'instructions': 'Pending template. Only the person who performed the review may complete human fields.',
        'rows': []}


def _strings(value, path):
    if isinstance(value, str) and value:
        return [{'path': path, 'text': value}]
    if isinstance(value, list):
        return [item for i, child in enumerate(value) for item in _strings(child, f'{path}[{i}]')]
    return []


def visible_items(report):
    """All final normalized items, plus additional user-visible prose omitted by legacy audits."""
    result = []
    normalized = report.get('normalized', {})
    if report.get('harness_failure'):
        require(not normalized.get('audit_items'), 'HARNESS_FAILURE_HAS_UNBOUND_OUTPUT')
        return []
    require(isinstance(normalized.get('audit_items'), list), 'AUDIT_ITEMS_REQUIRED')
    for index, item in enumerate(normalized['audit_items']):
        if item.get('kind') == 'finding':
            continue
        fields = {key: deepcopy(item.get(key)) for key in ('text', 'condition', 'expected_result', 'level') if key in item}
        require(isinstance(fields.get('text'), str), 'ITEM_TEXT_MISSING')
        result.append({'native_path': f'normalized.audit_items[{index}]', 'kind': item.get('kind'),
            'visible_fields': fields, 'refs': item.get('refs', []), 'counter_refs': item.get('counter_refs', [])})
    native = report.get('native_report', {})
    output = native.get('output') or {}
    for group, names in (('hypotheses', ('evidence_explanation', 'pending_checks')),
                         ('recommended_actions', ('condition', 'expected_result', 'risk')), ('findings', ('explanation',))):
        for index, item in enumerate(output.get(group, [])):
            for name in names:
                for part in _strings(item.get(name), f'native_report.output.{group}[{index}].{name}'):
                    result.append({'native_path': part['path'], 'kind': 'visible_prose',
                        'visible_fields': {'text': part['text']}, 'refs': [], 'counter_refs': []})
    for index, item in enumerate(native.get('pending_actions', [])):
        for name in ('action', 'condition', 'expected_result', 'risk', 'review_reason'):
            for part in _strings(item.get(name), f'native_report.pending_actions[{index}].{name}'):
                result.append({'native_path': part['path'], 'kind': 'pending_prose',
                    'visible_fields': {'text': part['text']}, 'refs': [], 'counter_refs': []})
    prose = _strings(output.get('missing_information', []), 'native_report.output.missing_information')
    drafts = native.get('drafts', [])
    if not prose and drafts:
        prose = _strings(drafts[-1].get('draft', {}).get('gaps', []), 'native_report.drafts[-1].draft.gaps')
    for part in prose:
        result.append({'native_path': part['path'], 'kind': 'gap',
            'visible_fields': {'text': part['text']}, 'refs': [], 'counter_refs': []})
    return result


def build_review_materials(reports, cases, *, bundle_sha256, opaque_factory=None):
    opaque = opaque_factory or (lambda prefix: prefix + secrets.token_hex(12))
    packets, links, roots = [], [], []
    evidence_template = _header('evidence', None)
    root_template = _header('root_match', None)
    root_template['evidence_review_sha256'] = None
    for record in reports:
        report, entry = record['report'], record['entry']
        case = cases[entry['case_id']]
        rid, sources, source_map = opaque('R'), [], {}
        for source in report.get('normalized', {}).get('observations', []):
            eid = source.get('evidence_id')
            require(eid is not None, 'EVIDENCE_ID_REQUIRED')
            if eid in source_map:
                continue
            sid = source_map[eid] = opaque('S')
            sources.append({'source_id': sid, 'payload': deepcopy(source.get('payload', {})),
                'source_fields': {k: deepcopy(v) for k, v in source.get('source_fields', {}).items()
                    if k in {'service', 'observed_at', 'data_version', 'allowed_field_paths', 'truncated', 'status'}}})
        def refs(rows):
            projected = []
            for row in rows:
                require(isinstance(row, dict), 'UNRESOLVED_REFERENCE')
                projected.append({**{k: deepcopy(row[k]) for k in ('field_path', 'value', 'unit', 'observed_at',
                    'data_version', 'quote') if k in row}, 'source_id': source_map.get(row.get('evidence_id')),
                    'unresolved': row.get('evidence_id') not in source_map})
            return projected
        items, private_items = [], []
        for item in visible_items(report):
            iid = opaque('I')
            public = {'item_id': iid, 'kind': item['kind'], 'visible_fields': item['visible_fields'],
                'support_refs': refs(item['refs']), 'counter_refs': refs(item['counter_refs'])}
            items.append(public)
            private_items.append({'item_id': iid, 'native_path': item['native_path'],
                'item_sha256': canonical_sha(public), 'text_sha256': canonical_sha(item['visible_fields'])})
        question = {k: deepcopy(v) for k, v in case['public']['world']['ticket'].items()
                    if k in {'title', 'description', 'symptoms', 'scope'}}
        facts = [{'visible_fields': {'text': item.get('text', '')}, 'support_refs': refs(item.get('refs', []))}
            for item in report.get('normalized', {}).get('audit_items', []) if item.get('kind') == 'finding']
        query_quality = []
        for query in report.get('normalized', {}).get('queries', []):
            query_quality.append({**{k: deepcopy(query[k]) for k in ('tool', 'name', 'args', 'query_profile',
                'status', 'error', 'truncated', 'completed_range', 'observed_from', 'observed_to', 'requested_range') if k in query},
                'source_ids': [source_map[eid] for eid in query.get('evidence_ids', []) if eid in source_map]})
        packet = {'report_id': rid, 'question': question, 'acquired_sources': sources, 'items': items,
            'query_quality': query_quality,
            'read_only_source_facts': facts, 'source_facts_are_not_generated_hypotheses': True}
        packets.append(packet)
        bound = {'report_id': rid, 'packet_report_sha256': canonical_sha(packet),
            'report_sha256': entry['report_sha256'], 'source_sha256': canonical_sha(sources),
            'query_quality_sha256': canonical_sha(query_quality),
            'world_sha256': case['public_sha256'], 'truth_sha256': case['truth_sha256']}
        links.append({**bound, 'identity': {k: entry.get(k) for k in ('report_id', 'case_id', 'split', 'repeat',
            'system_id', 'stage', 'control_id')}, 'items': private_items})
        # No native verdict, status, arm, repeat, case ID, family or reference
        # answer is included in the evidence panel. Hashes live only in linkage.
        evidence_template['rows'].append({'report_id': rid, 'packet_report_sha256': bound['packet_report_sha256'],
            'source_sha256': bound['source_sha256'], 'status': 'pending', 'approved': False, 'reason': None,
            'query_quality_sha256': bound['query_quality_sha256'],
            'items': [{'item_id': i['item_id'], 'item_sha256': canonical_sha(i), 'source_support': None,
                'source_ids': [], 'reason': None} for i in items],
            **{name: None for name in EVIDENCE_FIELDS}})
        root = {'report_id': rid, 'evidence_panel_report_sha256': bound['packet_report_sha256'],
            'truth_sha256': case['truth_sha256'], 'candidate_truth': deepcopy(case['truth']),
            'truth_content_sha256': canonical_sha(case['truth']),
            'user_visible_assertion_context': [{k: deepcopy(row[k]) for k in ('cause', 'level', 'status') if k in row}
                for row in report.get('native_report', {}).get('output', {}).get('hypotheses', [])],
            'user_visible_status_is_output_under_test_not_gold': True,
            'items': deepcopy(items), 'formal_scoring_blocked_until_human_provenance_confirmation': True}
        roots.append(root)
        root_template['rows'].append({'report_id': rid, 'root_packet_sha256': canonical_sha(root),
            'truth_sha256': case['truth_sha256'], 'status': 'pending', 'approved': False, 'reason': None,
            'established_item_ids': [],
            'items': [{'item_id': i['item_id'], 'item_sha256': canonical_sha(i), 'reason': None} for i in items],
            **{name: None for name in ROOT_FIELDS}})
    evidence = {'schema_version': 'rca-evidence-panel-v1', 'panel': 'evidence', 'packets': packets,
        'instructions': 'First judge only this output and sources. Do not consult root-match panel or native approvals.'}
    root_packet = {'schema_version': 'rca-root-panel-v1', 'panel': 'root_match', 'packets': roots,
        'instructions': 'Second step: compare with private candidate truth, after saving evidence review. Human truth confirmation is separately required.'}
    evidence_template['packet_sha256'] = canonical_sha(evidence)
    root_template['packet_sha256'] = canonical_sha(root_packet)
    truth_packets = {'schema_version': 'rca-truth-panel-v1', 'cases': [
        {'case_id': row['case_id'], 'truth_sha256': row['truth_sha256'], 'public_sha256': row['public_sha256'],
         'truth_content_sha256': canonical_sha(row['truth']),
         'public': deepcopy(row['public']), 'candidate_truth': deepcopy(row['truth'])} for row in cases.values()]}
    truth_template = _header('truth', canonical_sha(truth_packets))
    truth_template['rows'] = [{'case_id': row['case_id'], 'truth_sha256': row['truth_sha256'],
        'public_sha256': row['public_sha256'], 'status': 'pending', 'approved': False, 'reason': None,
        'origin_confirmed': None, 'provenance_confirmed': None, 'identifiability_confirmed': None,
        'identifiability': None, 'scope_confirmed': None, 'causal_confirmation': None,
        'reproduction_artifacts': [], 'limitations': None,
        'fixed_controls': [{'control_id': c['control_id'], 'control_sha256': canonical_sha(c),
            'candidate_relation': c['expected_relation'], 'status': 'pending', 'human_relation': None, 'reason': None}
            for c in row['truth'].get('fixed_controls', [])]} for row in cases.values()]
    linkage = {'schema_version': 'rca-linkage-private-v1', 'bundle_sha256': bundle_sha256,
        'evidence_packet_sha256': canonical_sha(evidence), 'root_packet_sha256': canonical_sha(root_packet),
        'truth_packet_sha256': canonical_sha(truth_packets), 'reports': links,
        'do_not_provide_to_evidence_reviewer': True}
    return {'evidence-packets.json': evidence, 'root-match-packets.private.json': root_packet,
        'truth-packets.private.json': truth_packets, 'linkage.private.json': linkage,
        'evidence-review.template.json': evidence_template, 'root-review.template.json': root_template,
        'truth-review.template.json': truth_template}


def verify_report_bindings(materials, reports, cases, *, bundle_sha256):
    """Rebuild all projections from the source bundle with saved opaque IDs."""
    require(materials['linkage.private.json']['bundle_sha256'] == bundle_sha256, 'REVIEW_BUNDLE_BINDING_MISMATCH')
    public = materials['evidence-packets.json']['packets']
    require(len(public) == len(reports), 'REVIEW_REPORT_COUNT_MISMATCH')
    identifiers = []
    for packet in public:
        identifiers.append(('R', packet['report_id']))
        identifiers.extend(('S', source['source_id']) for source in packet['acquired_sources'])
        identifiers.extend(('I', item['item_id']) for item in packet['items'])
    iterator = iter(identifiers)
    def opaque(prefix):
        try:
            expected, value = next(iterator)
        except StopIteration as exc:
            raise ValueError('REVIEW_OPAQUE_ID_SET_CHANGED') from exc
        require(expected == prefix, 'REVIEW_OPAQUE_ID_ORDER_CHANGED')
        return value
    rebuilt = build_review_materials(reports, cases, bundle_sha256=bundle_sha256, opaque_factory=opaque)
    require(all(canonical_sha(rebuilt[name]) == canonical_sha(materials[name]) for name in rebuilt),
            'REVIEW_SOURCE_OUTPUT_OR_REFERENCE_CHANGED')
    return True


def _validate_rows(document, template, *, artifact_root=None):
    human_header(document)
    require(document.get('panel') == template['panel'] and document.get('packet_sha256') == template['packet_sha256'], 'REVIEW_PACKET_MISMATCH')
    key = 'case_id' if template['panel'] == 'truth' else 'report_id'
    expected = {row[key]: row for row in template['rows']}
    rows = {row[key]: row for row in document.get('rows', [])}
    require(len(rows) == len(document.get('rows', [])) and set(rows) == set(expected), 'INCOMPLETE_OR_DUPLICATE_REVIEW_ROWS')
    for identity, row in rows.items():
        original = expected[identity]
        for field in ('packet_report_sha256', 'root_packet_sha256', 'source_sha256', 'query_quality_sha256', 'truth_sha256', 'public_sha256'):
            if field in original:
                require(row.get(field) == original[field], 'REVIEW_BINDING_CHANGED:' + field)
        require(row.get('status') in {'pending', 'reviewed', 'abstained'}, 'UNKNOWN_REVIEW_STATUS')
        if template['panel'] == 'truth':
            controls = row.get('fixed_controls', [])
            original_controls = {c['control_id']: c for c in original['fixed_controls']}
            require(len(controls) == len(original_controls) and {c['control_id'] for c in controls} == set(original_controls), 'TRUTH_CONTROL_COVERAGE_MISMATCH')
            for control in controls:
                old = original_controls[control['control_id']]
                require(control.get('control_sha256') == old['control_sha256']
                    and control.get('candidate_relation') == old['candidate_relation'], 'TRUTH_CONTROL_BINDING_CHANGED')
                require(control.get('status') in {'pending', 'reviewed', 'abstained'}, 'UNKNOWN_CONTROL_REVIEW_STATUS')
                if control['status'] == 'reviewed':
                    require(control.get('human_relation') in {'supported', 'partial', 'contradicted', 'insufficient'}, 'HUMAN_CONTROL_RELATION_REQUIRED')
                    require(isinstance(control.get('reason'), str) and control['reason'].strip(), 'HUMAN_CONTROL_REASON_REQUIRED')
        if 'items' in original:
            original_items = {i['item_id']: i for i in original['items']}
            items = {i['item_id']: i for i in row.get('items', [])}
            require(len(items) == len(row.get('items', [])) and set(items) == set(original_items), 'INCOMPLETE_ITEM_COVERAGE')
            for iid, item in items.items():
                require(item.get('item_sha256') == original_items[iid]['item_sha256'], 'ITEM_HASH_CHANGED')
                if row['status'] == 'reviewed':
                    require(isinstance(item.get('reason'), str) and item['reason'].strip(), 'ITEM_REASON_REQUIRED')
                    if template['panel'] == 'evidence':
                        require(item.get('source_support') in {'fully_entailed', 'partially_entailed', 'contradicted', 'insufficient', 'not_causal'}, 'INVALID_SOURCE_SUPPORT')
        if row['status'] != 'reviewed':
            require(row.get('approved') is False, 'PENDING_CANNOT_BE_APPROVED')
            continue
        require(row.get('approved') is True, 'REVIEW_COMPLETION_REQUIRED')
        require(isinstance(row.get('reason'), str) and row['reason'].strip(), 'HUMAN_REASON_REQUIRED')
        fields = EVIDENCE_FIELDS if template['panel'] == 'evidence' else ROOT_FIELDS if template['panel'] == 'root_match' else ()
        require(all(type(row.get(name)) is bool for name in fields), 'INCOMPLETE_CASE_JUDGMENT')
        if template['panel'] == 'truth':
            require(all(row.get(name) is True for name in ('origin_confirmed', 'provenance_confirmed', 'identifiability_confirmed', 'scope_confirmed')), 'TRUTH_PROVENANCE_NOT_CONFIRMED')
            require(row.get('identifiability') in IDENTIFIABILITY, 'INVALID_IDENTIFIABILITY')
            confirmations = {'reproduced_injection', 'confirmed_incident'} if row['identifiability'] == 'root_identifiable' else {'observable_boundary_confirmed'}
            require(row.get('causal_confirmation') in confirmations, 'CAUSAL_OR_BOUNDARY_CONFIRMATION_REQUIRED')
            require(isinstance(row.get('limitations'), str) and row['limitations'].strip(), 'TRUTH_LIMITATIONS_REQUIRED')
            artifacts = row.get('reproduction_artifacts', [])
            require(bool(artifacts), 'CONFIRMATION_ARTIFACT_REQUIRED')
            for artifact in artifacts:
                require(isinstance(artifact.get('description'), str) and artifact['description'].strip(), 'ARTIFACT_DESCRIPTION_REQUIRED')
                path = (Path(artifact_root or '.') / artifact.get('path', '')).resolve()
                require(path.is_file() and sha_file(path) == artifact.get('sha256'), 'CONFIRMATION_ARTIFACT_HASH_MISMATCH')
    return rows


def import_reviews(materials, documents, *, allow_single_reviewer=False, artifact_root=None):
    """All formal rows need human consensus. Disputes stay pending, never majority-voted away."""
    for panel, name in (('evidence', 'evidence-packets.json'), ('root_match', 'root-match-packets.private.json'), ('truth', 'truth-packets.private.json')):
        require(canonical_sha(materials[name]) == materials['linkage.private.json'][
            {'evidence': 'evidence_packet_sha256', 'root_match': 'root_packet_sha256', 'truth': 'truth_packet_sha256'}[panel]], 'PANEL_HASH_MISMATCH')
    templates = {p: materials[n] for p, n in [('evidence', 'evidence-review.template.json'),
        ('root_match', 'root-review.template.json'), ('truth', 'truth-review.template.json')]}
    public = materials['evidence-packets.json']['packets']
    roots = materials['root-match-packets.private.json']['packets']
    truth = materials['truth-packets.private.json']['cases']
    linkrows = materials['linkage.private.json']['reports']
    require(len({r['report_id'] for r in public}) == len(public), 'DUPLICATE_PUBLIC_PACKET')
    require({r['report_id'] for r in public} == {r['report_id'] for r in roots}
        == {r['report_id'] for r in linkrows}, 'PANEL_REPORT_SET_MISMATCH')
    for panel, rows, idfield in (('evidence', public, 'report_id'), ('root_match', roots, 'report_id'), ('truth', truth, 'case_id')):
        template = templates[panel]
        require(template['packet_sha256'] == canonical_sha(materials[
            {'evidence': 'evidence-packets.json', 'root_match': 'root-match-packets.private.json', 'truth': 'truth-packets.private.json'}[panel]]), 'TEMPLATE_PACKET_MISMATCH')
        require(len(template['rows']) == len(rows) and {r[idfield] for r in template['rows']} == {r[idfield] for r in rows}, 'TEMPLATE_ROW_SET_MISMATCH')
        index = {r[idfield]: r for r in rows}
        for row in template['rows']:
            packet = index[row[idfield]]
            if panel == 'truth':
                require(row['truth_sha256'] == packet['truth_sha256'] and row['public_sha256'] == packet['public_sha256']
                    and canonical_sha(packet['candidate_truth']) == packet['truth_content_sha256'], 'TRUTH_PACKET_BINDING_CHANGED')
                expected_controls = {c['control_id']: (canonical_sha(c), c['expected_relation'])
                    for c in packet['candidate_truth'].get('fixed_controls', [])}
                require(len(row['fixed_controls']) == len(expected_controls)
                    and {c['control_id']: (c['control_sha256'], c['candidate_relation']) for c in row['fixed_controls']} == expected_controls,
                    'TRUTH_TEMPLATE_CONTROL_BINDING_CHANGED')
            else:
                field = 'packet_report_sha256' if panel == 'evidence' else 'root_packet_sha256'
                require(row[field] == canonical_sha(packet), 'TEMPLATE_REPORT_BINDING_CHANGED')
                require(len(row['items']) == len(packet['items']) and {i['item_id']: i['item_sha256'] for i in row['items']}
                    == {i['item_id']: canonical_sha(i) for i in packet['items']}, 'TEMPLATE_ITEM_BINDING_CHANGED')
                if panel == 'evidence':
                    require(row['source_sha256'] == canonical_sha(packet['acquired_sources']), 'TEMPLATE_SOURCE_BINDING_CHANGED')
                    require(row['query_quality_sha256'] == canonical_sha(packet['query_quality']), 'TEMPLATE_QUERY_QUALITY_BINDING_CHANGED')
                else:
                    require(row['truth_sha256'] == packet['truth_sha256'] and canonical_sha(packet['candidate_truth']) == packet['truth_content_sha256'], 'ROOT_TRUTH_BINDING_CHANGED')
    validated, seen = [], set()
    evidence_docs = {canonical_sha(doc): doc for doc in documents if doc.get('panel') == 'evidence'}
    for doc in documents:
        panel = doc.get('panel')
        require(panel in templates, 'UNKNOWN_PANEL')
        key = (panel, doc.get('reviewer_id'))
        require(key not in seen, 'DUPLICATE_REVIEWER_PANEL')
        seen.add(key)
        rows = _validate_rows(doc, templates[panel], artifact_root=artifact_root)
        require(doc.get('review_role', 'independent') in {'independent', 'adjudicator'}, 'UNKNOWN_HUMAN_REVIEW_ROLE')
        if doc.get('review_role') == 'adjudicator':
            cited = doc.get('adjudication_of')
            require(isinstance(cited, list) and len(cited) >= 2 and len(set(cited)) == len(cited), 'ADJUDICATION_REQUIRES_EXACT_PRIOR_DOCUMENTS')
            prior = {canonical_sha(d): d for d in documents if d.get('panel') == panel
                and d.get('review_role', 'independent') == 'independent'}
            require(set(cited) <= set(prior) and all(prior[h].get('reviewer_id') != doc['reviewer_id'] for h in cited), 'ADJUDICATOR_MUST_BE_INDEPENDENT_OF_PRIOR_REVIEWERS')
        if panel == 'root_match':
            evidence = evidence_docs.get(doc.get('evidence_review_sha256'))
            require(evidence is not None and evidence.get('reviewer_id') == doc['reviewer_id'], 'ROOT_REQUIRES_SAME_REVIEWER_SAVED_EVIDENCE_STEP')
            first = {r['report_id']: r for r in evidence['rows']}
            packets = {r['report_id']: r for r in materials['root-match-packets.private.json']['packets']}
            for rid, row in rows.items():
                itemids = row.get('established_item_ids')
                allowed = {i['item_id'] for i in packets[rid]['items'] if i['kind'] == 'hypothesis'}
                require(isinstance(itemids, list) and all(isinstance(i, str) for i in itemids)
                    and len(itemids) == len(set(itemids)) and set(itemids) <= allowed, 'ESTABLISHED_ITEM_BINDING_INVALID')
                if row['status'] == 'reviewed':
                    require(first[rid]['status'] == 'reviewed', 'ROOT_REQUIRES_COMPLETED_FIRST_STEP')
        if panel == 'evidence':
            sources = {p['report_id']: {s['source_id'] for s in p['acquired_sources']}
                for p in materials['evidence-packets.json']['packets']}
            for rid, row in rows.items():
                require(all(isinstance(i.get('source_ids'), list) and all(isinstance(s, str) for s in i['source_ids'])
                    and len(set(i['source_ids'])) == len(i['source_ids']) and set(i['source_ids']) <= sources[rid]
                    for i in row['items']), 'UNKNOWN_OR_INVALID_REVIEW_SOURCE_ID')
                require(all(i.get('source_support') not in {'fully_entailed', 'partially_entailed', 'contradicted'}
                    or bool(i.get('source_ids')) for i in row['items']), 'SOURCE_SUPPORT_REQUIRES_ACQUIRED_SOURCE')
        validated.append({'panel': panel, 'reviewer_id': doc['reviewer_id'], 'document_sha256': canonical_sha(doc),
            'review_role': doc.get('review_role', 'independent'), 'adjudication_of': doc.get('adjudication_of', []), 'rows': rows})
    minimum = 1 if allow_single_reviewer else 2
    outcomes = {}
    for panel, template in templates.items():
        identity = 'case_id' if panel == 'truth' else 'report_id'
        outcomes[panel] = {}
        for original in template['rows']:
            key = original[identity]
            votes = [(d, d['rows'][key]) for d in validated if d['panel'] == panel
                and d['review_role'] == 'independent' and d['rows'][key]['status'] == 'reviewed']
            fields = EVIDENCE_FIELDS if panel == 'evidence' else ROOT_FIELDS if panel == 'root_match' else ('identifiability',)
            def values(row):
                result = {name: row[name] for name in fields}
                if panel == 'truth':
                    result['fixed_controls'] = {c['control_id']: c['human_relation'] if c['status'] == 'reviewed' else None
                        for c in row.get('fixed_controls', [])}
                if panel == 'root_match':
                    result['established_item_ids'] = row['established_item_ids']
                    # The locked first-step item judgments constrain this later
                    # conclusion. Correctly refuted alternatives are excluded.
                    first_doc = evidence_docs[next(d['evidence_review_sha256'] for d in documents
                        if d.get('panel') == 'root_match' and any(r is row for r in d['rows']))]
                    first_row = next(r for r in first_doc['rows'] if r['report_id'] == row['report_id'])
                    support = {i['item_id']: i['source_support'] for i in first_row['items']}
                    result['established_assertions_supported'] = bool(row['established_item_ids']) and all(
                        support[i] == 'fully_entailed' for i in row['established_item_ids'])
                return result
            signatures = {canonical_sha({**values(row),
                **({'item_support': {i['item_id']: i['source_support'] for i in row['items']}} if panel == 'evidence' else {})}) for _, row in votes}
            state = 'pending' if len(votes) < minimum else 'disputed' if len(signatures) != 1 else 'human_consensus'
            selected = votes[0][1] if state == 'human_consensus' else None
            adjudicators = [(d, d['rows'][key]) for d in validated if d['panel'] == panel
                and d['review_role'] == 'adjudicator' and d['rows'][key]['status'] == 'reviewed']
            if state == 'disputed' and adjudicators:
                require(len(adjudicators) == 1, 'MULTIPLE_ADJUDICATORS_REQUIRE_SEPARATE_EXPLICIT_RESOLUTION')
                adjudicator, selected = adjudicators[0]
                require({d['document_sha256'] for d, _ in votes} <= set(adjudicator['adjudication_of']), 'ADJUDICATION_MUST_CITE_ALL_DISAGREEING_REVIEWS')
                state = 'human_adjudicated'
            outcomes[panel][key] = {'status': state, 'human_reviewers': [d['reviewer_id'] for d, _ in votes],
                'judgment': values(selected) if selected is not None else None,
                'review_document_sha256': [d['document_sha256'] for d, _ in votes],
                'adjudicator': adjudicators[0][0]['reviewer_id'] if state == 'human_adjudicated' else None,
                'adjudication_document_sha256': adjudicators[0][0]['document_sha256'] if state == 'human_adjudicated' else None}
    # Row-level approval cannot re-label every candidate as causally verified.
    # Original private truth remains immutable; this separate review records the
    # humans and exact supporting artifacts, without editing the dataset.
    return {'schema_version': 'rca-human-review-joined-v1', 'linkage_sha256': canonical_sha(materials['linkage.private.json']),
        'evidence_packet_sha256': canonical_sha(materials['evidence-packets.json']),
        'root_packet_sha256': canonical_sha(materials['root-match-packets.private.json']),
        'truth_packet_sha256': canonical_sha(materials['truth-packets.private.json']),
        'review_design': 'single_human_explicitly_disclosed' if allow_single_reviewer else 'two_independent_humans_consensus_required',
        'human_only': True, 'ai_judgments_used_as_gold': False, 'outcomes': outcomes,
        'review_documents': deepcopy(documents), 'formal_ready': all(row['status'] in {'human_consensus', 'human_adjudicated'}
            for panel in outcomes.values() for row in panel.values()) and bool(outcomes['evidence'])}


def load_materials(directory):
    directory = Path(directory)
    names = ('evidence-packets.json', 'root-match-packets.private.json', 'truth-packets.private.json',
        'linkage.private.json', 'evidence-review.template.json', 'root-review.template.json', 'truth-review.template.json')
    return {name: read_json(directory / name) for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--bundle', action='append', required=True)
    export.add_argument('--dataset')
    export.add_argument('--output-dir', required=True)
    ingest = sub.add_parser('import')
    ingest.add_argument('--review-dir', required=True)
    for panel in ('evidence', 'root', 'truth'):
        ingest.add_argument('--' + panel + '-review', action='append', default=[])
    ingest.add_argument('--allow-single-reviewer', action='store_true')
    ingest.add_argument('--artifact-root')
    ingest.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'export':
        _, dataset, reports, sources, _ = load_bundles(args.bundle, args.dataset)
        used = {r['entry']['case_id'] for r in reports} or {r['case_id'] for r in dataset['cases']}
        cases = {c['case_id']: c for c in dataset['cases'] if c['case_id'] in used}
        materials = build_review_materials(reports, cases, bundle_sha256=bundle_binding(sources))
        for name, value in materials.items():
            target = Path(args.output_dir) / name
            require(not target.exists(), 'REVIEW_EXPORT_MUST_NOT_OVERWRITE_EXISTING_BINDINGS')
            write_json(target, value)
        print('Exported pending human panels:', len(reports), 'reports;', len(cases), 'candidate truths. No gold created.')
    else:
        materials = load_materials(args.review_dir)
        paths = args.evidence_review + args.root_review + args.truth_review
        joined = import_reviews(materials, [read_json(p) for p in paths],
            allow_single_reviewer=args.allow_single_reviewer, artifact_root=args.artifact_root)
        write_json(args.output, joined)
        print('Human import formal_ready=' + str(joined['formal_ready']) + '; unresolved rows remain pending/disputed.')


if __name__ == '__main__':
    main()
