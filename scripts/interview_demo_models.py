"""Authored responses for two interview demonstrations, not autonomous models.

These actors consume observed tool results through the real workflow. They never
read fixtures or reference answers directly. Plans and verdicts are deliberately
scripted for a reproducible explanation of role boundaries.
"""
import json
from datetime import datetime, timedelta
from langchain_core.messages import AIMessage, ToolMessage
from agents.scripted import scripted_models, selector, latest_metrics


def reply(value):
    return AIMessage(content=json.dumps(value, ensure_ascii=False))


def source(evidence, code):
    return next(e for e in evidence if e['payload'].get('error_code') == code)


class InterviewLeaf:
    def __init__(self, role):
        self.role = role

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        data = json.loads(messages[1].content)
        objective = data['objective']
        pending = [row for row in objective['check_completion']['checks'] if row['status'] == 'not_executed']
        if pending:
            return AIMessage(content='', tool_calls=[{'name': row['tool'], 'args': row['args'],
                'id': f'interview-{n}', 'type': 'tool_call'} for n, row in enumerate(pending)])
        evidence = {e['evidence_id']: e for e in objective.get('evidence', [])}
        for message in messages:
            if isinstance(message, ToolMessage):
                evidence.update({e['evidence_id']: e for e in json.loads(message.content)['evidence']})
        facts = []
        for item in evidence.values():
            allowed = item['kind'] == 'observation' if self.role == 'investigation' else item['kind'] != 'observation'
            field = next((key for key in ('value', 'message', 'summary', 'resolution', 'text')
                          if key in item['payload']), None)
            if allowed and field:
                facts.append({'statement': '合成来源观测', 'refs': [selector(item, field)]})
        return reply({'findings': facts[:8], 'tentative_hypotheses': [], 'missing_information': []})


class InterviewDecision:
    def __init__(self, role, scenario):
        self.role, self.scenario = role, scenario

    def invoke(self, messages):
        payload = json.loads(messages[1].content)['input']
        operation = {'supervisor': self.schedule, 'diagnosis': self.diagnose, 'reviewer': self.review}[self.role]
        return reply(operation(payload))

    def schedule(self, data):
        window = {k: data['ticket']['scope'][k] for k in ('start', 'end')}
        def check(tool, **args):
            return {'tool': tool, 'args': {**window, **args}}
        def task(goal, checks, role='investigation', need_ids=None):
            return {'role': role, 'goal': goal, 'checks': checks,
                'need_ids': need_ids or ['N1', 'N2', 'N3'],
                'expected_value': '对照异常响应、适用范围与候选机制；查询完成本身不证明因果。'}
        if not data['tasks']:
            if self.scenario == 'history':
                tasks = [task('核查当前远端拒绝与编码器版本', [check('get_service_logs', category='dependency'),
                         check('get_service_logs', category='configuration')]),
                         task('检索历史恢复条件和版本适用手册', [
                             {'tool': 'search_incidents', 'args': {'symptoms': data['ticket']['symptoms'][:200]}},
                             {'tool': 'search_runbooks', 'args': {'query': data['ticket']['symptoms'][:200]}}],
                              role='knowledge', need_ids=['N3'])]
            else:
                tasks = [task('比较绿色健康探针与实际导出写入请求', [
                    check('get_service_metrics', metrics=['health_probe_success', 'export_error_rate']),
                    check('get_service_logs', category='dependency')])]
            return {'action': 'dispatch', 'reason': '按演示预设计划分派范围内的取证任务', 'tasks': tasks}
        initial_count = 2 if self.scenario == 'history' else 1
        if len(data['tasks']) == initial_count:
            recent = {**window, 'start': (datetime.fromisoformat(window['end']) - timedelta(minutes=15)).isoformat()}
            tasks = ([task('核查扩容适用条件及编码配置变更', [
                {'tool': 'get_service_metrics', 'args': {**recent, 'metrics': ['cpu_utilization', 'workers_active']}},
                check('get_recent_changes', category='configuration')])] if self.scenario == 'history'
                else [task('核查当前身份及旧实例磁盘错误的适用范围', [
                    check('get_service_logs', category='configuration'),
                    check('get_service_logs', category='resource')])])
            return {'action': 'dispatch', 'reason': '补足有区分价值的当前对照', 'tasks': tasks}
        ids = [e['evidence_id'] for e in data['evidence'] if e['kind'] == 'observation'][-8:]
        return {'action': 'diagnose', 'reason': '预设演示取证已完成，交由独立节点组织诊断及复核',
            'need_updates': [{'need_id': n['need_id'], 'status': 'supported', 'evidence_ids': ids,
                'reason': '控制演示的预设评估：当前观测可区分本案例的机制与替代解释。'}
                for n in data['evidence_needs']]}

    def diagnose(self, data):
        evidence = data['evidence']
        if self.scenario == 'history':
            rejection = source(evidence, 'REMOTE_SCHEMA_REJECTED')
            config = source(evidence, 'ENCODER_SELECTED')
            metrics = latest_metrics(evidence)
            history = next(e for e in evidence if e['kind'] == 'past_incident')
            change = next(e for e in evidence if 'summary' in e['payload'] and e['kind'] == 'observation')
            cause = '本次编码器 v2 与接收端 v1 协议不兼容，远端拒绝使推送无法完成'
            support = [selector(rejection, 'message'), selector(config, 'message'), selector(change, 'summary')]
            alternative = {'cause': '历史 CPU 饱和机制同样解释本次远端字段拒绝', 'level': 'alternative',
                'support_refs': [selector(history, 'resolution')],
                'counter_refs': [selector(metrics['cpu_utilization'], 'value'), selector(rejection, 'message')],
                'evidence_explanation': '历史扩容的前提是CPU饱和且协议正确；当前CPU低、远端明确拒绝字段，不支持直接套用。'}
            facts = [support, [selector(metrics['cpu_utilization'], 'value'), selector(metrics['workers_active'], 'value')]]
            action = {'action': '人工核对双方协议契约，确认条件后经审批恢复兼容编码配置',
                'condition': '确认编码器及接收端版本、兼容配置和变更窗口；登记演示不执行变更',
                'expected_result': '实际推送不再收到字段拒绝，错误率和积压下降',
                'risk': '配置变更可能影响已使用新协议的客户端；需备份及回退方案'}
        else:
            denial = source(evidence, 'STORAGE_PERMISSION_DENIED')
            health = source(evidence, 'PROBE_OK')
            config = source(evidence, 'IDENTITY_SELECTED')
            old = source(evidence, 'DISK_FULL')
            cause = '当前导出使用的身份缺少存储写权限，写入被403拒绝；绿色探针未覆盖写入'
            support = [selector(denial, 'message'), selector(config, 'message')]
            alternative = {'cause': '旧 runner 的磁盘满导致当前导出写入失败', 'level': 'alternative',
                'support_refs': [selector(old, 'message')], 'counter_refs': [selector(old, 'message')],
                'evidence_explanation': '该日志明确来自已退出当前路由的旧runner，不能外推到当前403请求。'}
            facts = [support, [selector(health, 'message')], [selector(old, 'message')]]
            action = {'action': '确认预期导出身份及最小写权限，经审批修正身份或授权配置',
                'condition': '由负责人核实目标bucket与预期写权限，选择最小权限方案并完成审批',
                'expected_result': '实际导出写入成功且403下降；不能只用健康探针验证恢复',
                'risk': '扩大授权会增加访问范围；禁止未经审批授予全局权限'}
        action.update(kind='manual_change', requires_approval=True)
        return {'findings': [{'statement': '按原始来源核对', 'refs': refs} for refs in facts],
            'hypotheses': [{'cause': cause, 'level': 'mechanism', 'support_refs': support,
                'evidence_explanation': '控制演示按本次响应与配置观测支持近端机制；不确认唯一深层触发原因。'}, alternative],
            'recommended_actions': [action], 'missing_information': ['操作条件与实际恢复结果仍需人工确认，合成演示未执行修复。']}

    def review(self, data):
        ids = [e['evidence_id'] for e in data['evidence'] if e['kind'] == 'observation'][-8:]
        return {'assessments': [{'target_id': key, 'verdict': 'not_supported' if key == 'H2' else 'supported',
            'evidence_ids': ids, 'reason': ('预设复核：替代解释的历史前提或来源范围不适用于当前请求。'
                if key == 'H2' else '预设复核：核对当前来源字段和有条件建议；支持不代表批准或执行。')}
            for key in data['required_target_ids']],
            'need_assessments': [{'need_id': n['need_id'], 'verdict': 'supported', 'evidence_ids': ids,
                'reason': '控制演示预设问题评估；不代表真实模型准确率。'} for n in data['evidence_needs']],
            'follow_up_reason': '本演示核心问题已形成预设复核结果，实际变更条件留给人工确认。'}


def interview_models(scenario):
    if scenario == 'rework':
        return scripted_models()  # Existing history-led initial mistake + actual scoped rework.
    if scenario not in {'history', 'scope'}:
        raise ValueError('Unknown interview demonstration')
    return {role: InterviewLeaf(role) if role in {'investigation', 'knowledge'} else InterviewDecision(role, scenario)
            for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}
