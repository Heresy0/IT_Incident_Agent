"""Bounded Prometheus/Loki reads using operator-registered query templates."""
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx
from pydantic import ValidationError
from providers.fixtures import ProviderError
from providers.logs import LogMapping


def request_json(base, method, path, *, params=None, authorization_env=None, transport=None):
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ProviderError('ENDPOINT_INVALID')
    if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ProviderError('ENDPOINT_TLS_REQUIRED')
    headers = {}
    if authorization_env:
        token = os.getenv(authorization_env)
        if not token:
            raise ProviderError('PROVIDER_AUTH_MISSING')
        headers['Authorization'] = token
    try:
        started = time.monotonic()
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream(method, base.rstrip('/') + path, params=params, headers=headers) as response:
                if response.status_code == 429:
                    raise ProviderError('RATE_LIMIT', True)
                if not 200 <= response.status_code < 300:
                    raise ProviderError('PROVIDER_HTTP_ERROR')
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > 10:
                        raise ProviderError('TIMEOUT', True)
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ProviderError('PROVIDER_RESPONSE_TOO_LARGE')
                    chunks.append(chunk)
                body = b''.join(chunks)
                return json.loads(body) if body else {}
    except httpx.TimeoutException:
        raise ProviderError('TIMEOUT', True) from None
    except httpx.HTTPError:
        raise ProviderError('NETWORK_ERROR', True) from None
    except (ValueError, TypeError):
        raise ProviderError('PROVIDER_FORMAT_INVALID') from None


def safe_text(text):
    # Only sanitized log messages enter model prompts/evidence, never raw log documents.
    text = re.sub(r'(?i)bearer\s+\S+', 'Bearer [REDACTED]', str(text))
    text = re.sub(r'''(?i)(["']?(?:password|token|api[_-]?key|secret|authorization|access_token|refresh_token)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED]', text)
    return text[:1800]


class HTTPObservationProvider:
    name = 'registered_prometheus_loki'

    def __init__(self, binding, ticket, principal, *, transport=None):
        self.binding, self._ticket, self.principal, self.transport = binding, ticket, principal, transport
        try:
            self.log_mapping = LogMapping.model_validate(binding.observations.get('log_mapping', {}))
        except ValidationError:
            raise ProviderError('LOG_MAPPING_INVALID') from None

    def ticket(self):
        return self._ticket.model_copy(deep=True)

    def authorize(self, principal, scope):
        if principal != self.principal or scope != self._ticket.scope:
            raise ProviderError('SCOPE_DENIED')

    def _read(self, source, path, params):
        config = self.binding.observations.get(source)
        if not config:
            raise ProviderError('SOURCE_NOT_CONFIGURED')
        return request_json(config['url'], 'GET', path, params=params,
                            authorization_env=config.get('authorization_env'), transport=self.transport)

    def _row(self, timestamp, **fields):
        payload = {'timestamp': timestamp, 'service': self.binding.service,
                   'environment': self.binding.environment, 'data_version': self.binding.version, **fields}
        payload['id'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        return payload

    def query(self, name, args, scope, *, max_metric_points=60):
        self.authorize(self.principal, scope)
        if hasattr(args, 'start') and (args.start < scope.start or args.end > scope.end):
            raise ProviderError('WINDOW_DENIED')
        if name == 'get_service_metrics':
            definitions = self.binding.observations.get('metrics', {})
            if any(metric not in definitions for metric in args.metrics):
                raise ProviderError('METRIC_NOT_REGISTERED')
            rows, truncated = [], False
            point_limit = max(1, min(60, max_metric_points))
            requested_step = 60 if args.granularity == '1m' else 300
            # Prometheus parses range endpoints at millisecond precision. Round inward
            # and anchor the grid on the end so the latest observation remains in scope.
            lower = math.ceil(args.start.timestamp() * 1000) / 1000
            query_end = math.floor(args.end.timestamp() * 1000) / 1000
            if lower > query_end:
                return [], False
            duration = query_end - lower
            step = max(requested_step, int(duration / max(1, point_limit - 1)))
            intervals = min(point_limit - 1, int(duration // step))
            query_start = query_end - intervals * step
            for metric in args.metrics:
                definition = definitions[metric]
                data = self._read('prometheus', '/api/v1/query_range', {
                    'query': definition['query'], 'start': query_start, 'end': query_end,
                    'step': step})
                if data.get('status') != 'success' or data.get('data', {}).get('resultType') != 'matrix':
                    raise ProviderError('PROVIDER_FORMAT_INVALID')
                series = data['data']['result']
                # Registered queries must aggregate to one service series; do not silently mix labels.
                if len(series) > 1:
                    raise ProviderError('METRIC_QUERY_NOT_AGGREGATED')
                points = series[0]['values'] if series else []
                truncated |= len(points) > point_limit
                for stamp, value in points[-point_limit:]:
                    moment = datetime.fromtimestamp(float(stamp), timezone.utc)
                    number = float(value)
                    if args.start <= moment <= args.end and math.isfinite(number):
                        rows.append(self._row(moment.isoformat(), metric=metric, value=number,
                            unit=definition['unit'], aggregation=definition.get('aggregation', 'registered'),
                            granularity=args.granularity if step == requested_step else f'{step}s',
                            requested_granularity=args.granularity))
            return sorted(rows, key=lambda row: row['timestamp'], reverse=True), truncated
        if name == 'get_service_logs':
            self.last_log_stats = {}
            expression = self.binding.observations.get('log_queries', {}).get(args.category)
            if not expression:
                raise ProviderError('LOG_CATEGORY_NOT_REGISTERED')
            data = self._read('loki', '/loki/api/v1/query_range', {'query': expression,
                'start': int(args.start.timestamp() * 1e9), 'end': int(args.end.timestamp() * 1e9),
                'limit': 51, 'direction': 'forward'})
            if data.get('status') != 'success' or data.get('data', {}).get('resultType') != 'streams':
                raise ProviderError('PROVIDER_FORMAT_INVALID')
            rows = []
            count = 0
            unusable = filtered = 0
            for stream in data['data']['result']:
                for stamp, line in stream['values']:
                    count += 1
                    moment = datetime.fromtimestamp(int(stamp) / 1e9, timezone.utc)
                    if not args.start <= moment <= args.end:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        record = {'message': line}
                    normalized = self.log_mapping.normalize(record)
                    if normalized is None:
                        unusable += 1
                        continue
                    message, level, code = normalized
                    if args.level and level != args.level or args.error_code and code != args.error_code:
                        filtered += 1
                        continue
                    rows.append(self._row(moment.isoformat(), category=args.category, level=level,
                        error_code=code, message=safe_text(message), content_truncated=len(message) > 1800))
            rows.sort(key=lambda row: (row['timestamp'], row['id']))
            self.last_log_stats = {'source_rows': count, 'unusable_rows': unusable,
                                   'filtered_rows': filtered, 'retained_rows': min(len(rows), 50)}
            return rows[:50], count >= 51 or len(rows) > 50
        # Small operator-maintained metadata; no model-supplied paths or knowledge-base credentials.
        key = {'get_recent_changes': 'changes', 'get_service_owner': 'owners',
               'search_runbooks': 'runbooks', 'search_incidents': 'incidents'}.get(name)
        if key is None:
            raise ProviderError('TOOL_DENIED')
        if key not in self.binding.observations:
            raise ProviderError('SOURCE_NOT_CONFIGURED')
        rows = []
        for item in self.binding.observations[key]:
            services = {scope.service, *scope.allowed_dependencies} if key == 'owners' else {scope.service}
            if item.get('service', scope.service) not in services or item.get('environment', scope.environment) != scope.environment:
                continue
            if name == 'get_service_owner' and args.alias not in item.get('aliases', []):
                continue
            if hasattr(args, 'start') and not args.start <= datetime.fromisoformat(item['timestamp']) <= args.end:
                continue
            if name == 'get_recent_changes' and args.category and item.get('category') != args.category:
                continue
            if key in {'runbooks', 'incidents'}:
                if scope.service_version not in item.get('versions', []):
                    continue
                query = args.query if key == 'runbooks' else args.symptoms
                if not set(re.findall(r'\w+', query.casefold())).intersection(re.findall(r'\w+', item.get('text', '').casefold())):
                    continue
                if key == 'runbooks' and args.category and item.get('category') != args.category:
                    continue
                if key == 'incidents' and (not item.get('confirmed') or item.get('validity') != 'active'
                    or args.version and args.version != scope.service_version
                    or args.error_code and args.error_code not in item.get('error_codes', [])):
                    continue
            allowed = {'team', 'aliases', 'escalation', 'category', 'summary', 'text', 'versions',
                       'updated_at', 'confirmed_at', 'confirmed', 'resolution', 'validity'}
            fields = {key: value for key, value in item.items() if key in allowed}
            if key == 'owners':
                fields['service'] = item.get('service', scope.service)
            for field in ('summary', 'text', 'resolution', 'escalation'):
                if field in fields:
                    fields[field] = safe_text(fields[field])
            rows.append(self._row(item['timestamp'], **fields))
        cap = 1 if key == 'owners' else 10 if key == 'changes' else 4
        return rows[:cap], len(rows) > cap
