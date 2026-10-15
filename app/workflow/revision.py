"""Draft-bound editing; unchanged evidence never inherits an earlier approval."""
from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, Sequence

from agents.contracts import DiagnosisDraft, RevisionRequest
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

    def model_input(self):
        return {'base_draft_revision': self.base_draft_revision, 'basis_id': self.basis_id,
                'previous_draft': self.draft.model_dump(mode='json'),
                'requests': [item.model_dump(mode='json') for item in self.requests],
                'instruction': '仅使用当前证据修正指定表达或逻辑，返回完整新草稿；保留关键未知，'
                               '不删除证据问题以通过门禁，不调用工具。新稿必须重新复核。'}


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
