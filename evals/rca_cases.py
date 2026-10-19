"""Build inspectable, provisional RCA evaluation data without private runtime input.

The thirty new scenarios are authored candidates, not thirty independently
verified incidents. Their observation records and causal references still need
human review and actual reproduction. Old source controls are calibration only.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from evals.rca_contracts import DEFAULT_MANIFEST, SCHEMA_VERSION, sha_file, load_dataset


VERSION = 'rca-authored-candidate-dataset-v1'


def _metadata(world):
    return {'prometheus': {'synthetic_world': True}, 'loki': {'synthetic_world': True},
        'metrics': {r['metric']: {'unit': r['unit'], 'aggregation': r['aggregation'],
            'description': r.get('quantity_description', 'Observed quantity; no causal interpretation.'),
            'observed_object': r.get('observed_object', 'local service'), 'source': VERSION}
            for r in world['metrics']},
        'log_queries': {name: 'synthetic_partition' for name in ('dependency', 'configuration', 'resource')},
        'log_metadata': {
            'dependency': {'description': 'Request outcomes, protocol boundary and route-specific probes.'},
            'configuration': {'description': 'Recorded local configuration loading and validation.'},
            'resource': {'description': 'Local resource, queue and acquisition observations.'}}}


def _world(index, symptom):
    start = datetime(2026, 10, 18, 6, tzinfo=timezone.utc)
    return {'synthetic': True, 'ticket': {'incident_id': f'eval_observation_{index:03}',
        'title': 'Synthetic incident observation exercise', 'symptoms': symptom,
        'scope': {'service': f'component_{index:03}', 'environment': 'staging', 'service_version': 'v7',
            'start': start.isoformat(), 'end': (start + timedelta(minutes=20)).isoformat(),
            'allowed_dependencies': [f'peer_{index:03}']}},
        'metrics': [], 'logs': [], 'changes': [], 'owners': [], 'runbooks': [], 'incidents': []}


def _row(world, identifier, minute, **fields):
    scope = world['ticket']['scope']
    return {'id': identifier,
        'timestamp': (datetime.fromisoformat(scope['start']) + timedelta(minutes=minute)).isoformat(),
        'service': scope['service'], 'environment': scope['environment'],
        'visibility': 'public_synthetic', 'data_version': VERSION, **fields}


def _queries(world, *, categories=None):
    window = {key: world['ticket']['scope'][key] for key in ('start', 'end')}
    categories = categories if categories is not None else sorted({r['category'] for r in world['logs']})
    queries = [{'tool': 'get_service_logs', 'args': {**window, 'category': category}}
               for category in categories]
    if world['metrics']:
        metrics = sorted({r['metric'] for r in world['metrics']})
        # Full reference catalogs have a 12k response cap; a six-metric read can
        # keep only the newest sample. Small sets preserve before/after samples.
        for offset in range(0, len(metrics), 2):
            queries.append({'tool': 'get_service_metrics', 'args': {**window,
                'metrics': metrics[offset:offset + 2], 'granularity': '1m'}})
    if world['changes']:
        queries.append({'tool': 'get_recent_changes', 'args': dict(window)})
    return queries


def _observed_ids(world, query):
    """Expected fixture reachability, including provider caps and row ordering."""
    tool, args = query['tool'], query['args']
    if tool in world.get('source_errors', {}):
        return [], world['source_errors'][tool]
    if tool == 'get_service_logs':
        rows = [r for r in world['logs'] if r['category'] == args['category']]
        rows = [r for r in rows if args['start'] <= r['timestamp'] <= args['end']]
        rows = sorted(rows, key=lambda r: (r['timestamp'], r['id']))[:50]
    elif tool == 'get_service_metrics':
        rows = [r for r in world['metrics'] if r['metric'] in args['metrics']
                and args['start'] <= r['timestamp'] <= args['end']]
    else:
        rows = [r for r in world['changes'] if args['start'] <= r['timestamp'] <= args['end']]
        rows = sorted(rows, key=lambda r: (r['timestamp'], r['id']))[:10]
    return [r['id'] for r in rows], None


def _package(case_id, world, *, split, family, group, identifiability,
             conditions, explanation, essential_ids, unknowns, source_case=None,
             controls=None, query_override=None, notes=None):
    public = {'schema_version': SCHEMA_VERSION, 'world': deepcopy(world),
              'capability_metadata': _metadata(world)}
    queries = query_override or _queries(world)
    oracle = []
    for query in queries:
        ids, error = _observed_ids(world, query)
        oracle.append({**deepcopy(query), 'expected_observation_ids': ids, 'expected_error': error})
    if controls is None:
        first = next(r for r in world['logs'] if r['id'] in essential_ids)
        controls = [
            {'control_id': 'control_01', 'statement': '记录中明确写道：' + first['message'][:380],
             'level': 'mechanism', 'selectors': [[first['id'], 'message']], 'expected_relation': 'supported'},
            {'control_id': 'control_02', 'statement': '现有记录已证明远端数据库进程崩溃是所有失败请求的唯一根因。',
             'level': 'trigger', 'selectors': [[first['id'], 'message']], 'expected_relation': 'insufficient'}]
    diagnostics = {'schema_version': SCHEMA_VERSION, 'source_queries': deepcopy(queries),
        'fixed_controls': [{key: deepcopy(value) for key, value in item.items() if key != 'expected_relation'}
                           for item in controls]}
    truth = {'schema_version': SCHEMA_VERSION, 'case_id': case_id, 'split': split,
        'independence_group': group, 'origin': 'authored_synthetic_provisional',
        'truth_status': 'pending_human_review', 'holdout_sealed': False,
        'identifiability': identifiability,
        'scope': {'service': world['ticket']['scope']['service'],
            'time_window': {k: world['ticket']['scope'][k] for k in ('start', 'end')},
            'causal_level': identifiability,
            'target_boundary': 'Local request failure within the declared observation window; no organizational why.'},
        'causal_conditions': [{'condition_id': f'condition_{n}', 'statement': text,
            'kind': 'candidate_causal_condition' if identifiability == 'root_identifiable' else 'observed_mechanism'}
            for n, text in enumerate(conditions, 1)],
        'causal_edges': [{'from': f'condition_{n}', 'to': 'observed_failure', 'mechanism': explanation}
                        for n in range(1, len(conditions) + 1)],
        'reference_explanation': explanation,
        'evidence_criteria': [{'criterion_id': 'criterion_01', 'proposition': explanation,
            'acceptable_observation_sets': [list(essential_ids)], 'required': True,
            'equivalent_fact_paths_allowed': True}],
        'boundaries': {'must_preserve_unknown': list(unknowns),
            'forbidden_assertions': ['A successful health probe proves every business route healthy.',
                'Missing/unavailable source proves the remote process crashed.',
                'Temporal correlation alone proves a unique root cause.']},
        'oracle_queries': oracle, 'sufficient_evidence_queries': deepcopy(oracle),
        'fixed_controls': controls,
        'provenance': {'human_verified': False, 'is_gold': False,
            'human_review_status': 'pending', 'causal_confirmation_status': 'not_confirmed',
            'reproduction_evidence': [], 'reviewers': [], 'source_case_id': source_case,
            'source_origin': 'existing_development_source_control' if source_case else 'new_authored_candidate',
            'reference_basis': 'Author-written synthetic records and proposed interpretation, not executed fault injection.',
            'required_next_step': 'Independent human review and documented controlled reproduction before sealing or gold use.',
            'notes': list(notes or [])}}
    return {'case_id': case_id, 'split': split, 'scenario_family': family,
        'independence_group': group, 'public': public, 'public_diagnostics': diagnostics, 'truth': truth}


# Each tuple is a distinct authored mechanism, not a renamed service instance.
# Tuple: family, symptom, conditions, explanation, (category, message) records.
_ROOT_SCENARIOS = [
 ('connection_wait', 'Requests time out waiting for pooled connections.',
  ['连接池上限在负载不变时从40缩至4。'], '容量收缩使连接获取排队并超过请求期限。', [
   ('configuration', '06:03 applied pool_max=4; previous pool_max=40; traffic stayed 30 requests/s.'),
   ('resource', 'request=q11 acquisition timeout: in_use=4 pool_max=4 waiters=28; SQL hold time stayed 20ms.'),
   ('resource', 'Synthetic fixture replay record: same 30 requests/s and 20ms SQL, pool_max=40 => no acquisition timeout; pool_max=4 => acquisition timeout.')]),
 ('connection_wait', 'Requests time out waiting for pooled connections.',
  ['业务SQL的锁等待长期占用所有池连接。'], '事务锁等待延长连接持有时间，后续请求无法取得连接。', [
   ('resource', 'request=q12 acquisition timeout: pool_max=20 unchanged; all 20 holders attached to SQL lock wait L7 for 9s.'),
   ('dependency', 'SQL trace: statement UPDATE account waits for lock L7; blocker transaction b41 remains open; ping SELECT 1 finishes 3ms.'),
   ('dependency', 'Synthetic fixture replay record: releasing b41 ends L7 waits; same workload and pool_max=20 then acquire latency falls to 2ms.')]),
 ('worker_wait', 'Requests wait for a worker slot and expire.',
  ['全部工作线程在同步对端调用中阻塞。'], '同步等待占用工作线程，使本地队列超过等待期限。', [
   ('resource', 'Thread snapshot: 16/16 workers blocked in synchronous HTTP client wait; local pending=83; CPU=9%.'),
   ('dependency', 'In-flight calls to /render have not returned for 8s; configured read timeout=30s.'),
   ('resource', 'Synthetic fixture replay record: stub /render response 20ms with same 16 workers and arrival rate => pending queue drains and local expiries stop.')]),
 ('deadline', 'Business calls fail while the remote endpoint returns responses.',
  ['本地读取期限从2000ms缩短为100ms，正常响应耗时为180ms。'], '本地期限早于正常响应到达，客户端提前终止请求。', [
   ('configuration', '06:03 client read timeout_ms loaded as 100; previous value=2000.'),
   ('dependency', 'request=q14 deadline fired at 100ms; correlated remote response completed at 180ms; response payload was otherwise valid.'),
   ('dependency', 'Synthetic fixture replay record: same 180ms response, timeout_ms=2000 => success; timeout_ms=100 => deadline expiry.')]),
 ('authentication', 'A previously successful business route now returns 401.',
  ['调用方令牌aud从pay-api改成report-api，而接收方要求pay-api。'], '令牌受众与该路由校验值不符，触发认证拒绝。', [
   ('configuration', 'Token issuer local audience setting changed from pay-api to report-api at 06:03; no token secret is included.'),
   ('dependency', 'request=q15 receiver diagnostic: received aud=report-api; route expects aud=pay-api; reject=audience_mismatch.'),
   ('dependency', 'Synthetic fixture replay record: token identical except aud=pay-api => accepted; aud=report-api => rejected on same route.')]),
 ('authentication', 'A previously successful business route now returns 401.',
  ['校验节点时钟比可信参考快420秒，令牌因而被判过期。'], '节点时钟偏移使本来有效的令牌超过exp校验边界。', [
   ('resource', 'Clock comparison: validator node UTC is 420s ahead of external reference; sample uncertainty <1s.'),
   ('dependency', 'request=q16 token exp=06:10:00; reference arrival=06:05:00; validator arrival=06:12:00; rejection=token_expired.'),
   ('dependency', 'Synthetic fixture replay record: validate same token at reference time 06:05 => accepted, at node time 06:12 => expired.')]),
 ('name_resolution', 'Business requests fail before connection with name lookup errors.',
  ['本地搜索后缀改为错误域，使短名称被解析成不存在的名称。'], '错误名称展开导致NXDOMAIN，连接尚未开始。', [
   ('configuration', '06:03 resolver search suffix changed svc.internal -> old.invalid; application still uses short name ledger.'),
   ('dependency', 'request=q17 resolved ledger.old.invalid => NXDOMAIN; no TCP connection attempted; ledger.svc.internal exists in authoritative snapshot.'),
   ('dependency', 'Synthetic fixture replay record: same resolver and target, suffix svc.internal => address returned; suffix old.invalid => NXDOMAIN.')]),
 ('name_resolution', 'Business requests fail before connection with name lookup errors.',
  ['本地负缓存保留旧NXDOMAIN直至06:18，虽然权威记录06:03已存在。'], '负缓存命中绕过了更新后的权威记录，业务持续解析失败。', [
   ('resource', 'Cache entry for ledger.internal: NXDOMAIN, inserted 05:58, expires 06:18; current local time=06:05.'),
   ('dependency', 'request=q18 lookup ledger.internal hit negative cache; authoritative snapshot at 06:05 returns 10.0.3.8.'),
   ('dependency', 'Synthetic fixture replay record: bypass negative entry => address and connection succeed; restore entry => lookup returns NXDOMAIN.')]),
 ('transport_handshake', 'TLS requests fail during the connection handshake.',
  ['部署的信任包遗漏目标证书链所需根证书R2。'], '本地证书链校验无法构建到受信根，TLS在HTTP请求前失败。', [
   ('configuration', 'Loaded trust bundle version b7 lists roots R1,R3; previous b6 listed R1,R2,R3.'),
   ('dependency', 'request=q19 TLS chain leaf->intermediate->R2; local validation=unknown_ca; HTTP request was not sent.'),
   ('dependency', 'Synthetic fixture replay record: same chain and host with b6 => validated; b7 => unknown_ca.')]),
 ('transport_handshake', 'TLS requests fail during the connection handshake.',
  ['代理将TLS SNI覆盖为错误主机名称。'], 'SNI选择了不匹配证书，主机名校验在HTTP前失败。', [
   ('configuration', '06:03 proxy override server_name changed ledger.internal -> metrics.internal; application route still ledger.internal.'),
   ('dependency', 'request=q20 SNI=metrics.internal; peer selected certificate SAN=metrics.internal; requested identity ledger.internal; validation=hostname_mismatch.'),
   ('dependency', 'Synthetic fixture replay record: SNI=ledger.internal yields matching SAN and handshake success; SNI=metrics.internal yields mismatch.')]),
 ('local_validation', 'A gateway-facing route returns 502 for submitted jobs.',
  ['客户端schema仍发送已移除的mode值legacy。'], '本地载荷校验拒绝字段值并映射为502，没有访问下游。', [
   ('configuration', 'Schema v7 allowed mode values=batch,stream; v6 also allowed legacy; local validator now loads v7.'),
   ('configuration', 'request=q21 payload mode=legacy; validator=unsupported_enum; emitted HTTP 502; outgoing connection attempted=false.'),
   ('configuration', 'Synthetic fixture replay record: same v7 route with mode=batch => accepted; mode=legacy => rejected before dispatch.')]),
 ('routing', 'Only the /submit business route returns 404 while /ready succeeds.',
  ['当前路由表遗漏/submit POST映射。'], '路由匹配在本地失败，健康探针的独立映射不覆盖该业务路径。', [
   ('configuration', 'Loaded route table r7 contains GET /ready and GET /status; previous r6 additionally contained POST /submit.'),
   ('dependency', 'request=q22 POST /submit local matcher=no_match => HTTP 404; GET /ready => HTTP 200; outgoing request attempted=false.'),
   ('configuration', 'Synthetic fixture replay record: restore only POST /submit mapping => matcher dispatches; remove mapping => local 404.')]),
 ('connection_wait', 'Requests time out waiting for pooled connections.',
  ['重试上限从1升至5放大下游请求。', '池容量为8使放大后的并发请求耗尽连接。'],
  '重试放大与有限连接容量共同造成获取排队；任一单独条件不足解释对照中的故障。', [
   ('configuration', '06:03 retry attempts changed 1 -> 5; pool_max remains 8; original external arrivals=20/s.'),
   ('resource', 'During delayed replies outbound attempts=95/s; in_use=8 waiters=42; external arrivals remain 20/s.'),
   ('resource', 'Synthetic 2x2 fixture replay: retries=1,pool=8 success; retries=5,pool=40 success; retries=5,pool=8 acquire expiry; all use same response delay and arrivals.')]),
 ('memory_pressure', 'Large batch requests terminate when the local process is killed.',
  ['单请求展开后缓冲区增长到约180MiB。', '容器内存上限为128MiB。'],
  '请求展开内存需求超过容器限制触发OOM终止，多因素共同构成故障条件。', [
   ('resource', 'Container limit=128MiB; request=q24 decoded batch allocation attempts=180MiB; kill reason=cgroup_oom; host free memory=8GiB.'),
   ('configuration', 'Batch payload source changed compact encoding to expanded item count=90000; container limit unchanged.'),
   ('resource', 'Synthetic fixture replay: expanded batch + limit128 => kill; expanded batch + limit512 => success; compact batch + limit128 => success.')]),
 ('queue_backpressure', 'Accepted jobs expire before dispatch to the output sink.',
  ['入口关闭了队列背压并继续以100任务每秒接纳。', '下游sink处理能力只有20任务每秒。'],
  '无背压接纳率持续超过消费率，积压超过任务TTL后过期。', [
   ('configuration', '06:03 admission backpressure disabled; accept rate=100 jobs/s; job TTL=5s.'),
   ('resource', 'Sink completion=20 jobs/s; queue age reaches 6s; expiries occur before any sink dispatch.'),
   ('resource', 'Synthetic fixture replay: backpressure enabled + sink20 => admitted<=20 no expiry; disabled + sink100 => no expiry; disabled + sink20 => queue TTL expiry.')]),
 ('lock_deadlock', 'Two write requests stop making progress while CPU stays low.',
  ['请求a与b以相反顺序持有互相需要的锁。'], '互斥锁形成等待环，两个请求均不能推进至释放点。', [
   ('resource', 'Snapshot: request=a holds lock X waits lock Y owned by b; request=b holds Y waits X owned by a; lock owners stable across 10 samples.'),
   ('resource', 'No timeout or cancellation configured for these lock acquisitions; requests perform no outbound calls while waiting.'),
   ('resource', 'Synthetic fixture replay: enforce X-before-Y for both paths => completion; opposing X/Y order at same barrier => wait cycle reproduced.')]),
 ('filesystem', 'Writes fail creating local temporary files.',
  ['临时卷空闲inode为0。'], '文件元数据分配失败，即使字节容量仍有剩余也无法创建新文件。', [
   ('resource', 'request=q27 create /tmp/job-27 returns ENOSPC; filesystem free bytes=6GiB, free inodes=0.'),
   ('resource', 'Existing temp directory contains 400000 zero-byte files; inode quota=400000; file contents consume negligible bytes.'),
   ('resource', 'Synthetic fixture replay: remove 100 unused zero-byte files => inode free=100 and create succeeds; bytes unchanged materially.')]),
 ('descriptor_exhaustion', 'New outbound connections fail in a long-running local process.',
  ['异常路径没有关闭套接字，打开描述符积累到1024上限。'], '本地进程描述符耗尽导致新socket创建失败，尚未连接对端。', [
   ('resource', 'request=q28 socket() returns EMFILE; process fd count=1024; rlimit_nofile=1024; no SYN sent.'),
   ('resource', 'Descriptor trace: failed-response cleanup path leaves one socket open per attempt; same owner stack accounts for 1000 retained sockets.'),
   ('resource', 'Synthetic fixture replay: same workload with cleanup close enabled => fd count stays24 and socket succeeds; close disabled => count rises to limit.')]),
]


_BOUNDARY_SCENARIOS = [
 ('thread_contention', 'mechanism_only', 'Requests wait for a local mutex and miss their deadlines.',
  ['工作线程在本地互斥锁上等待。'], '可确认互斥锁等待环节，持锁线程为何不释放尚不可见。', [
   ('resource', 'request=q31 mutex wait=2100ms ending at local deadline; lock owner id=73; owner stack was not captured.'),
   ('dependency', 'request=q31 never attempted a network call; CPU remains12%.')],
  ['Unknown owner code path, release condition and reason for extended hold.']),
 ('resolver_boundary', 'mechanism_only', 'Some business requests fail before TCP connection.',
  ['本地名称解析调用返回SERVFAIL。'], '可确认请求止于名称解析失败，不能定位解析器内部起因。', [
   ('dependency', 'request=q32 lookup ledger.internal returned SERVFAIL; resolver internal logs unavailable; no TCP connect started.'),
   ('dependency', 'A separate GET /ready to a cached address succeeded; it performed no DNS lookup.')],
  ['Resolver internal root cause and whether every name is affected.']),
 ('remote_auth_boundary', 'mechanism_only', 'One remote business route returns 401.',
  ['对端认证边界返回audience mismatch拒绝。'], '可确认受众校验拒绝，但无法判断调用方还是对端配置偏差。', [
   ('dependency', 'request=q33 remote /authorize returned401 diagnostic=audience_mismatch; received and expected audience values were not exported.'),
   ('configuration', 'Local token contents and remote expected-audience config are outside the available observation set.')],
  ['Which party has the wrong audience and which configuration produced it.']),
 ('cpu_boundary', 'mechanism_only', 'Local processing pauses despite spare host CPU.',
  ['本地任务在cgroup CPU配额节流窗口内暂停。'], '可确认配额节流造成处理停顿，但配额设置的起因与合理性未知。', [
   ('resource', 'request=q34 paused during cgroup throttled interval; throttled time=850ms of1s; host CPU=18%.'),
   ('configuration', 'Current workload CPU quota exists; historical quota, demand baseline and deployment changes are not exposed.')],
  ['Reason for quota and whether quota or excessive demand is the upstream cause.']),
 ('transport_boundary', 'mechanism_only', 'Outbound requests lose their established connection.',
  ['本地连接在读取响应期间收到RST。'], '可确认连接被重置，无法从本地记录确定哪个中间设备或对端发起。', [
   ('dependency', 'request=q35 establishedTCP then received RST while awaiting response; application payload not captured.'),
   ('dependency', 'Traffic traverses proxy and load balancer; their logs and packet origin attribution are unavailable.')],
  ['RST origin and underlying remote/intermediate cause.']),
 ('queue_boundary', 'mechanism_only', 'Some background jobs expire before execution.',
  ['本地队列等待超过任务TTL。'], '可确认队列等待导致过期，缺少生产率和消费率历史无法归因积压。', [
   ('resource', 'job=q36 accepted06:03, first dispatch06:10, TTL=5min; expired locally before execution.'),
   ('resource', 'Queue depth snapshot=300; arrival-rate history, worker availability and service time samples not available.')],
  ['Whether arrival surge, stopped consumers or long task service caused the queue growth.']),
 ('missing_remote', 'not_identifiable', 'Gateway returns generic502 and the needed remote source is unavailable.',
  [], '现有记录只确认502结果，无法区分本地映射、代理故障或远端响应。', [
   ('dependency', 'request=q37 emitted HTTP502; original exception, downstream status and wire trace were not captured.'),
   ('configuration', 'No local change records are configured for this observation source.')],
  ['Failure-emitting component and any remote root cause.']),
 ('aggregate_ambiguity', 'not_identifiable', 'Aggregate request latency rises, but route and phase breakdown are missing.',
  [], '聚合延迟不足区分网络等待、排队和执行阶段，不能确认局部机制或根因。', [
   ('resource', 'All-route aggregate p95=2100ms; no per-route, queue, execution or dependency spans captured.'),
   ('dependency', 'Health probe GET /ready=200 in8ms; probe does not execute business operations.')],
  ['Which route, phase and causal condition account for aggregate latency.']),
 ('late_change', 'not_identifiable', 'Authentication failures preceded a later configuration update.',
  [], '已知配置更新晚于首次失败，可排除此更新触发更早记录，尚不能确定其他起因。', [
   ('dependency', 'First captured request=q39 failed at06:04 with generic AUTH_REJECTED; no token/validator details.'),
   ('configuration', 'Config event at06:08 changed authentication settings; there is no earlier config snapshot.')],
  ['Cause of the06:04 failure; a later change cannot initiate that already recorded event.']),
 ('probe_ambiguity', 'not_identifiable', 'A user reports /submit failures, while only /ready probe telemetry exists.',
  [], '健康探针不覆盖用户路径，无法确认业务失败机制或排除依赖故障。', [
   ('dependency', 'GET /ready returned200; this probe did not call /submit, authenticate a user or read business data.'),
   ('resource', 'Only probe task resource samples available; no business request trace or error event recorded.')],
  ['Whether /submit failed and which component or phase is responsible.']),
 ('content_truncation', 'not_identifiable', 'The only diagnostic message exceeds the provider field limit.',
  [], '可见部分仅有通用失败；被截断的内容不是已取得证据，不能推断隐藏起因。', [
   ('dependency', 'request=q41 failed; diagnostic filler: ' + 'x'*1850 + '; inaccessible suffix: local validator field mismatch.'),
   ('configuration', 'Diagnostic source provides no pagination or shorter projection for the overlong message.')],
  ['Any alleged cause appearing only beyond the actual returned1800-character message.']),
 ('window_boundary', 'not_identifiable', 'The failure occurred before the scoped observation window.',
  [], '授权时间窗口内只有恢复后的记录，无法确认较早故障的机制或起因。', [
   ('dependency', '06:05 routine request succeeds; current capture has no historical failure samples.'),
   ('resource', 'Incident report mentions failure at05:40; available window begins06:00; history outside scope is not authorized.')],
  ['Mechanism and cause of the earlier05:40 failure.']),
]


def build_candidate_cases():
    result = []
    scenarios = [(family, 'root_identifiable', symptom, conditions, explanation, records, [])
                 for family, symptom, conditions, explanation, records in _ROOT_SCENARIOS]
    scenarios.extend(_BOUNDARY_SCENARIOS)
    for index, (family, identifiability, symptom, conditions, explanation, records, unknowns) in enumerate(scenarios, 1):
        world = _world(index, symptom)
        essential = []
        for n, (category, message) in enumerate(records, 1):
            identifier = f'row_{n:02}'
            world['logs'].append(_row(world, identifier, 4 + n, category=category, level='INFO',
                error_code='OBSERVATION', message=message))
            essential.append(identifier)
        if family == 'missing_remote':
            world['source_errors'] = {'get_recent_changes': 'SOURCE_NOT_CONFIGURED'}
        queries = _queries(world)
        if family == 'missing_remote':
            queries.append({'tool': 'get_recent_changes', 'args':
                {key: world['ticket']['scope'][key] for key in ('start', 'end')}})
        notes = ['No actual fault injection or intervention has been executed; replay statements are authored synthetic observations.']
        if family == 'content_truncation':
            notes.append('Diagnostic suffix is intentionally unobservable after current provider field truncation; no positive root claim permitted.')
        result.append(_package(f'rca_candidate_{index:03}', world, split='candidate_acceptance',
            family=family, group='candidate_template_' + family, identifiability=identifiability,
            conditions=conditions, explanation=explanation, essential_ids=essential,
            unknowns=unknowns or ['Organizational origin of the stated local condition remains outside scope.'],
            query_override=queries, notes=notes))
    return result


def build_calibration_cases():
    # Reuse all eight old source controls, with explicit non-holdout provenance.
    from evals.closed_loop_cases import build_cases
    result = []
    for index, (source_id, case) in enumerate(build_cases().items(), 1):
        world = deepcopy(case.observations)
        controls = [{'control_id': f'control_{n:02}', 'statement': item['statement'],
            'level': item['level'], 'selectors': [list(pair) for pair in item['selectors']],
            'expected_relation': item['relation']} for n, item in enumerate(case.review_claims, 1)]
        ids = list(case.key_observation_ids)
        if not ids:
            ids = [row['id'] for row in world['logs']]
        identifiable = source_id == 'followup_available'
        result.append(_package(f'rca_calibration_{index:03}', world, split='calibration',
            family='old_source_' + source_id, group='old_source_' + source_id,
            identifiability='root_identifiable' if identifiable else
                'not_identifiable' if source_id in ('probe_scope', 'temporal_counterevidence') else 'mechanism_only',
            conditions=['本地payload的mode值不符合当前schema。'] if identifiable else [],
            explanation='本地schema校验拒绝载荷并产生502，未建立对外连接。' if identifiable else
                'Existing source-based review/control case; no independently verified root-cause truth.',
            essential_ids=ids, unknowns=['Root cause is not independently established by this calibration material.'],
            source_case=source_id, controls=controls,
            notes=['Already used during development and earlier real-model studies; calibration only, never held-out acceptance.']))
    return result


def validate_reachability(dataset):
    """Run public diagnostic queries through the unmodified real ToolExecutor.

    This verifies the current tool's authorization, argument schema, filtering,
    caps, actual payload truncation and referenced fields. It is not a proof that
    a model will choose the witness, or that the authored causal truth is valid.
    """
    from api.auth import Principal
    from evals.closed_loop_cases import SourceCase, SourceWorldProvider
    from runtime.context import RunContext, Limits
    from tools.executor import ToolExecutor
    principal = Principal('rca_evaluation', 'observation_reader')
    rows = []
    for case in dataset['cases']:
        provider = SourceWorldProvider(SourceCase(case['case_id'], case['public']['world'], ()), principal)
        scope = provider.ticket().scope
        context = RunContext(scope=scope, limits=Limits(tool_calls=8, max_sources=20))
        executor = ToolExecutor(provider, principal, scope, context, role='single')
        observed, errors, truncated_fields = set(), [], []
        for query, oracle in zip(case['public_diagnostics']['source_queries'], case['truth']['oracle_queries'], strict=True):
            result = executor.execute(query['tool'], query['args'])
            actual_ids = {item.locator.rsplit('/', 1)[-1] for item in result.evidence}
            code = result.error.code if result.error else None
            if code != oracle['expected_error'] or actual_ids != set(oracle['expected_observation_ids']):
                raise ValueError('Real tool witness mismatch: ' + case['case_id'] + ':' + query['tool'])
            observed.update(actual_ids)
            if code:
                errors.append(code)
            truncated_fields.extend(item.locator for item in result.evidence
                                    if item.payload.get('content_truncated'))
        for control in case['public_diagnostics']['fixed_controls']:
            for row_id, field in control['selectors']:
                evidence = next((item for item in executor.evidence.values()
                                 if item.locator.endswith('/' + row_id)), None)
                if evidence is None or not any(option.field_path == field for option in evidence.reference_options):
                    raise ValueError('Fixed diagnostic selector not actually readable: ' + case['case_id'])
        for criterion in case['truth']['evidence_criteria']:
            if not any(set(alternative) <= observed for alternative in criterion['acceptable_observation_sets']):
                raise ValueError('Evidence witness does not acquire the declared criterion: ' + case['case_id'])
        rows.append({'case_id': case['case_id'], 'legal_queries': len(case['truth']['oracle_queries']),
            'observed_records': len(observed), 'expected_source_errors': errors,
            'content_truncated_sources': truncated_fields,
            'model_calls': context.counts['model_calls'], 'tool_calls': context.counts['tool_calls']})
    return {'status': 'public_witness_checked_against_current_executor', 'cases': rows,
        'human_gold': False, 'causal_truth_validated': False, 'model_calls': 0}


def write_dataset(folder=None, *, replace_unfrozen_draft=False):
    """Materialize reviewable JSON; never silently overwrite an existing dataset."""
    folder = Path(folder) if folder is not None else DEFAULT_MANIFEST.parent
    if (folder / 'manifest.json').exists() and not replace_unfrozen_draft:
        raise FileExistsError('Dataset exists; deliberate versioning is required')
    for name in ('public', 'private', 'diagnostics'):
        (folder / name).mkdir(parents=True, exist_ok=True)
    entries = []
    for case in build_calibration_cases() + build_candidate_cases():
        entry = {key: case[key] for key in ('case_id', 'split', 'scenario_family', 'independence_group')}
        for label, field in (('public', 'public'), ('truth', 'truth'), ('diagnostics', 'public_diagnostics')):
            subfolder = 'private' if label == 'truth' else label
            relative = subfolder + '/' + case['case_id'] + '.json'
            artifact = folder / relative
            # v1 was frozen with CRLF; reproduction must retain those bytes on every OS.
            artifact.write_text(json.dumps(case[field], ensure_ascii=False, indent=2) + '\n',
                                encoding='utf-8', newline='\r\n')
            entry[label + '_path'], entry[label + '_sha256'] = relative, sha_file(artifact)
        entries.append(entry)
    manifest = {'schema_version': SCHEMA_VERSION, 'dataset_version': VERSION,
        'holdout_sealed': False, 'human_verified': False, 'is_gold': False,
        'origin': 'authored_synthetic_provisional', 'truth_status': 'pending_human_review',
        'formal_acceptance_enabled': False,
        'intended_candidate_composition': {'root_identifiable': 18, 'mechanism_only': 6, 'not_identifiable': 6},
        'calibration_cases': 8, 'candidate_cases': 30,
        'independence_policy': 'Count template groups, not renamed variants or repetitions, as the conservative independent unit.',
        'sealing_requirements': ['Independent human review of scope and identifiability',
            'Documented reproduction or adequate real-incident confirmation',
            'No related calibration/development variant in the acceptance split',
            'Freeze new version before paid acceptance; any used-for-tuning case becomes development data'],
        'cases': entries}
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                        encoding='utf-8', newline='\r\n')
    return load_dataset(folder / 'manifest.json')


if __name__ == '__main__':
    print(json.dumps(write_dataset()['validation'], ensure_ascii=False))
