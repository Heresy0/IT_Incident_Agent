"""Four independent, executed local failure mechanisms for evaluation only.

No socket, provider, production service, or business module is used here. Raw
trials retain what the Python operations returned. Public rows are projections
of those measurements; reference interpretations stay in a separate field and
are not human-verified gold.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
from io import BytesIO
import json
import time
from urllib.parse import urljoin, urlsplit


SCHEMA_VERSION = 'rca-independent-program-experiments-v1'
CASE_IDS = tuple('rca_independent_' + value for value in ('005', '006', '007', '008'))


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _canonical_sha(value):
    return _sha_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _validate_repeats(repeats):
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError('repeats must be an integer between 1 and 5')


def _check(name, passed, observed):
    return {'name': name, 'passed': bool(passed), 'observed': observed}


def _row(observation_id, category, message):
    return {'observation_id': observation_id, 'category': category,
        'message': deepcopy(message)}


def _table(columns, measurements):
    """Compact fact tables keep every repeat visible within snippet budgets."""
    return {'columns': columns, 'measurements': measurements}


def _finish(case_id, family, parameters, trials, checks, public_rows, reference, started):
    return {'schema_version': SCHEMA_VERSION, 'case_id': case_id, 'family': family,
        'origin': 'executed_synthetic_experiment', 'human_verified': False,
        'is_gold': False, 'human_review_status': 'pending',
        'causal_truth_validated': False, 'model_calls': 0,
        'original_incident_reproduced': False,
        'status': 'passed' if all(item['passed'] for item in checks) else 'failed',
        'executed_at_utc': datetime.now(timezone.utc).isoformat(),
        'wall_seconds': time.perf_counter() - started, 'parameters': parameters,
        'trials': trials, 'checks': checks, 'public_rows': public_rows,
        'reference': reference, 'limitations': list(reference['limitations'])}


class _BytesSocket:
    """The HTTPResponse file interface backed by actual local wire bytes."""
    def __init__(self, payload):
        self._payload = payload

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('Only a binary HTTP reader is expected')
        return BytesIO(self._payload)


def _http_read(body, declared_length):
    wire = (b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(declared_length).encode('ascii')
        + b'\r\nConnection: close\r\n\r\n' + body)
    response = http.client.HTTPResponse(_BytesSocket(wire))
    response.begin()
    observed = {'configured_content_length': declared_length,
        'wire_body_bytes': len(body), 'wire_body_sha256': _sha_bytes(body),
        'parsed_status': response.status,
        'parsed_content_length': int(response.getheader('Content-Length'))}
    try:
        received = response.read()
        observed.update({'success': True, 'received_bytes': len(received),
            'received_sha256': _sha_bytes(received), 'exception_type': None,
            'expected_additional_bytes': 0})
    except http.client.IncompleteRead as exc:
        observed.update({'success': False, 'received_bytes': len(exc.partial),
            'received_sha256': _sha_bytes(exc.partial),
            'exception_type': type(exc).__name__, 'exception_message': str(exc),
            'expected_additional_bytes': exc.expected})
    finally:
        response.close()
    return observed


def reproduce_http_content_length(repeats=3):
    """Read identical body bytes with correct, oversized, restored HTTP lengths."""
    _validate_repeats(repeats)
    started = time.perf_counter()
    body = b'{"order_id":42,"state":"paid"}'
    correct_length = len(body)
    oversized_length = correct_length + 11
    parameters = {'transport': 'local_bytesio_http_wire',
        'reader': 'stdlib http.client.HTTPResponse.read',
        'body_hex': body.hex(), 'declared_lengths': [correct_length, oversized_length, correct_length],
        'repeats': repeats}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        baseline = _http_read(body, correct_length)
        fault = _http_read(body, oversized_length)
        restored = _http_read(body, correct_length)
        trial = {'repeat': repeat, 'baseline': baseline, 'fault': fault, 'restored': restored}
        trials.append(trial)
        checks.extend([
            _check(f'repeat_{repeat}_baseline_complete', baseline['success']
                and baseline['received_bytes'] == correct_length, baseline),
            _check(f'repeat_{repeat}_oversized_header_incomplete_read', not fault['success']
                and fault['exception_type'] == 'IncompleteRead'
                and fault['expected_additional_bytes'] == 11
                and fault['received_bytes'] == correct_length, fault),
            _check(f'repeat_{repeat}_restored_complete', restored['success']
                and restored['received_bytes'] == correct_length, restored),
            _check(f'repeat_{repeat}_same_body_and_status',
                len({phase['wire_body_sha256'] for phase in (baseline, fault, restored)}) == 1
                and all(phase['parsed_status'] == 200 for phase in (baseline, fault, restored))
                and all(phase['received_sha256'] == _sha_bytes(body)
                    for phase in (baseline, fault, restored)),
                {'body_sha256': [phase['wire_body_sha256'] for phase in (baseline, fault, restored)],
                 'parsed_status': [phase['parsed_status'] for phase in (baseline, fault, restored)]}),
        ])
    public_rows = [
        _row('row_01', 'configuration', {'reader': parameters['reader'],
            'measurements': [{'repeat': trial['repeat'],
                'before_declared_bytes': trial['baseline']['configured_content_length'],
                'changed_declared_bytes': trial['fault']['configured_content_length'],
                'wire_body_bytes': trial['fault']['wire_body_bytes'],
                'body_sha256': trial['fault']['wire_body_sha256']} for trial in trials]}),
        _row('row_02', 'dependency', _table(['repeat', 'parsed_status', 'parsed_content_length',
            'wire_body_bytes', 'received_bytes', 'exception_type', 'expected_additional_bytes'], [
            [trial['repeat']] + [trial['fault'][key] for key in ('parsed_status',
                'parsed_content_length', 'wire_body_bytes', 'received_bytes',
                'exception_type', 'expected_additional_bytes')] for trial in trials])),
        _row('row_03', 'dependency', _table(['repeat', 'before_declared_bytes', 'before_success',
            'before_received_bytes', 'restored_declared_bytes', 'restored_success',
            'restored_received_bytes', 'restored_body_sha256'], [[trial['repeat'],
                trial['baseline']['configured_content_length'], trial['baseline']['success'],
                trial['baseline']['received_bytes'], trial['restored']['configured_content_length'],
                trial['restored']['success'], trial['restored']['received_bytes'],
                trial['restored']['wire_body_sha256']] for trial in trials])),
    ]
    reference = {'causal_condition': 'The Content-Length header was larger than the actual local response body.',
        'mechanism': 'HTTPResponse.read() expects the declared remaining body bytes and raises IncompleteRead at EOF.',
        'reference_explanation': 'The same body and 200 status are read completely with the measured length, fail with eleven additional declared bytes, and complete after only the header length is restored.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'dependency'],
        'acceptable_aliases': ['Content-Length mismatch', 'oversized response length', 'HTTP body framing error',
            'Content-Length大于实际响应体', '响应长度声明错误', '响应体长度不匹配'],
        'forbidden_assertions': ['The remote database crashed.', 'A TCP or production network outage was reproduced.'],
        'limitations': ['This is a local BytesIO-backed HTTP parser experiment, without a network connection.',
            'It does not establish which production proxy, server, or code path generated a bad header.']}
    return _finish(CASE_IDS[0], 'http_response_framing', parameters, trials, checks,
        public_rows, reference, started)


def _decode(payload, encoding):
    observed = {'configured_encoding': encoding, 'input_bytes': len(payload),
        'input_sha256': _sha_bytes(payload), 'decode_errors_policy': 'strict'}
    try:
        text = payload.decode(encoding, errors='strict')
        observed.update({'success': True, 'decoded_text': text,
            'decoded_utf8_sha256': _sha_bytes(text.encode('utf-8')), 'exception_type': None})
    except UnicodeDecodeError as exc:
        observed.update({'success': False, 'exception_type': type(exc).__name__,
            'exception_message': str(exc), 'error_start': exc.start, 'error_end': exc.end,
            'error_reason': exc.reason})
    return observed


def reproduce_character_decoding(repeats=3):
    """Decode fixed non-ASCII UTF-8 bytes under UTF-8, ASCII, restored UTF-8."""
    _validate_repeats(repeats)
    started = time.perf_counter()
    text = '订单已支付'
    payload = text.encode('utf-8')
    parameters = {'input_hex': payload.hex(), 'encodings': ['utf-8', 'ascii', 'utf-8'],
        'decode_errors_policy': 'strict', 'repeats': repeats}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        baseline = _decode(payload, 'utf-8')
        fault = _decode(payload, 'ascii')
        restored = _decode(payload, 'utf-8')
        trials.append({'repeat': repeat, 'baseline': baseline, 'fault': fault, 'restored': restored})
        checks.extend([
            _check(f'repeat_{repeat}_baseline_utf8_decode', baseline['success']
                and baseline['decoded_text'] == text, baseline),
            _check(f'repeat_{repeat}_ascii_raises_unicode_decode_error', not fault['success']
                and fault['exception_type'] == 'UnicodeDecodeError'
                and fault['error_start'] == 0, fault),
            _check(f'repeat_{repeat}_restored_utf8_decode', restored['success']
                and restored['decoded_text'] == text, restored),
            _check(f'repeat_{repeat}_fixed_bytes_and_strict_mode',
                len({phase['input_sha256'] for phase in (baseline, fault, restored)}) == 1
                and all(phase['decode_errors_policy'] == 'strict'
                    for phase in (baseline, fault, restored)),
                {'input_sha256': [phase['input_sha256'] for phase in (baseline, fault, restored)]}),
        ])
    public_rows = [
        _row('row_01', 'configuration', {'measurements': [{'repeat': trial['repeat'],
            'before_encoding': trial['baseline']['configured_encoding'],
            'changed_encoding': trial['fault']['configured_encoding'],
            'input_bytes': trial['fault']['input_bytes'],
            'input_sha256': trial['fault']['input_sha256'],
            'errors_policy': trial['fault']['decode_errors_policy']} for trial in trials]}),
        _row('row_02', 'dependency', _table(['repeat', 'configured_encoding', 'success',
            'exception_type', 'error_start', 'error_end', 'error_reason'], [
            [trial['repeat']] + [trial['fault'][key] for key in ('configured_encoding',
                'success', 'exception_type', 'error_start', 'error_end', 'error_reason')]
            for trial in trials])),
        _row('row_03', 'dependency', _table(['repeat', 'before_encoding', 'before_success',
            'before_decoded_text', 'restored_encoding', 'restored_success',
            'restored_decoded_text', 'restored_input_sha256'], [[trial['repeat'],
                trial['baseline']['configured_encoding'], trial['baseline']['success'],
                trial['baseline']['decoded_text'], trial['restored']['configured_encoding'],
                trial['restored']['success'], trial['restored']['decoded_text'],
                trial['restored']['input_sha256']] for trial in trials])),
    ]
    reference = {'causal_condition': 'The configured decoder changed from UTF-8 to ASCII while the input remained non-ASCII UTF-8.',
        'mechanism': 'Strict ASCII decoding rejects the first byte above 127 with UnicodeDecodeError.',
        'reference_explanation': 'Identical input hashes decode to the same text before and after UTF-8 restoration, but fail at byte zero under ASCII.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'dependency'],
        'acceptable_aliases': ['decoder configuration mismatch', 'ASCII decoder on UTF-8 bytes',
            'encoding mismatch', '字符解码配置错误', '用ASCII解码UTF8字节'],
        'forbidden_assertions': ['The input bytes changed or were truncated between phases.',
            'The source database stored corrupted bytes.'],
        'limitations': ['This is a Python bytes.decode experiment with one fixed Unicode payload.',
            'It does not reproduce storage corruption or identify who changed a production configuration.']}
    return _finish(CASE_IDS[1], 'character_decoder_mismatch', parameters, trials, checks,
        public_rows, reference, started)


class _VersionedSource:
    def __init__(self, key, value):
        self._rows = {key: {'key': key, 'value': value, 'version': 1}}
        self.reads = 0

    def read(self, key):
        self.reads += 1
        return deepcopy(self._rows[key])

    def write(self, key, value):
        before = deepcopy(self._rows[key])
        self._rows[key] = {'key': key, 'value': value, 'version': before['version'] + 1}
        return {'before': before, 'after': deepcopy(self._rows[key])}


class _ReadThroughCache:
    def __init__(self, source, invalidate_on_source_update):
        self.source = source
        self.invalidate_on_source_update = invalidate_on_source_update
        self.entries = {}
        self.invalidations = 0

    def read(self, key):
        before_reads = self.source.reads
        cache_hit = key in self.entries
        if not cache_hit:
            self.entries[key] = self.source.read(key)
        return {**deepcopy(self.entries[key]), 'cache_hit': cache_hit,
            'source_reads_for_lookup': self.source.reads - before_reads}

    def write_source(self, key, value):
        update = self.source.write(key, value)
        before_invalidations = self.invalidations
        if self.invalidate_on_source_update:
            self.invalidate(key)
        return {**update, 'invalidate_on_source_update': self.invalidate_on_source_update,
            'invalidations_performed': self.invalidations - before_invalidations}

    def invalidate(self, key):
        removed = self.entries.pop(key, None)
        self.invalidations += 1
        return {'key': key, 'removed': removed is not None,
            'removed_entry_version': removed['version'] if removed else None}


def _cache_phase(source, cache, key):
    source_snapshot = source.read(key)
    read_result = cache.read(key)
    return {'lookup_key': key, 'source_snapshot': source_snapshot,
        'read_result': read_result, 'invalidations_total': cache.invalidations,
        'observed_value_matches_source': read_result['value'] == source_snapshot['value'],
        'observed_version_matches_source': read_result['version'] == source_snapshot['version']}


def reproduce_stale_local_cache(repeats=3):
    """Actually retain an old cache entry across a source write, then evict it."""
    _validate_repeats(repeats)
    started = time.perf_counter()
    parameters = {'storage': 'in_memory_versioned_source_and_read_through_cache',
        'lookup_key': 'stock:item-42', 'initial_source_value': 12, 'updated_source_value': 9,
        'invalidate_on_source_update': False, 'restoration_operation': 'explicit_cache_invalidate',
        'repeats': repeats}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        key = parameters['lookup_key']
        source = _VersionedSource(key, parameters['initial_source_value'])
        cache = _ReadThroughCache(source, invalidate_on_source_update=False)
        baseline = _cache_phase(source, cache, key)
        source_update = cache.write_source(key, parameters['updated_source_value'])
        fault = _cache_phase(source, cache, key)
        direct_read_control = source.read(key)
        invalidation = cache.invalidate(key)
        restored = _cache_phase(source, cache, key)
        trial = {'repeat': repeat, 'baseline': baseline, 'fault': fault, 'restored': restored,
            'source_update': source_update, 'direct_source_read_control': direct_read_control,
            'invalidation': invalidation}
        trials.append(trial)
        checks.extend([
            _check(f'repeat_{repeat}_baseline_cache_miss_matches_source',
                baseline['observed_value_matches_source'] and baseline['observed_version_matches_source']
                and not baseline['read_result']['cache_hit'], baseline),
            _check(f'repeat_{repeat}_source_update_leaves_stale_cached_value',
                source_update['invalidations_performed'] == 0
                and fault['read_result']['cache_hit']
                and fault['read_result']['value'] == parameters['initial_source_value']
                and fault['source_snapshot']['value'] == parameters['updated_source_value']
                and not fault['observed_value_matches_source']
                and not fault['observed_version_matches_source'], fault),
            _check(f'repeat_{repeat}_eviction_refreshes_value', invalidation['removed']
                and restored['observed_value_matches_source']
                and restored['observed_version_matches_source']
                and not restored['read_result']['cache_hit']
                and restored['read_result']['source_reads_for_lookup'] == 1, restored),
            _check(f'repeat_{repeat}_source_unchanged_during_cache_intervention',
                fault['source_snapshot'] == restored['source_snapshot'] == direct_read_control
                and baseline['lookup_key'] == fault['lookup_key'] == restored['lookup_key'],
                {'direct_source_read_control': direct_read_control,
                 'before_eviction_source': fault['source_snapshot'],
                 'after_eviction_source': restored['source_snapshot']}),
        ])
    public_rows = [
        _row('row_01', 'configuration', {'measurements': [
            {'repeat': trial['repeat'], **trial['source_update']} for trial in trials]}),
        _row('row_02', 'resource', _table(['repeat', 'lookup_key', 'source_value', 'source_version',
            'cached_value', 'cached_version', 'cache_hit', 'source_reads_for_lookup',
            'invalidations_total'], [[trial['repeat'], trial['fault']['lookup_key'],
                trial['fault']['source_snapshot']['value'], trial['fault']['source_snapshot']['version'],
                trial['fault']['read_result']['value'], trial['fault']['read_result']['version'],
                trial['fault']['read_result']['cache_hit'],
                trial['fault']['read_result']['source_reads_for_lookup'],
                trial['fault']['invalidations_total']] for trial in trials])),
        _row('row_03', 'resource', _table(['repeat', 'initial_read_value', 'initial_source_version',
            'eviction_removed', 'evicted_version', 'after_source_value', 'after_source_version',
            'after_read_value', 'after_read_version', 'after_cache_hit', 'after_source_reads'], [
            [trial['repeat'], trial['baseline']['read_result']['value'],
             trial['baseline']['source_snapshot']['version'], trial['invalidation']['removed'],
             trial['invalidation']['removed_entry_version'], trial['restored']['source_snapshot']['value'],
             trial['restored']['source_snapshot']['version'], trial['restored']['read_result']['value'],
             trial['restored']['read_result']['version'], trial['restored']['read_result']['cache_hit'],
             trial['restored']['read_result']['source_reads_for_lookup']] for trial in trials])),
        _row('row_04', 'resource', {'measurements': [
            {'repeat': trial['repeat'], 'direct_source_read': trial['direct_source_read_control']}
            for trial in trials]}),
    ]
    reference = {'causal_condition': 'The source update did not invalidate an existing local cache entry.',
        'mechanism': 'The read-through cache returns its version-one entry without reading the updated version-two source; explicit eviction forces a fresh source read.',
        'reference_explanation': 'The source changes from value12/version1 to value9/version2 while cached reads remain12/version1. With source and lookup key unchanged, evicting that entry produces9/version2.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'resource'],
        'acceptable_aliases': ['missing cache invalidation', 'stale cache entry after source update',
            'cache coherence failure', '源数据更新未失效缓存', '缓存陈旧', '缓存失效缺失'],
        'forbidden_assertions': ['The source write failed.', 'Eviction permanently fixes future updates.',
            'A real distributed Redis cluster was reproduced.'],
        'limitations': ['This is one actual in-memory cache and source, without distributed consistency or a cache server.',
            'Explicit eviction restores this lookup; automatic invalidation remains disabled, so future updates can reproduce the problem.']}
    return _finish(CASE_IDS[2], 'cache_invalidation_omission', parameters, trials, checks,
        public_rows, reference, started)


def _route_lookup(prefix, endpoint, route_table):
    base = 'https://local.invalid' + prefix.rstrip('/') + '/'
    url = urljoin(base, endpoint.lstrip('/'))
    parsed = urlsplit(url)
    route = route_table.get(('GET', parsed.path))
    return {'configured_prefix': prefix, 'logical_endpoint': endpoint, 'request_method': 'GET',
        'built_url': url, 'parsed_path': parsed.path, 'parsed_query': parsed.query,
        'registered_paths': sorted(key[1] for key in route_table),
        'route_table_sha256': _canonical_sha([
            {'method': key[0], 'path': key[1], 'response': route_table[key]}
            for key in sorted(route_table)]),
        'route_matched': route is not None,
        'response_status': route['status'] if route else 404,
        'response_body': route['body'] if route else 'route not found'}


def reproduce_url_prefix_routing(repeats=3):
    """Build and match real parsed paths with only the configured prefix changed."""
    _validate_repeats(repeats)
    started = time.perf_counter()
    endpoint = 'orders/42?include=items'
    route_table = {('GET', '/api/v1/orders/42'): {'status': 200, 'body': 'order42 paid'}}
    parameters = {'transport': 'local_url_parser_and_exact_route_table',
        'logical_endpoint': endpoint, 'prefixes': ['/api/v1', '/api/v2', '/api/v1'],
        'request_method': 'GET', 'repeats': repeats}
    trials, checks = [], []
    for repeat in range(1, repeats + 1):
        baseline = _route_lookup('/api/v1', endpoint, route_table)
        fault = _route_lookup('/api/v2', endpoint, route_table)
        restored = _route_lookup('/api/v1', endpoint, route_table)
        direct_route_control = _route_lookup('/api/v1', endpoint, route_table)
        trials.append({'repeat': repeat, 'baseline': baseline, 'fault': fault,
            'restored': restored, 'direct_route_control': direct_route_control})
        checks.extend([
            _check(f'repeat_{repeat}_baseline_route_found', baseline['route_matched']
                and baseline['response_status'] == 200, baseline),
            _check(f'repeat_{repeat}_changed_prefix_route_not_found', not fault['route_matched']
                and fault['response_status'] == 404 and fault['parsed_path'] == '/api/v2/orders/42', fault),
            _check(f'repeat_{repeat}_restored_route_found', restored['route_matched']
                and restored['response_status'] == 200, restored),
            _check(f'repeat_{repeat}_same_endpoint_method_table_and_query',
                len({phase['route_table_sha256'] for phase in (baseline, fault, restored)}) == 1
                and all(phase['logical_endpoint'] == endpoint and phase['request_method'] == 'GET'
                    and phase['parsed_query'] == 'include=items' for phase in (baseline, fault, restored))
                and direct_route_control['route_matched'],
                {'route_table_sha256': [phase['route_table_sha256'] for phase in (baseline, fault, restored)],
                 'direct_route_control': direct_route_control}),
        ])
    public_rows = [
        _row('row_01', 'configuration', {'measurements': [{'repeat': trial['repeat'],
            'before_prefix': trial['baseline']['configured_prefix'],
            'changed_prefix': trial['fault']['configured_prefix'],
            'logical_endpoint': trial['fault']['logical_endpoint'],
            'request_method': trial['fault']['request_method']} for trial in trials]}),
        _row('row_02', 'dependency', _table(['repeat', 'configured_prefix', 'parsed_path',
            'parsed_query', 'route_matched', 'response_status', 'registered_paths',
            'route_table_sha256'], [[trial['repeat']] + [trial['fault'][key] for key in
                ('configured_prefix', 'parsed_path', 'parsed_query', 'route_matched',
                 'response_status', 'registered_paths', 'route_table_sha256')] for trial in trials])),
        _row('row_03', 'dependency', _table(['repeat', 'before_prefix', 'before_path', 'before_status',
            'restored_prefix', 'restored_path', 'restored_status', 'restored_route_table_sha256'], [
            [trial['repeat'], trial['baseline']['configured_prefix'], trial['baseline']['parsed_path'],
             trial['baseline']['response_status'], trial['restored']['configured_prefix'],
             trial['restored']['parsed_path'], trial['restored']['response_status'],
             trial['restored']['route_table_sha256']] for trial in trials])),
        _row('row_04', 'dependency', _table(['repeat', 'direct_path', 'direct_route_matched',
            'direct_status', 'direct_route_table_sha256'], [[trial['repeat'],
                trial['direct_route_control']['parsed_path'],
                trial['direct_route_control']['route_matched'],
                trial['direct_route_control']['response_status'],
                trial['direct_route_control']['route_table_sha256']] for trial in trials])),
    ]
    reference = {'causal_condition': 'The URL path prefix changed to /api/v2 while the unchanged route table only registered /api/v1.',
        'mechanism': 'urllib.parse builds /api/v2/orders/42 and the exact GET route lookup returns no match, producing a local404.',
        'reference_explanation': 'The endpoint, method, query, and route table stay fixed. Only the prefix changes the parsed path; restoring /api/v1 restores route matching and a200 result.',
        'key_observation_ids': ['row_01', 'row_02', 'row_03'],
        'required_categories': ['configuration', 'dependency'],
        'acceptable_aliases': ['URL path prefix mismatch', 'wrong route prefix', 'incorrect API version prefix',
            '路径前缀拼接错误', 'URL前缀配置错误', '路由路径不匹配'],
        'forbidden_assertions': ['The registered route was removed.', 'A DNS or TCP connection failed.',
            'The remote service was unavailable.'],
        'limitations': ['This executes urllib.parse and an in-memory exact route matcher without sending HTTP requests.',
            'It cannot establish behavior of a real gateway, wildcard route matcher, DNS service, or production server.']}
    return _finish(CASE_IDS[3], 'url_prefix_route_mismatch', parameters, trials, checks,
        public_rows, reference, started)


def run_experiments(repeats=3):
    """Execute all four bounded experiments, preserving every first-run result."""
    _validate_repeats(repeats)
    return [reproduce_http_content_length(repeats), reproduce_character_decoding(repeats),
        reproduce_stale_local_cache(repeats), reproduce_url_prefix_routing(repeats)]
