"""Generic offline wiring double. Its judgments never count as semantic scores."""
import json
from langchain_core.messages import AIMessage


class ScriptedModel:
    json_output = True

    def __init__(self, provider):
        self.provider = provider
        self.reads = 0

    def invoke(self, messages):
        request = json.loads(messages[-1].content)
        stage, payload = request.get('stage'), request.get('input', request)
        if stage == 'investigate':
            capabilities = payload.get('capabilities', {})
            metrics = list(capabilities.get('registered_metrics', {}))
            categories = capabilities.get('registered_log_categories', [])
            if self.reads == 0 and metrics:
                value = {'kind': 'query', 'question': '读取当前登记指标的实际样本',
                         'tool': 'get_service_metrics', 'args': {'metrics': metrics[:2]}}
            elif self.reads <= 1 and categories:
                value = {'kind': 'query', 'question': '读取登记来源记录',
                         'tool': 'get_service_logs', 'args': {'category': categories[0]}}
            else:
                value = {'kind': 'finish', 'question': '免费接线控制结束，判断不代表真实模型能力。'}
            self.reads += 1
        elif stage == 'draft':
            options, sources = payload.get('reference_options', []), payload.get('evidence', [])
            choice = next((r for r in options if r['field_path'] == 'message'), None)
            choice = choice or next((r for r in options if r['field_path'] == 'value'), None)
            if choice:
                source = next(s for s in sources if s['evidence_id'] == choice['evidence_id'])
                source_value = source['payload'].get(choice['field_path'])
                value = {'claims': [{'statement': '当前来源记录：' + str(source_value)[:220],
                                    'refs': [choice['reference_id']], 'level': 'mechanism'}],
                         'next_steps': [{'action': '人工核对报告现象与实际业务请求结果，记录能够支持或收窄判断的样本。',
                                         'condition': '保持工单范围和既有只读权限', 'refs': [choice['reference_id']]}],
                         'gaps': ['脚本控制的判断只验证接线，不代表模型语义质量。']}
            else:
                value = {'claims': [], 'next_steps': [], 'gaps': ['没有取得可引用观测。']}
        else:
            targets, options = payload.get('targets', []), payload.get('reference_options', [])
            items = []
            for target in targets:
                refs = target.get('refs', [])
                option = next((o for o in options if any(o['evidence_id'] == r['evidence_id'] and o['field_path'] == r['field_path'] for r in refs)), None)
                verdict = ({'decision': 'appropriate' if option else 'insufficient'} if target['id'].startswith('A')
                           else {'relation': 'supported' if option else 'insufficient'})
                items.append({'target_id': target['id'], **verdict,
                              'refs': [option['reference_id']] if option else [],
                              'issue': '脚本接线控制；这不是语义准确率证据。'})
            value = {'items': items}
        return AIMessage(content=json.dumps(value, ensure_ascii=False), response_metadata={'finish_reason': 'stop'})


def scripted_model(provider):
    return ScriptedModel(provider)
