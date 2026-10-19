"""Stateless, deterministic responses for recovery tests; never quality gold."""
import json
import os
from langchain_core.messages import AIMessage

from tests.support import TestConfig
from workflow.service import WorkflowService


def audit(path, value):
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())


class RecoveryModel:
    def __init__(self, audit_path=None, repair=False):
        self.audit_path, self.repair = audit_path, repair

    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        stage, payload = request.get('stage'), request.get('input', request)
        audit(self.audit_path, {'kind': 'model', 'stage': stage or 'review', 'request': request})
        if payload.get('challenge_policy_version'):
            value = {'items': [{'target_id': item['id'], 'coverage': 'covered',
                'refs': ['R1'], 'reason': '固定恢复测试响应，不代表语义正确。'} for item in payload['targets']]}
        elif stage == 'investigate':
            collection = payload['collection']
            if payload.get('review_feedback'):
                done = any(row['query_profile'].get('category') == 'resource' for row in collection)
                value = {'kind': 'finish'} if done else {'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': 'resource'}}
            else:
                value = {'kind': 'finish'} if collection else {'kind': 'query', 'tool': 'get_service_logs', 'args': {'category': 'dependency'}}
        elif stage == 'draft':
            value = {'claims': [{'statement': '原范围过宽的命题', 'refs': ['R1']}],
                'next_steps': [{'action': '核对授权范围内请求记录', 'refs': ['R1']}], 'gaps': []}
        elif stage == 'revise':
            value = {'items': [{'target_id': 'C1', 'revision': {'statement': '来源仅记录当前请求失败', 'refs': ['R1']},
                'change_kind': 'narrow', 'reason': '固定修订测试响应'}]}
        elif self.repair and not payload.get('correction'):
            value = {'items': []}
        else:
            value = {'items': [{'target_id': item['id'], 'refs': ['R1'], 'issue': '固定审阅响应，不是准确率金标。',
                **({'decision': 'appropriate'} if item['id'].startswith('A') else
                   {'relation': 'partial' if item.get('statement') == '原范围过宽的命题' else 'supported'})}
                for item in payload['targets']]}
        return AIMessage(content=json.dumps(value, ensure_ascii=False), response_metadata={'finish_reason': 'stop'},
            usage_metadata={'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120})


def service_for(store, audit_path=None, *, repair=False):
    return WorkflowService('unused', store=store,
        config=TestConfig(postgres_dsn=store.dsn, api_key='offline-control-no-network'),
        checkpoint_recovery=True, incident_model_factory=lambda *args: RecoveryModel(audit_path, repair))
