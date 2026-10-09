import hashlib
import json
import time
from datetime import datetime, timezone
from api.auth import Principal
from runtime.context import ExecutionError, RunContext
from evidence.contracts import (TOOL_ARGS, INVESTIGATION_TOOLS, KNOWLEDGE_TOOLS, SINGLE_TOOLS, Evidence,
                        ToolResult, ToolError, ReferenceOption, EvidenceRef)
from providers.fixtures import ProviderError
from pydantic import ValidationError


def reference_field_paths(payload, prefix=""):
    """Describe the actual bounded payload; never invent fields or aliases."""
    paths = []
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.extend(reference_field_paths(value, path))
        else:
            paths.append(path)
    return sorted(paths)[:40]


class ToolExecutor:
    """One budget entry per real attempt; all boundaries checked before reservation."""
    def __init__(self, provider, principal: Principal, scope, context: RunContext, role="investigation"):
        self.provider, self.principal, self.scope, self.context, self.role = provider, principal, scope, context, role
        self.seen = set()
        self.evidence = {}
        self.references = {}
        self.last_query_profile = {}
        self.log_gaps = {}
        self.log_coverage = []

    def capabilities(self):
        """Public registration names only; never expose endpoint URLs or query templates."""
        if not hasattr(self.provider, 'binding'):
            return {}
        config = self.provider.binding.observations
        return {'registered_metrics': {k: {'unit': v.get('unit', '')}
                    for k, v in config.get('metrics', {}).items()} if config.get('prometheus') else {},
                'registered_log_categories': sorted(config.get('log_queries', {}))
                    if config.get('loki') else []}

    def schemas(self):
        allowed = self.allowed_tools()
        descriptions = {
            "get_service_metrics": "Read symptom-relevant metrics and healthy controls within ticket window. dependency_error_rate measures dependency failures; pool_usage/pool_wait measure application connection pressure; db_cpu is a database control; request_error_rate/request_latency show impact. Select a small useful set, not every metric. At most 60 points per metric; 1m/5m granularity.",
            "get_service_logs": "Read one log category: dependency for connectivity/health/timeouts, configuration for authentication/configuration failures, resource for pool acquisition/load/resource pressure. Omit level/error_code unless needed: restrictive filters can hide WARN or INFO controls. Empty means no matching records, not healthy. At most 50 records within ticket window.",
            "get_recent_changes": "Read sanitized changes within ticket window, at most 10. category deployment/configuration/scaling is optional: omit it to inspect all change kinds. Release timing alone does not prove cause.",
            "get_service_owner": "Read registered owner for ticket service or allowed dependency alias.",
            "search_runbooks": "Search applicable readable synthetic runbooks, at most 4 passages.",
            "search_incidents": "Search visible confirmed active cases of applicable version, at most 4 cases.",
        }
        definitions = (self.provider.binding.observations.get('metrics', {}) if hasattr(self.provider, 'binding')
                       else {r['metric']: {} for r in self.provider.base._data['metrics']} if hasattr(self.provider, 'base')
                       else {r['metric']: {} for r in self.provider._data['metrics']} if hasattr(self.provider, '_data') else {})
        descriptions['get_service_metrics'] = ('Read only registered metrics within the ticket window; at most 60 points per metric. '
            'Available metrics and units: ' + json.dumps({k: v.get('unit', '') for k, v in definitions.items()})
            + '. Select a small useful set. Missing samples do not establish health.')
        descriptions['search_runbooks'] = 'Search registered applicable readable runbooks, at most 4 passages.'
        capabilities = self.capabilities()
        if capabilities:
            descriptions['get_service_logs'] = ('仅查询已登记的日志类别：'
                + json.dumps(capabilities['registered_log_categories'])
                + '。未登记表示渠道不可用，不能通过重试补齐；保留信息缺口。最多返回 50 条，通常省略 level。')
            descriptions['get_service_metrics'] += (' 采样间隔按完整证据容量自动调整；返回 granularity 为实际间隔，'
                'requested_granularity 为请求值。稀疏采样不能排除短暂故障，必要时缩小时间窗口。')
        schemas = [{"type": "function", "function": {"name": n, "description": descriptions[n],
                "parameters": TOOL_ARGS[n].model_json_schema()}} for n in sorted(allowed)]
        for schema in schemas:
            if schema['function']['name'] == 'get_service_metrics' and capabilities:
                schema['function']['parameters']['properties']['metrics']['items']['enum'] = sorted(capabilities['registered_metrics'])
            if (schema['function']['name'] == 'get_service_logs' and capabilities
                    and capabilities['registered_log_categories']):
                schema['function']['parameters']['properties']['category']['enum'] = capabilities['registered_log_categories']
        return [schema for schema in schemas if not capabilities or not (
            schema['function']['name'] == 'get_service_metrics' and not capabilities['registered_metrics']
            or schema['function']['name'] == 'get_service_logs' and not capabilities['registered_log_categories'])]

    @staticmethod
    def rejected(code, retryable=False):
        return ToolResult(status="error", error=ToolError(code=code, retryable=retryable))

    def allowed_tools(self):
        return {"investigation": INVESTIGATION_TOOLS, "knowledge": KNOWLEDGE_TOOLS, "single": SINGLE_TOOLS}.get(self.role, ())

    def validate_args(self, name, raw_args):
        """Shared preflight for tool execution and proposed read-only rework; no calls or mutation."""
        allowed = self.allowed_tools()
        if name not in allowed or self.principal.role not in {"user", "operator"}:
            raise ExecutionError("TOOL_DENIED")
        if (self.scope.tenant_id, self.scope.user_id) != (self.principal.tenant_id, self.principal.user_id):
            raise ExecutionError("SCOPE_DENIED")
        try:
            # Tool protocol supplies JSON datetime strings; strict JSON rejects coercion of numbers/enums.
            args = TOOL_ARGS[name].model_validate_json(json.dumps(raw_args))
            self.provider.authorize(self.principal, self.scope)
        except (ValidationError, TypeError, ValueError):
            raise ExecutionError("INVALID_ARGUMENTS") from None
        except ProviderError as exc:
            raise ExecutionError(exc.code) from None
        if hasattr(args, "start") and (args.start < self.scope.start or args.end > self.scope.end):
            raise ExecutionError("WINDOW_DENIED")
        if name == "get_service_owner" and args.alias not in {self.scope.service, *self.scope.allowed_dependencies}:
            raise ExecutionError("SERVICE_DENIED")
        if hasattr(self.provider, 'binding'):
            config = self.provider.binding.observations
            if name == 'get_service_logs':
                if not config.get('loki'):
                    raise ExecutionError('SOURCE_NOT_CONFIGURED')
                if args.category not in config.get('log_queries', {}):
                    raise ExecutionError('LOG_CATEGORY_NOT_REGISTERED')
            if name == 'get_service_metrics':
                if not config.get('prometheus'):
                    raise ExecutionError('SOURCE_NOT_CONFIGURED')
                if any(metric not in config.get('metrics', {}) for metric in args.metrics):
                    raise ExecutionError('METRIC_NOT_REGISTERED')
        return args

    def metric_point_limit(self, args):
        # Plan sampling before the read, counting full snapshots and REF catalogs rather
        # than only compact model messages. Keep the existing 12,000-character boundary.
        config = self.provider.binding.observations['metrics']
        cost = 0
        for metric in args.metrics:
            definition = config[metric]
            row = {'id': '0' * 24, 'timestamp': args.end.isoformat(),
                'service': self.scope.service, 'environment': self.scope.environment,
                'data_version': self.provider.binding.version, 'metric': metric,
                'value': -1.7976931348623157e308, 'unit': definition['unit'],
                'aggregation': definition.get('aggregation', 'registered'),
                'granularity': '86400s', 'requested_granularity': args.granularity}
            cost += len(self.prepare_snapshot('get_service_metrics', row).model_dump_json()) + 1
        return max(1, min(60, (12000 - 256) // cost))

    def execute(self, name, raw_args):
        self.last_query_profile = {}
        try:
            args = self.validate_args(name, raw_args)
        except ExecutionError as exc:
            return self.rejected(exc.code)
        # Validated filters and query window; no credentials, identity or free text.
        self.last_query_profile = {key: value for key, value in args.model_dump(mode="json").items()
                                   if key in {"metrics", "granularity", "category", "level", "start", "end"}}
        signature = (name, args.model_dump_json())
        if signature in self.seen:
            return self.rejected("DUPLICATE_TOOL")
        self.seen.add(signature)
        for attempt in range(2):
            try:
                self.context.reserve("tool")
                with self.context.span("tool", name):
                    if name == 'get_service_metrics' and hasattr(self.provider, 'binding'):
                        rows, truncated = self.provider.query(name, args, self.scope,
                            max_metric_points=self.metric_point_limit(args))
                    else:
                        rows, truncated = self.provider.query(name, args, self.scope)
                if time.monotonic() - self.context.started >= self.context.limits.seconds - self.context.limits.reserve_seconds:
                    return self.rejected("BUDGET_EXCEEDED")
                items = []
                for row in rows:
                    # Count the serialized reference catalog too; only returned rows are registered.
                    item = self.prepare_snapshot(name, row)
                    candidate = ToolResult(status="ok", evidence=[*items, item], truncated=truncated,
                        sample_order="latest_first" if name == "get_service_metrics" else "source_order")
                    if len(candidate.model_dump_json()) > 12000:
                        truncated = True
                        break
                    truncated = truncated or row.get("content_truncated", False)
                    items.append(self.register_snapshot(item))
                result = ToolResult(status="ok" if items else "empty", evidence=items, truncated=truncated,
                    sample_order="latest_first" if name == "get_service_metrics" else "source_order")
                if name == 'get_service_logs' and not truncated:
                    if args.level == 'ERROR' and result.status == 'empty':
                        key = (args.category, args.start, args.end, args.error_code)
                        if not any(self.covers_logs(coverage, args) for coverage in self.log_coverage):
                            self.log_gaps[key] = args.model_dump(mode='json')
                    elif args.level is None:
                        self.log_coverage.append(args)
                        self.log_gaps = {k: v for k, v in self.log_gaps.items() if not (
                            args.category == k[0] and args.start <= k[1] and args.end >= k[2]
                            and (args.error_code is None or args.error_code == k[3]))}
                return result
            except ExecutionError as exc:
                return self.rejected(exc.code)
            except ProviderError as exc:
                if attempt == 0 and exc.retryable and exc.code in {"TIMEOUT", "RATE_LIMIT", "NETWORK_ERROR"}:
                    self.context.emit({"type": "retry", "provider": "incident_fixture", "name": name})
                    continue
                return self.rejected(exc.code)
            except Exception:
                return self.rejected("PROVIDER_UNAVAILABLE")
        return self.rejected("PROVIDER_UNAVAILABLE")

    @staticmethod
    def covers_logs(coverage, query):
        return (coverage.category == query.category and coverage.start <= query.start
                and coverage.end >= query.end
                and (coverage.error_code is None or coverage.error_code == query.error_code))

    def prepare_snapshot(self, name, row):
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        locator = f"{name}/{row['id']}"
        eid = "EV_" + hashlib.sha256(f"{self.provider.name}/{locator}/{digest}".encode()).hexdigest()[:24]
        if eid in self.evidence:
            return self.evidence[eid].model_copy(deep=True)
        observed = datetime.fromisoformat(row["timestamp"])
        paths = reference_field_paths(row)
        return Evidence(evidence_id=eid,
                kind="runbook" if name == "search_runbooks" else "past_incident" if name == "search_incidents" else "observation",
                provider=self.provider.name, service=row["service"], environment=row["environment"],
                observed_from=observed, observed_to=observed, retrieved_at=datetime.now(timezone.utc),
                payload=row, excerpt=encoded, locator=locator, data_version=row["data_version"], hash=digest,
                allowed_field_paths=paths, reference_options=[ReferenceOption(
                    reference_id="REF_" + hashlib.sha256(f"{eid}/{path}".encode()).hexdigest()[:24],
                    field_path=path) for path in paths])

    def register_snapshot(self, item):
        if item.evidence_id not in self.evidence:
            self.evidence[item.evidence_id] = item.model_copy(deep=True)
            for option in item.reference_options:
                value = item.payload
                for part in option.field_path.split("."):
                    value = value[part]
                # Extract an actual serialized field fragment, preserving escaping and JSON types.
                quote = json.dumps({option.field_path.split(".")[-1]: value}, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))[1:-1][:1000]
                if quote not in item.excerpt:
                    raise ValueError("invalid server reference fragment")
                self.references[option.reference_id] = EvidenceRef(evidence_id=item.evidence_id,
                    field_path=option.field_path, value=value, unit=item.payload.get("unit", ""),
                    observed_at=item.observed_from, data_version=item.data_version, quote=quote)
        # Preserve the initial snapshot even if a caller mutates its returned nested payload.
        return self.evidence[item.evidence_id].model_copy(deep=True)
