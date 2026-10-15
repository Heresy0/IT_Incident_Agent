"""Draft-bound editing; unchanged evidence never inherits an earlier approval."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from typing import Mapping, Sequence

from agents.contracts import DiagnosisDraft, RevisionRequest, DiagnosisSelection
from evidence.diagnosis import ProtocolError


def basis_id(evidence, executors):
    snapshot = {
        'evidence': {key: value.model_dump(mode='json') for key, value in evidence.items()},
        'queries': {role: [{'key': list(key) if isinstance(key, tuple) else key, 'result': row}
                           for key, row in sorted(executor.check_results.items(), key=lambda item: str(item[0]))]
                    for role, executor in executors.items()},
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class RevisionTask:
    base_draft_revision: int
    basis_id: str
    draft: DiagnosisDraft
    requests: tuple[RevisionRequest, ...]

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
        return schema


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
