"""IT diagnosis execution inside the shared admission and persistence lifecycle."""
from api.auth import Principal
from runtime.context import Limits
from providers.bound import ticket_for, BoundIncidentProvider


class IncidentAdapter:
    task_type = "incident"
    def __init__(self, service, model_factory=None):
        self.service, self.model_factory = service, model_factory

    def limits(self, request):
        execution = request["execution"]
        return Limits(model_calls=execution.get("model_budget") or 16, tool_calls=execution.get("tool_budget") or 24,
                      reserve_model_calls=3, reserve_seconds=20)

    def principal(self, request):
        return Principal(request["tenant_id"], request["user_id"])

    def metadata(self, execution):
        from runtime.version import implementation_version
        version = implementation_version()[:12]
        return {"model": "scripted" if execution["execution_mode"] == "scripted_control_only" else self.service._base_config.model,
                "workflow_version": "incident_graph_v1", "workflow_engine": "langgraph", "source_version": version,
                "limits": self.limits({"execution": execution}).__dict__}

    def scope(self, request):
        snapshot, principal = request['incident_snapshot'], self.principal(request)
        if not snapshot.get('demo_case_id'):
            from providers.fixtures import ProviderError
            try:
                return self.service.services.ticket(snapshot, principal).scope
            except ProviderError:
                pass
        return ticket_for(snapshot, principal).scope

    def execute(self, request, context, events):
        from workflow.coordinator import collaborate
        from evidence.render import render
        snapshot = request["incident_snapshot"]
        from providers.fixtures import ProviderError
        try:
            provider = (BoundIncidentProvider(snapshot, self.principal(request), self.service._store)
                        if snapshot.get('demo_case_id') else self.service.services.provider(snapshot, self.principal(request)))
        except ProviderError:
            result = self.failure(request, context, "OBSERVATION_PROVIDER_UNAVAILABLE")
            result.update(status="partial", business_result="needs_information",
                final="当前工单尚未绑定真实观测 Provider。没有执行模型或制造观测，请接入日志/指标来源或人工补充证据。")
            return result
        mode = request["execution"]["execution_mode"]
        if not snapshot.get('demo_case_id'):
            from providers.history import ProviderWithHistory
            provider = ProviderWithHistory(provider, self.service._store, self.principal(request))
            provider.repair_capabilities = self.service.repairs.capabilities(provider.binding)
        if mode == "scripted_control_only":
            if not snapshot.get('demo_case_id'):
                result = self.failure(request, context, 'LIVE_DIAGNOSIS_REQUIRED')
                result.update(status='partial', business_result='needs_information',
                              final='真实数据源不能使用固定案例脚本诊断；请显式选择live并提供模型和工具预算。')
                return result
            from agents.scripted import scripted_models
            models = scripted_models()
        else:
            from agents.models import build_roles
            config = self.service._base_config
            if not config.api_key or config.api_key == "test-only":
                return self.failure(request, context, "AUTH_ERROR")
            models = (self.model_factory or build_roles)(config.api_key, config.model)
        result = collaborate(models, provider, self.principal(request), context=context, event_log=events)
        result.update(execution_mode=mode, model="scripted" if mode == "scripted_control_only" else self.service._base_config.model)
        result["final"] = render(result)
        result.update(task_type="incident", workflow="collaboration", incident_snapshot=snapshot, memory_enabled=False)
        return result

    def failure(self, request, context, code):
        return {"run_id": context.run_id, "task_type": "incident", "incident_id": request["incident_snapshot"]["id"],
            "incident_snapshot": request["incident_snapshot"], "execution_mode": request["execution"]["execution_mode"],
            "status": "failed", "business_result": "needs_information", "review_status": "not_performed",
            "final": "诊断未完成，请查看运行记录并补充信息或重新诊断。", "memory_enabled": False,
            "run_summary": {**context.summary(), "termination_reason": code}, "evidence": [], "sources": []}
