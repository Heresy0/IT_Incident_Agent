"""Generic protocol smoke, deliberately no case diagnosis or semantic score."""
import json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from agents.scripted import selector


class SmokeModel:
    def __init__(self, role, metric):
        self.role, self.metric = role, metric

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        if self.role in ('single', 'investigation'):
            results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
            if not results:
                scope = data['ticket']['scope']
                args = {k: scope[k] for k in ('start', 'end')}
                args['metrics'] = [self.metric]
                return AIMessage(content='', tool_calls=[{'name': 'get_service_metrics', 'args': args,
                    'id': 'smoke-observation', 'type': 'tool_call'}])
            evidence = [e for result in results for e in result['evidence']]
        else:
            payload = data['input']
            evidence = payload['evidence']
            if self.role == 'supervisor':
                if payload['tasks'] and not evidence:
                    return AIMessage(content=json.dumps({'action': 'request_info',
                        'reason': '免费协议检查未取得观测，保留缺口',
                        'missing_information': ['观测渠道未返回可用数据；脚本不判断原因']}, ensure_ascii=False))
                value = ({'action': 'dispatch', 'reason': '免费协议检查，读取一个登记指标',
                          'tasks': [{'role': 'investigation', 'goal': '免费协议检查：读取一个指标，不判断业务原因'}]}
                         if not payload['tasks'] else {'action': 'diagnose', 'reason': '免费协议检查：转交已有观测'})
                return AIMessage(content=json.dumps(value, ensure_ascii=False))
            if self.role == 'reviewer':
                value = {'assessments': [{'target_id': tid, 'verdict': 'uncertain',
                    'reason': '脚本不判断语义，保留缺口', 'evidence_ids': []} for tid in payload['targets']],
                    'follow_up_reason': '免费脚本控制，不发起自主补查'}
                return AIMessage(content=json.dumps(value, ensure_ascii=False))
        facts = [{'statement': '免费协议检查取得的来源观测', 'refs': [selector(e, 'value')]}
                 for e in evidence if 'value' in e['payload']][:4]
        value = {'findings': facts, 'missing_information': ['脚本控制，只验证数据与协议，不判断故障原因']}
        if self.role == 'investigation':
            value['tentative_hypotheses'] = []
        else:
            value.update(hypotheses=[], recommended_actions=[])
        return AIMessage(content=json.dumps(value, ensure_ascii=False))


def smoke_factory(provider):
    metric = provider._data['metrics'][0]['metric']
    return lambda flow: SmokeModel('single', metric) if flow == 'single' else {
        role: SmokeModel(role, metric) for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}
