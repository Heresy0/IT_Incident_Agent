"""IT diagnosis execution inside the shared admission and persistence lifecycle."""
from api.auth import Principal
from runtime.context import Limits
from providers.bound import ticket_for, BoundIncidentProvider
from dataclasses import replace
import hashlib
import json


class IncidentAdapter:
    task_type = "incident"
    def __init__(self, service, model_factory=None):
        self.service, self.model_factory = service, model_factory

    def limits(self, request):
        frozen = request.get('run_spec', {}).get('limits')
        if frozen:
            return Limits(**frozen)
        execution = request["execution"]
        base = Limits.from_env()
        return replace(base, model_calls=min(execution.get("model_budget") or 16, base.model_calls),
                       tool_calls=min(execution.get("tool_budget") or 24, base.tool_calls),
                       reserve_model_calls=3, reserve_seconds=min(20, base.seconds / 2))

    def principal(self, request):
        return Principal(request["tenant_id"], request["user_id"])

    def metadata(self, execution, *, incident=None, principal=None, registry=None, config=None):
        from runtime.version import implementation_version
        full_version = implementation_version()
        version = full_version[:12]
        engine = 'autonomous_v2'
        config = config or (self.service._base_config if self.service else None)
        settings = {'model': getattr(config, 'model', 'unknown'), 'base_url': getattr(config, 'base_url', ''),
                    'extra_body': getattr(config, 'extra_body', {}), 'temperature': 0, 'max_tokens': 2200}
        model_settings_sha256 = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        limits = self.limits({'execution': execution}).__dict__
        from workflow.v2.review import REVIEW_POLICY_VERSION
        from workflow.rollout import configuration_fingerprint
        rollout = getattr(self.service, 'incident_rollout', None)
        variant = rollout.variant if rollout else 'closed_loop'
        challenge = variant == 'closed_loop' and (rollout.enable_challenge if rollout else True)
        configuration_sha256 = configuration_fingerprint(model_settings_sha256, variant, challenge, limits,
                                                         execution_mode=execution['execution_mode'])
        if rollout is not None:
            decision = rollout.select(incident, principal, registry or self.service.services,
                source_version=full_version, review_policy_version=REVIEW_POLICY_VERSION,
                configuration_sha256=configuration_sha256)
            engine = decision['engine_id']
        else:
            decision = {'execution_variant': 'closed_loop',
                        'enable_rework': True,
                        'enable_challenge': True,
                        'rollout': {'policy_version': 'v2_default',
                                    'mode': 'default_closed_loop',
                                    'selected_engine': engine, 'acceptance': {'verdict': 'hold'},
                                    'shadow': {'requested': False, 'mode': 'offline_snapshot_only',
                                               'production_executed': False, 'production_dual_reads': False,
                                               'can_authorize_repair': False}}}
        from workflow.v2.engine import ENGINE_VERSION, PROMPT_VERSION
        engine_version, prompt_version = ENGINE_VERSION, PROMPT_VERSION
        run_spec = {'engine_id': engine, 'engine_version': engine_version,
                    'contract_version': 'atomic_draft_v1',
                    'prompt_version': prompt_version,
                    'capability_version': 'generic_capabilities_v1', 'source_version': version,
                    'source_version_full': full_version,
                    'model_settings_sha256': model_settings_sha256, 'configuration_sha256': configuration_sha256,
                    'limits': limits, **{key: decision[key] for key in (
                        'execution_variant', 'enable_rework', 'enable_challenge', 'rollout')}}
        if engine == 'autonomous_v2':
            run_spec['review_policy_version'] = REVIEW_POLICY_VERSION
            run_spec['challenge_policy_version'] = ('blind_scope_challenge_v1'
                if decision['enable_challenge'] else 'not_enabled')
        if getattr(self.service, 'checkpoint_recovery', False):
            from storage.recovery import SCHEMA_VERSION
            from importlib.metadata import version as distribution_version
            run_spec['recovery'] = {'mode': 'postgres_v1', 'schema_version': SCHEMA_VERSION,
                'unknown_response': 'stop_without_resend', 'deadline': 'wall_clock',
                'dependencies': {name: distribution_version(name) for name in (
                    'langgraph', 'langgraph-checkpoint', 'langgraph-checkpoint-postgres')}}
            if incident and incident.get('demo_case_id'):
                from providers.fixtures import FixtureProvider
                from storage.recovery import digest
                run_spec['recovery']['fixture_sha256'] = digest(FixtureProvider(incident['demo_case_id'], principal)._data)
        if incident and not incident.get('demo_case_id'):
            from providers.fixtures import ProviderError
            try:
                binding = (registry or self.service.services).resolve(incident, principal)
                run_spec['provider_binding_sha256'] = binding.fingerprint
            except ProviderError:
                run_spec['provider_binding_sha256'] = 'unavailable'
        return {"model": "scripted" if execution["execution_mode"] == "scripted_control_only" else settings['model'],
                "workflow_version": run_spec['engine_version'],
                "workflow_engine": "autonomous_loop", "source_version": version,
                "limits": limits, 'run_spec': run_spec}

    def scope(self, request):
        snapshot, principal = request['incident_snapshot'], self.principal(request)
        registry = request.get('_provider_registry', self.service.services)
        if not snapshot.get('demo_case_id'):
            from providers.fixtures import ProviderError
            try:
                return registry.ticket(snapshot, principal).scope
            except ProviderError:
                pass
        return ticket_for(snapshot, principal).scope

    def execute(self, request, context, events):
        snapshot = request["incident_snapshot"]
        engine = request.get('run_spec', {}).get('engine_id', 'autonomous_v2')
        if engine != 'autonomous_v2':
            return self.failure(request, context, 'ENGINE_VERSION_UNAVAILABLE')
        expected_source = request.get('run_spec', {}).get('source_version_full')
        if expected_source:
            from runtime.version import implementation_version
            if implementation_version() != expected_source:
                return self.failure(request, context, 'ENGINE_SOURCE_CHANGED')
        registry = request.get('_provider_registry', self.service.services)
        from providers.fixtures import ProviderError
        expected_binding = request.get('run_spec', {}).get('provider_binding_sha256')
        if expected_binding and not snapshot.get('demo_case_id'):
            try:
                actual_binding = registry.resolve(snapshot, self.principal(request)).fingerprint
            except ProviderError:
                actual_binding = 'unavailable'
            if actual_binding != expected_binding:
                return self.failure(request, context, 'PROVIDER_BINDING_CHANGED')
        try:
            provider = (BoundIncidentProvider(snapshot, self.principal(request), self.service._store)
                        if snapshot.get('demo_case_id') else registry.provider(snapshot, self.principal(request)))
        except ProviderError:
            return self.failure(request, context, "OBSERVATION_PROVIDER_UNAVAILABLE", status='partial',
                final=("当前工单尚未绑定真实观测 Provider。没有执行模型或制造观测，请接入日志/指标来源后重新诊断；本轮提交内容已保留。"
                    if request.get('followup_context') else
                    "当前工单尚未绑定真实观测 Provider。没有执行模型或制造观测，请接入日志/指标来源或人工补充证据。"))
        mode = request["execution"]["execution_mode"]
        if not snapshot.get('demo_case_id'):
            from providers.history import ProviderWithHistory
            provider = ProviderWithHistory(provider, self.service._store, self.principal(request))
            provider.repair_capabilities = self.service.repairs.capabilities(provider.binding)
        if request.get('followup_context'):
            provider.followup_context = request['followup_context']
        if mode == "scripted_control_only":
            if not snapshot.get('demo_case_id'):
                return self.failure(request, context, 'LIVE_DIAGNOSIS_REQUIRED', status='partial',
                    final='真实数据源不能使用固定案例脚本诊断；请显式选择live并提供模型和工具预算。')
            from workflow.v2.scripted import scripted_model
            models = scripted_model(provider)
        else:
            from agents.models import build_roles
            config = request.get('_model_config', self.service._base_config)
            if not config.api_key or config.api_key == "test-only":
                return self.failure(request, context, "AUTH_ERROR")
            models = (self.model_factory(config.api_key, config.model) if self.model_factory else
                      build_roles(config.api_key, config.model, base_url=config.base_url, extra_body=config.extra_body))
        from workflow.v2.engine import investigate
        result = investigate(models, provider, self.principal(request), context=context, event_log=events,
                             enable_rework=bool(request.get('run_spec', {}).get('enable_rework', False)),
                             enable_challenge=bool(request.get('run_spec', {}).get('enable_challenge', False)))
        effective_config = request.get('_model_config', self.service._base_config)
        result.update(execution_mode=mode, model="scripted" if mode == "scripted_control_only" else effective_config.model,
                      engine_id=engine, run_spec=request.get('run_spec', {}))
        result.update(task_type="incident", workflow="autonomous_v2", incident_id=snapshot['id'],
                      incident_snapshot=snapshot, memory_enabled=False)
        return self.followup_result(request, result)

    def followup_result(self, request, result):
        followup = request.get('followup_context')
        if not followup or result.get('followup') == followup:
            return result
        from copy import deepcopy
        result = {**result, 'followup': deepcopy(followup)}
        if any(submission['observations'] for submission in followup['submissions']):
            result['repair_eligible'] = False
        if 'output' in result:
            from workflow.v2.engine import render_report
            result['final'] = render_report(result)
        else:
            from incidents.followups import report_lines
            result['final'] += '\n' + '\n'.join(report_lines(followup, result['review_status'])) + '\n'
        return result

    def resume_error(self, request):
        """Validate a persisted run without rebuilding a client or calling a model."""
        from runtime.version import implementation_version
        from workflow.v2.review import REVIEW_POLICY_VERSION
        from storage.recovery import SCHEMA_VERSION
        from providers.fixtures import ProviderError
        from importlib.metadata import version as distribution_version
        spec = request['run_spec']
        if spec.get('source_version_full') != implementation_version():
            return 'ENGINE_SOURCE_CHANGED'
        if spec.get('review_policy_version') != REVIEW_POLICY_VERSION or spec['recovery']['schema_version'] != SCHEMA_VERSION:
            return 'RECOVERY_POLICY_CHANGED'
        dependencies = {name: distribution_version(name) for name in (
            'langgraph', 'langgraph-checkpoint', 'langgraph-checkpoint-postgres')}
        if spec['recovery'].get('dependencies') != dependencies:
            return 'RECOVERY_DEPENDENCIES_CHANGED'
        config = request.get('_model_config', self.service._base_config)
        settings = {'model': config.model, 'base_url': getattr(config, 'base_url', ''),
                    'extra_body': getattr(config, 'extra_body', {}), 'temperature': 0, 'max_tokens': 2200}
        current = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        if current != spec['model_settings_sha256']:
            return 'MODEL_SETTINGS_CHANGED'
        snapshot, principal = request['incident_snapshot'], self.principal(request)
        registry = request.get('_provider_registry', self.service.services)
        try:
            if snapshot.get('demo_case_id'):
                provider = BoundIncidentProvider(snapshot, principal, self.service._store)
                provider.authorize(principal, provider.ticket().scope)
                from storage.recovery import digest
                if digest(provider.base._data) != spec['recovery'].get('fixture_sha256'):
                    return 'RECOVERY_FIXTURE_CHANGED'
            else:
                binding = registry.resolve(snapshot, principal)
                if binding.fingerprint != spec.get('provider_binding_sha256'):
                    return 'PROVIDER_BINDING_CHANGED'
        except ProviderError:
            return 'RECOVERY_AUTHORIZATION_CHANGED'
        return None

    def failure(self, request, context, code, *, status='failed', final=None):
        result = {"run_id": context.run_id, "task_type": "incident", "incident_id": request["incident_snapshot"]["id"],
            "incident_snapshot": request["incident_snapshot"], "execution_mode": request["execution"]["execution_mode"],
            "status": status, "business_result": "needs_information", "review_status": "not_performed",
            "engine_id": request.get('run_spec', {}).get('engine_id', 'autonomous_v2'), "repair_eligible": False,
            'run_spec': request.get('run_spec', {}),
            "final": final or "诊断未完成，请查看运行记录并补充信息或重新诊断。", "memory_enabled": False,
            "run_summary": {**context.summary(), "termination_reason": code}, "evidence": [], "sources": []}
        return self.followup_result(request, result)
