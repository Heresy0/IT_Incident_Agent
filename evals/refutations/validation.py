"""Supplementary synthetic services; never a blind or external quality benchmark.

Authored after the bounded guards. Criteria are only for post-run review and
are never included in snapshots, packs, or model inputs.
"""
from workflow.diagnostic_pack import DiagnosticPack

VALIDATION_IDS = ('queue_polling', 'read_only_volume', 'certificate_window')


def validation_cases():
    definitions = [
        ('queue_polling', 'batch-label-worker', '队列长轮询卡住，健康探测正常，希望定位批任务无法执行的原因。',
         'dependency', 'QUEUE_FETCH_TIMEOUT',
         '10:10 worker-7对任务队列的长轮询等待30秒超时，尚未取得任务，未进入业务处理器；同一远端的健康探测成功，耗时8毫秒，该探测不覆盖长轮询请求。',
         [('request_error_rate', 0.35, 'ratio'), ('queue_backlog', 680, 'count'), ('worker_cpu', 22, 'percent')],
         '10:04更新追踪字段；队列地址和长轮询超时配置保持不变。'),
        ('read_only_volume', 'attachment-indexer', '索引文件写入失败，进程存活且远端探测正常，是否可以认定服务正常？',
         'resource', 'READ_ONLY_FILESYSTEM',
         '10:10任务idx-42写入挂载卷/index返回EROFS；同次挂载状态为只读，可用字节5368709120；进程存活。远端探测成功不覆盖本地文件写入。',
         [('request_error_rate', 0.62, 'ratio'), ('disk_usage', 0.42, 'ratio'), ('process_cpu', 18, 'percent')],
         '10:05仅修改日志保留时长；未记录挂载状态为何改变。'),
        ('certificate_window', 'event-ingester', '调用开始失败，刚更新过日志组件，希望判断是否回滚以及是否是业务接口拒绝。',
         'dependency', 'PEER_CERT_NOT_YET_VALID',
         '10:10请求evt-19的TLS握手校验失败：对端证书notBefore为10:20，校验时刻为10:10；连接未进入HTTP请求阶段。当前记录不证明证书部署历史。',
         [('request_error_rate', 0.78, 'ratio'), ('dependency_error_rate', 0.78, 'ratio'), ('process_cpu', 21, 'percent')],
         '10:06更新日志组件，依赖地址、信任链和证书校验选项未改变。'),
    ]
    cases = []
    for cid, service, symptoms, category, code, message, metrics, change in definitions:
        common = {'service': service, 'environment': 'staging', 'data_version': 'bounded-refutation-validation-v1',
                  'visibility': 'public_synthetic', 'timestamp': '2026-10-13T10:10:00+00:00'}
        scope = {'service': service, 'environment': 'staging', 'service_version': '2.0',
                 'start': '2026-10-13T09:50:00+00:00', 'end': '2026-10-13T10:25:00+00:00', 'allowed_dependencies': []}
        snapshot = {'synthetic': True, 'ticket': {'incident_id': cid, 'title': cid, 'symptoms': symptoms, 'scope': scope},
            'metrics': [{**common, 'id': name, 'metric': name, 'value': value, 'unit': unit,
                         'aggregation': 'sample', 'granularity': '1m'} for name, value, unit in metrics],
            'logs': [{**common, 'id': 'business-request', 'category': category, 'level': 'ERROR',
                      'error_code': code, 'message': message}],
            'changes': [{**common, 'id': 'recent-change', 'category': 'deployment', 'version': '2.0',
                         'summary': change, 'config_summary': {}}],
            'owners': [], 'runbooks': [], 'incidents': []}
        pack = DiagnosticPack.model_validate({'pack_id': 'supplementary-'+cid, 'service': service,
            'environment': 'staging', 'service_version': '2.0', 'checks': [
                {'check_id': 'business-failure', 'tool': 'get_service_logs', 'args': {'category': category},
                 'human_check': '核对同一窗口的业务请求日志和记录覆盖范围。'},
                {'check_id': 'symptom-controls', 'tool': 'get_service_metrics', 'args': {'metrics': [m[0] for m in metrics]},
                 'human_check': '核对当前指标样本、时间和对象范围，保留未覆盖信息。'}]})
        cases.append({'case_id': cid, 'snapshot': snapshot, 'pack': pack})
    return cases


def quality_criteria():
    return {
        'queue_polling': ['不得用探活成功或低处理器使用率排除长轮询故障。', '保留长轮询取任务失败这一机制；远端内部原因未知。',
                          '核查建议不得断言远端内部故障已经成立。'],
        'read_only_volume': ['不得把进程存活或远端正常扩大为本地写入正常。', '只读挂载导致写入拒绝可成立，挂载变化来源未知。',
                             '可用空间和失败类型只约束当前写入，不能证明所有资源健康；修改挂载须有条件与审批。'],
        'certificate_window': ['同次请求未进入HTTP阶段，可否定该请求发生HTTP业务拒绝的具体命题。',
                               '保留证书有效期与校验时刻不匹配的机制；部署历史未知。', '不得因日志组件发布时间接近就建议直接回滚。'],
    }
