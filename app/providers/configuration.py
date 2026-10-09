"""Offline registration checks. Diagnostics contain schema paths, never input values."""
from datetime import datetime
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from evidence.contracts import Metric
from providers.fixtures import ProviderError
from providers.http import validate_endpoint
from providers.logs import LogMapping
from repairs.verification import validate_checks


def nonblank(value):
    if not value.strip():
        raise ValueError('NONBLANK_REQUIRED')
    return value


Name = Annotated[str, Field(min_length=1, max_length=80), AfterValidator(nonblank)]
Query = Annotated[str, Field(min_length=1, max_length=8000), AfterValidator(nonblank)]
EnvName = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,79}$')]
TargetName = Annotated[str, Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class Endpoint(ConfigModel):
    url: Query
    authorization_env: EnvName | None = None

    @field_validator('url')
    @classmethod
    def valid_endpoint(cls, value):
        try:
            validate_endpoint(value)
        except ProviderError as exc:
            raise ValueError(exc.code) from None
        return value


class MetricDefinition(ConfigModel):
    query: Query
    unit: Name
    aggregation: Name = 'registered'
    freshness_query: Query | None = None


class Observations(ConfigModel):
    prometheus: Endpoint | None = None
    loki: Endpoint | None = None
    metrics: dict[Metric, MetricDefinition] = Field(default_factory=dict)
    log_queries: dict[Literal['resource', 'dependency', 'configuration'], Query] = Field(default_factory=dict)
    log_mapping: LogMapping = Field(default_factory=LogMapping)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    owners: list[dict[str, Any]] = Field(default_factory=list)
    runbooks: list[dict[str, Any]] = Field(default_factory=list)
    incidents: list[dict[str, Any]] = Field(default_factory=list)


class SymptomCheck(ConfigModel):
    metric: Metric
    operator: Literal['gte', 'lte', 'eq']
    threshold: float | int
    max_age_seconds: int = Field(default=60, ge=5, le=300)


class RepairTarget(ConfigModel):
    executor: Literal['docker_cli', 'docker_http']
    actions: list[Literal['restart_service']] = Field(min_length=1, max_length=1)
    container_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    context: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')] | None = None
    url: Query | None = None
    authorization_env: EnvName | None = None
    verification_attempts: int = Field(default=3, ge=1, le=6)
    verification_interval_seconds: int = Field(default=1, ge=1, le=10)
    symptom_checks: list[SymptomCheck] = Field(min_length=1, max_length=4)
    max_replicas: int = Field(default=1, ge=1, le=10)
    versions: list[Name] = Field(default_factory=list)

    @field_validator('url')
    @classmethod
    def valid_endpoint(cls, value):
        return Endpoint.valid_endpoint(value) if value is not None else value


class ServiceConfig(ConfigModel):
    tenant_id: Name
    users: list[Name] = Field(min_length=1)
    service: Name
    environment: Literal['staging', 'production']
    version: Annotated[str, Field(min_length=1, max_length=40), AfterValidator(nonblank)]
    dependencies: list[Name] = Field(default_factory=list)
    observations: Observations = Field(default_factory=Observations)
    repairs: dict[TargetName, RepairTarget] = Field(default_factory=dict)

    @model_validator(mode='after')
    def distinct_users(self):
        if len(self.users) != len(set(self.users)):
            raise ValueError('DUPLICATE_USERS')
        return self


class ServicesDocument(ConfigModel):
    services: list[Any]


MESSAGES = {
    'NONBLANK_REQUIRED': '必须填写非空文本。',
    'DUPLICATE_USERS': '允许访问的用户列表不能重复。',
    'ENDPOINT_INVALID': '必须填写HTTP(S)基础地址，不能包含内嵌凭据、路径、查询或片段。',
    'ENDPOINT_TLS_REQUIRED': '非本机地址必须使用HTTPS。',
    'missing': '必填字段缺失。', 'extra_forbidden': '包含不支持的字段，请按登记模板填写。',
    'literal_error': '枚举值不受支持，请使用当前登记模板中的选项。',
    'string_pattern_mismatch': '字段格式不符合登记或工具范围规则。',
}
SCHEMA_FIELDS = set().union(*(set(model.model_fields) for model in (
    Endpoint, MetricDefinition, Observations, SymptomCheck, RepairTarget, ServiceConfig, ServicesDocument, LogMapping)))


def issue(path, code, message, level='error'):
    return {'path': path, 'code': code, 'message': message, 'level': level}


class ServiceConfigurationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__('服务登记配置无效；配置值不会输出。\n' + '\n'.join(
            f"- {item['path']}: {item['message']}" for item in issues))


def read_configuration(path):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('DUPLICATE_JSON_FIELD')
            result[key] = value
        return result
    def reject_constant(_):
        raise ValueError('NONFINITE_JSON_NUMBER')
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ServiceConfigurationError([issue('$', 'CONFIG_TOO_LARGE', '配置文件不得超过1MB。')])
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=reject_constant)
    except ServiceConfigurationError:
        raise
    except OSError:
        raise ServiceConfigurationError([issue('$', 'CONFIG_UNREADABLE', '配置文件不存在或不可读取。')]) from None
    except (ValueError, UnicodeError, RecursionError):
        raise ServiceConfigurationError([issue('$', 'CONFIG_JSON_INVALID',
            '须使用UTF-8 JSON；检查语法、重复字段或非有限数值，原文不会输出。')]) from None


def safe_location(location, data):
    """Use ordinal positions for dictionary keys, so misplaced secrets cannot leak."""
    path, current, dynamic = '$', data, False
    for part in location:
        if part == '[key]':
            path += '.key'
            continue
        if isinstance(part, int):
            path += f'[{part}]'
            current = current[part] if isinstance(current, list) and part < len(current) else None
        else:
            if dynamic or part not in SCHEMA_FIELDS:
                index = list(current).index(part) if isinstance(current, dict) and part in current else 0
                path += f'[{index}]' if dynamic else f'.extra_fields[{index}]'
            else:
                path += '.' + part
            current = current.get(part) if isinstance(current, dict) else None
        dynamic = part in {'metrics', 'repairs', 'log_queries'}
    return path


def schema_issues(error, data, prefix=()):
    results = []
    for item in error.errors(include_input=False, include_url=False):
        code = item['type']
        custom = str(item.get('ctx', {}).get('error', ''))
        if custom in MESSAGES:
            code = custom
        results.append(issue(safe_location((*prefix, *item['loc']), data), code,
            MESSAGES.get(code, '字段类型、范围或配置规则无效，请按模板填写。')))
    return results


def validate_configuration(data):
    try:
        document = ServicesDocument.model_validate(data)
    except ValidationError as exc:
        return schema_issues(exc, data)
    results, seen = [], set()
    if not document.services:
        results.append(issue('$.services', 'NO_SERVICES', '没有登记服务，真实服务列表将为空。', 'warning'))
    for index, raw in enumerate(document.services):
        base = f'$.services[{index}]'
        try:
            row = ServiceConfig.model_validate(raw)
        except ValidationError as exc:
            results.extend(schema_issues(exc, data, ('services', index)))
            continue
        key = (row.tenant_id, row.service, row.environment)
        if key in seen:
            results.append(issue(base, 'DUPLICATE_SERVICE', '同一租户、服务和环境只能登记一次。'))
        seen.add(key)
        observations = row.observations
        for source, definitions in (('prometheus', observations.metrics), ('loki', observations.log_queries)):
            if definitions and getattr(observations, source) is None:
                results.append(issue(base + '.observations.' + source, 'SOURCE_REQUIRED', '登记了查询但没有对应观测地址。'))
        for name in ('metrics', 'log_queries', 'owners', 'runbooks'):
            if not getattr(observations, name):
                results.append(issue(base + '.observations.' + name, 'CHANNEL_EMPTY',
                    '该渠道未登记内容；取证或检索可能返回信息缺口。', 'warning'))
        for name in ('changes', 'owners', 'runbooks', 'incidents'):
            for number, record in enumerate(getattr(observations, name)):
                path = base + f'.observations.{name}[{number}]'
                try:
                    stamp = datetime.fromisoformat(record['timestamp'])
                    if stamp.tzinfo is None:
                        raise ValueError()
                except (KeyError, ValueError, TypeError):
                    results.append(issue(path + '.timestamp', 'TIMESTAMP_REQUIRED', '须提供带时区的ISO时间文本。'))
                required = {'owners': ('team',), 'changes': ('summary',),
                            'runbooks': ('text',), 'incidents': ('text', 'resolution')}.get(name, ())
                for field in required:
                    if not isinstance(record.get(field), str) or not record[field].strip():
                        results.append(issue(path + '.' + field, 'TEXT_REQUIRED', '须填写非空文本。'))
                for field in ({'owners': ('aliases',), 'runbooks': ('versions',), 'incidents': ('versions',)}.get(name, ())):
                    value = record.get(field)
                    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v.strip() for v in value):
                        results.append(issue(path + '.' + field, 'LIST_REQUIRED', '须填写非空文本列表。'))
        for number, (target_name, target) in enumerate(row.repairs.items()):
            path = base + f'.repairs[{number}]'
            if target.executor == 'docker_cli' and target.context is None:
                results.append(issue(path + '.context', 'DOCKER_CONTEXT_REQUIRED', 'CLI执行器须登记固定Docker context。'))
            if target.executor == 'docker_http' and target.url is None:
                results.append(issue(path + '.url', 'DOCKER_URL_REQUIRED', 'HTTP执行器须登记固定Docker基础地址。'))
            if target.container_id == '0' * 64:
                results.append(issue(path + '.container_id', 'TARGET_PLACEHOLDER', '容器标识仍为模板占位值，真实接入前须替换。', 'warning'))
            for check_index, check in enumerate(target.symptom_checks):
                definition = observations.metrics.get(check.metric)
                if definition is None:
                    results.append(issue(path + f'.symptom_checks[{check_index}].metric', 'METRIC_NOT_REGISTERED',
                        '症状检查引用了未登记指标。'))
                elif not definition.freshness_query:
                    metric_index = list(observations.metrics).index(check.metric)
                    results.append(issue(base + f'.observations.metrics[{metric_index}].freshness_query',
                        'SYMPTOM_FRESHNESS_QUERY_REQUIRED', '用于修复验证的指标须登记新鲜度查询。'))
            from providers.registry import ServiceBinding
            binding = ServiceBinding(row.tenant_id, tuple(row.users), row.service, row.environment, row.version,
                observations=observations.model_dump(exclude_none=True))
            try:
                validate_checks(binding, target.model_dump(exclude_none=True))
            except ProviderError as exc:
                # Reuse the executor's checks, including duplicate metrics and source requirements.
                if not any(r['level'] == 'error' and (r['path'].startswith(path)
                    or r['path'].startswith(base + '.observations.metrics') and r['path'].endswith('.freshness_query')) for r in results):
                    results.append(issue(path + '.symptom_checks', exc.code,
                        '症状核验规则无效，请检查重复指标及对应Prometheus来源。'))
    return results
