"""Draft-bound editing; unchanged evidence never inherits an earlier approval."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from typing import Mapping, Sequence

from agents.contracts import DiagnosisDraft, RevisionRequest, DiagnosisSelection, Hypothesis
from evidence.diagnosis import ProtocolError, expand_refs


def basis_id(evidence, executors):
    snapshot = {
        'evidence': {key: value.model_dump(mode='json') for key, value in evidence.items()},
        'queries': {role: [{'key': list(key) if isinstance(key, tuple) else key, 'result': row}
                           for key, row in sorted(executor.check_results.items(), key=lambda item: str(item[0]))]
                    for role, executor in executors.items()},
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class RevisionCandidate:
    draft: DiagnosisDraft
    concerns: tuple[dict, ...]


@dataclass(frozen=True)
class RevisionTask:
    base_draft_revision: int
    basis_id: str
    draft: DiagnosisDraft
    requests: tuple[RevisionRequest, ...]

    def project_candidate(self, candidate, concerns, registry):
        """Separate non-target H proposals; never broaden the committed edit scope."""
        requested = {item.target_id for item in self.requests}
        available = {f'H{i + 1}' for i in range(len(self.draft.hypotheses))} - requested
        proposals = {}
        for item in concerns:
            if item.target_id not in available or item.target_id in proposals:
                raise ProtocolError('REVISION_CONCERN_TARGET', [{'target_id': item.target_id, 'allowed': sorted(available)}])
            data = item.candidate.model_dump(exclude={'support_refs', 'counter_refs'})
            hypothesis = Hypothesis(hypothesis_id=item.target_id, **data,
                support_refs=expand_refs(item.candidate.support_refs, registry, 'revision_concerns.support_refs'),
                counter_refs=expand_refs(item.candidate.counter_refs, registry, 'revision_concerns.counter_refs'))
            proposals[item.target_id] = {'target_id': item.target_id, 'reason': item.reason,
                'candidate': hypothesis.model_dump(mode='json', exclude={'status'}), 'origin': 'explicit_proposal'}
        restored = list(candidate.hypotheses)
        if len(restored) == len(self.draft.hypotheses):
            for index, (before, after) in enumerate(zip(self.draft.hypotheses, restored)):
                target = f'H{index + 1}'
                if target in available and before != after:
                    if target in proposals and proposals[target]['candidate'] != after.model_dump(mode='json', exclude={'status'}):
                        raise ProtocolError('REVISION_CONCERN_CONFLICT', [{'target_id': target}])
                    proposals.setdefault(target, {'target_id': target,
                        'reason': '修稿候选同时改动非目标假设；此改动仅作待独立复核提案，不提交。',
                        'candidate': after.model_dump(mode='json', exclude={'status'}), 'origin': 'scope_projection'})
                    restored[index] = before.model_copy(deep=True)
        projected = candidate.model_copy(update={'hypotheses': restored})
        self.validate_candidate(projected)
        return projected, tuple(proposals.values())

    def assert_current(self, revision, evidence, executors):
        if revision != self.base_draft_revision or basis_id(evidence, executors) != self.basis_id:
            raise ProtocolError('STALE_REVISION_TASK')

    def model_input(self, references=None):
        template = selection_template(self.draft, references) if references is not None else None
        instruction = ('以output_template为完整输出起点，仅修改指定H/A；未指定目标原样保留，'
            'F/H/A数量与顺序不变，不新增触发或竞争假设。表达修稿不能改变行动kind/checks/'
            'repair/requires_approval，核查expected_result保留程序固定说明。原信息缺口保留，'
            '新增未知只写入missing_information，不借纯修稿重新调查。不得提升判定或调用工具，'
            '输出仍需完整重新复核；原稿level包括unspecified时无需为格式习惯改写。') if template else (
            '补查后依据更新的全部证据重新形成完整诊断；旧稿仅用于理解修稿要求，不能沿用旧判定。'
            '结合requests修正表达，可重新评估候选和下一步，保留关键未知，结果完整重新复核。')
        return {'base_draft_revision': self.base_draft_revision, 'basis_id': self.basis_id,
                'previous_draft': self.draft.model_dump(mode='json'),
                'output_template': template,
                'scope': 'requested_targets_only' if template else 'new_evidence_draft',
                'editable_target_ids': [item.target_id for item in self.requests],
                'requests': [item.model_dump(mode='json') for item in self.requests],
                'concern_policy': '非目标H发现问题时保留原条目，使用revision_concerns提交target_id/reason/candidate；'
                    '提案不是结论或授权，交由Reviewer独立判定。不得改非目标A、增删F/H/A或改变执行方式。',
                'instruction': instruction}

    def validate_candidate(self, candidate):
        requested = {item.target_id: item.issue_type for item in self.requests}
        details = []
        for field, prefix in (('findings', 'F'), ('hypotheses', 'H'), ('recommended_actions', 'A')):
            original, updated = getattr(self.draft, field), getattr(candidate, field)
            if len(original) != len(updated):
                details.append({'path': field, 'required_count': len(original), 'actual_count': len(updated)})
                continue
            for index, (before, after) in enumerate(zip(original, updated)):
                target = f'{prefix}{index + 1}'
                before_data, after_data = before.model_dump(mode='json'), after.model_dump(mode='json')
                if target in requested:
                    editable = ('action', 'condition', 'expected_result', 'risk') if prefix == 'A' else (
                        'cause', 'evidence_explanation', 'support_refs', 'counter_refs', 'pending_checks')
                    if all(before_data[name] == after_data[name] for name in editable):
                        details.append({'path': f'{field}.{index}', 'target_id': target, 'code': 'REQUESTED_TARGET_UNCHANGED'})
                if target not in requested:
                    if before_data != after_data:
                        details.append({'path': f'{field}.{index}', 'target_id': target, 'code': 'UNREQUESTED_TARGET_CHANGED'})
                elif requested[target] == 'expression':
                    locked = ('kind', 'checks', 'repair', 'requires_approval') if prefix == 'A' else (
                        'level', 'support_refs', 'counter_refs', 'pending_checks')
                    for name in locked:
                        if before_data[name] != after_data[name]:
                            details.append({'path': f'{field}.{index}.{name}', 'target_id': target, 'code': 'EXPRESSION_SCOPE_CHANGED'})
        if not set(self.draft.missing_information) <= set(candidate.missing_information):
            details.append({'path': 'missing_information', 'code': 'EXISTING_LIMITATION_REMOVED'})
        if candidate.escalation_team != self.draft.escalation_team:
            details.append({'path': 'escalation_ref', 'code': 'UNREQUESTED_OWNER_CHANGED'})
        if details:
            raise ProtocolError('REVISION_SCOPE_CHANGED', [{**item,
                'editable_target_ids': list(requested),
                'instruction': '只修改指定目标；从revision_task.output_template恢复其余条目及固定字段，'
                               '不得增删F/H/A或删除已有缺口。指定目标仍须按requests实际改写，'
                               '不能直接返回旧模板；不要求用新假设补全触发原因。'} for item in details])

    def output_schema(self, schema, references):
        """Expose server-enforced edit boundaries in the generation contract."""
        schema = deepcopy(schema)
        template = selection_template(self.draft, references)
        requested = {item.target_id: item.issue_type for item in self.requests}
        for field, prefix in (('findings', 'F'), ('hypotheses', 'H'), ('recommended_actions', 'A')):
            definition = schema['properties'][field]
            item_schema = definition['items']
            entries = []
            for index, original in enumerate(template[field]):
                target = f'{prefix}{index + 1}'
                if target not in requested:
                    entries.append({'const': original})
                    continue
                constraints = [deepcopy(item_schema), {'not': {'const': original}}]
                if requested[target] == 'expression':
                    locked = ('kind', 'checks', 'repair', 'requires_approval') if prefix == 'A' else (
                        'level', 'support_refs', 'counter_refs', 'pending_checks')
                    constraints.append({'properties': {key: {'const': original[key]} for key in locked},
                                        'required': list(locked)})
                entries.append({'allOf': constraints})
            definition.update(minItems=len(entries), maxItems=len(entries), prefixItems=entries, items=False)
        schema['properties']['escalation_ref'] = {'const': template['escalation_ref']}
        if 'RevisionConcern' in schema.get('$defs', {}):
            ids = [f'H{i + 1}' for i in range(len(self.draft.hypotheses)) if f'H{i + 1}' not in requested]
            schema['properties']['revision_concerns']['maxItems'] = len(ids)
            schema['$defs']['RevisionConcern']['properties']['target_id']['enum'] = ids
        return schema


def validate_scope_assessments(review, concerns, evidence):
    expected = {item['target_id'] for item in concerns}
    decisions = review.scope_assessments
    ids = [item.target_id for item in decisions]
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ProtocolError('REVISION_SCOPE_COVERAGE', [{'required_target_ids': sorted(expected), 'actual': ids}])
    verdicts = {item.target_id: item.verdict for item in review.assessments}
    requests = {item.target_id: item for item in review.revision_requests}
    for scope_index, decision in enumerate(decisions):
        if len(set(decision.evidence_ids)) != len(decision.evidence_ids) or any(key not in evidence for key in decision.evidence_ids):
            raise ProtocolError('UNKNOWN_OR_DUPLICATE_EVIDENCE')
        target = decision.target_id
        request = requests.get(target)
        evidence_request = review.request_evidence
        linked_gap = (evidence_request and evidence_request.target_id == target) or any(
            item.verdict == 'uncertain' and item.blocker and target in item.blocker.affected_targets
            for item in review.need_assessments)
        valid = (decision.disposition == 'keep' and verdicts[target] == 'supported' and request is None
                    and any(evidence[key].kind == 'observation' for key in decision.evidence_ids)
                 or decision.disposition == 'revise' and verdicts[target] != 'supported'
                    and request is not None and request.issue_type == 'reasoning'
                    and any(evidence[key].kind == 'observation' for key in decision.evidence_ids)
                 or decision.disposition == 'evidence' and verdicts[target] != 'supported' and linked_gap)
        if not valid:
            index = next((i for i, item in enumerate(review.revision_requests) if item.target_id == target), None)
            raise ProtocolError('REVISION_SCOPE_DECISION', [{'target_id': target, 'disposition': decision.disposition,
                'path': f'revision_requests.{index}.issue_type' if index is not None
                    and decision.disposition == 'revise' and request.issue_type != 'reasoning'
                    else f'scope_assessments.{scope_index}.disposition',
                'actual_issue_type': request.issue_type if request else None, 'actual_target_verdict': verdicts[target],
                'instruction': '逐项独立裁决：keep须原H受当前观测支持且无修稿请求；revise须原H未支持且有reasoning修稿请求；'
                               'evidence须原H未支持并关联真实证据缺口。撤回错误因果归因、改写论证属于reasoning，不是expression。'
                               '独立重评N2：现有证据能说明近端机制时，不能仅因深层触发未知要求补查；'
                               '确有关键观测缺口仍保留uncertain。提案本身不作为证据，不为完成改判。'}])


def scope_review_schema(schema, concerns):
    """Advertise the same cross-field authorization rules enforced at runtime."""
    schema = deepcopy(schema)
    ids = [item['target_id'] for item in concerns]
    schema['properties']['scope_assessments'].update(minItems=len(ids), maxItems=len(ids))
    if not ids:
        return schema
    schema['$defs']['RevisionScopeAssessment']['properties']['target_id']['enum'] = ids
    schema.setdefault('required', []).append('scope_assessments')
    schema['$defs']['RevisionRequest'].setdefault('allOf', []).append({
        'if': {'properties': {'target_id': {'enum': ids}}, 'required': ['target_id']},
        'then': {'properties': {'issue_type': {'const': 'reasoning'}}}})
    for target in ids:
        scope = {'properties': {'target_id': {'const': target}, 'disposition': {'const': 'revise'}},
                 'required': ['target_id', 'disposition']}
        request = {'properties': {'target_id': {'const': target}, 'issue_type': {'const': 'reasoning'}},
                   'required': ['target_id', 'issue_type']}
        schema.setdefault('allOf', []).append({
            'if': {'properties': {'scope_assessments': {'contains': scope}}, 'required': ['scope_assessments']},
            'then': {'properties': {'revision_requests': {'contains': request}}, 'required': ['revision_requests']}})
    return schema


def scope_revision_authorized(review, concerns):
    approved = {item.target_id for item in review.scope_assessments if item.disposition == 'revise'}
    return bool(review.revision_requests) and all(item.issue_type == 'reasoning' and item.target_id in approved
        for item in review.revision_requests) and approved <= {item['target_id'] for item in concerns}


def validate_revision_needs(review, evidence):
    """An unresolved draft can require editing without declaring missing data."""
    requests = {item.target_id for item in review.revision_requests}
    verdicts = {item.target_id: item.verdict for item in review.assessments}
    revision_needs = set()
    for index, assessment in enumerate(review.need_assessments):
        targets = getattr(assessment, 'revision_targets', [])
        if getattr(assessment, 'gap_kind', 'evidence') != 'revision':
            if targets:
                raise ProtocolError('REVISION_NEED_CONFLICT', [{'path': f'need_assessments.{index}.revision_targets'}])
            continue
        query = review.request_evidence
        conflict = query and (query.need_id == assessment.need_id or query.target_id in targets)
        if (assessment.verdict != 'uncertain' or assessment.blocker is not None or conflict
                or not targets or len(targets) != len(set(targets)) or not set(targets) <= requests
                or any(verdicts.get(target) == 'supported' for target in targets)
                or not any(evidence[key].kind == 'observation' for key in assessment.evidence_ids)):
            raise ProtocolError('REVISION_NEED_CONFLICT', [{'path': f'need_assessments.{index}',
                'instruction': 'revision表示已有当前证据、稿中论证待改；保持uncertain，关联未支持目标及修稿请求，'
                               '引用当前观测，不填blocker或相关request_evidence。确需新证据则用evidence并保留真实缺口。'}])
        revision_needs.add(assessment.need_id)
    return revision_needs


def selection_template(draft, references):
    def refs(items):
        result = []
        for item in items:
            key = next((key for key, candidate in references.items() if candidate == item), None)
            if key is None:
                raise ProtocolError('STALE_REVISION_REFERENCE')
            result.append({'reference_id': key})
        return result
    hypotheses = [{**item.model_dump(mode='json', exclude={'hypothesis_id', 'status'}),
                   'support_refs': refs(item.support_refs), 'counter_refs': refs(item.counter_refs)}
                  for item in draft.hypotheses]
    owner = next((key for key, item in references.items()
                  if item.field_path == 'team' and item.value == draft.escalation_team), None)
    if draft.escalation_team and owner is None:
        raise ProtocolError('STALE_REVISION_REFERENCE')
    return DiagnosisSelection.model_validate({
        'findings': [{'statement': item.statement, 'refs': refs(item.refs)} for item in draft.findings],
        'hypotheses': hypotheses,
        'recommended_actions': [item.model_dump(mode='json') for item in draft.recommended_actions],
        'missing_information': list(draft.missing_information),
        'escalation_ref': {'reference_id': owner} if owner else None,
    }).model_dump(mode='json')


def make_revision_task(revision, draft, requests: Sequence[RevisionRequest], evidence, executors):
    return RevisionTask(revision, basis_id(evidence, executors), draft.model_copy(deep=True),
                        tuple(item.model_copy(deep=True) for item in requests))


def validate_revision_requests(review, targets: Mapping):
    requests = getattr(review, 'revision_requests', ())
    verdicts = {item.target_id: item.verdict for item in review.assessments}
    ids = [item.target_id for item in requests]
    details = []
    for index, key in enumerate(ids):
        if key not in targets or verdicts.get(key) == 'supported' or ids.count(key) > 1:
            details.append({'path': f'revision_requests.{index}.target_id', 'actual': key,
                'verdict': verdicts.get(key),
                'allowed_target_ids': [target for target in targets if target.startswith(('H', 'A'))
                                       and verdicts.get(target) != 'supported'],
                'instruction': '修稿目标须存在、不重复，且判定为uncertain或not_supported。'
                    'supported目标不提出修稿；保持独立判定，删除矛盾请求或依据真实问题重新评估，'
                    '不得仅为通过而改变判定。核查expected_result是程序固定的中性说明，不请求改写该字段。'})
    if details:
        raise ProtocolError('INVALID_REVISION_TARGET', details)


@dataclass(frozen=True)
class CollectionExit:
    route: str
    reason: str


def collection_exit(evidence, executors, *, initial_replans, can_replan):
    if any(item.kind == 'observation' for item in evidence.values()):
        return CollectionExit('diagnosis', 'observations_available_not_semantically_confirmed')
    executor = executors['investigation']
    attempted = {row['tool'] for row in executor.check_results.values()}
    available = {item['function']['name'] for item in executor.schemas()} - {'get_service_owner'}
    if initial_replans < 1 and available - attempted and can_replan:
        return CollectionExit('supervisor', 'untried_registered_observation_channel')
    return CollectionExit('handoff', 'no_current_observations')
