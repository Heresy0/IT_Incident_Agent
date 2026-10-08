"""Only the two workflows needed here; shared admission, workers and persistence."""
from backend.auth import Principal
from mult_agents.harness.runtime import Limits
from pathlib import Path
import hashlib
from .incident_provider import ticket_for, BoundIncidentProvider


class ResearchAdapter:
    task_type = "research"
    def __init__(self, service):
        self.service = service

    def limits(self, request):
        return self.service._limits

    def scope(self, request):
        return request["tenant_id"], request["user_id"]

    def execute(self, request, context, events):
        return self.service._research_result(request, context)

    def failure(self, request, context, code):
        return {"run_id": context.run_id, **{k: request[k] for k in ("query", "tenant_id", "user_id", "thread_id")},
                "status": "failed", "final": "研究执行失败，请根据运行 ID 查看记录。", "sources": [], "run_summary": {**context.summary(), "termination_reason": code}}


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
        from mult_agents.incident import collaboration
        root = Path(collaboration.__file__).parent
        version = hashlib.sha256(b"".join((root / name).read_bytes() for name in (
            "collaboration.py", "collaboration_contracts.py", "collaboration_prompts.py", "diagnosis.py", "observations.py", "model_view.py", "ownership.py", "investigation.py", "contracts.py", "prompts.py", "tools.py"))).hexdigest()[:12]
        return {"model": "scripted" if execution["execution_mode"] == "scripted_control_only" else self.service._base_config.model,
                "workflow_version": "incident_m4_v1", "source_version": version,
                "limits": self.limits({"execution": execution}).__dict__}

    def scope(self, request):
        return ticket_for(request["incident_snapshot"], self.principal(request)).scope

    def execute(self, request, context, events):
        from mult_agents.incident.collaboration import collaborate
        from mult_agents.incident.render import render
        snapshot = request["incident_snapshot"]
        if not snapshot.get("demo_case_id"):
            result = self.failure(request, context, "OBSERVATION_PROVIDER_UNAVAILABLE")
            result.update(status="partial", business_result="needs_information",
                final="当前工单尚未绑定真实观测 Provider。没有执行模型或制造观测，请接入日志/指标来源或人工补充证据。")
            return result
        provider = BoundIncidentProvider(snapshot, self.principal(request), self.service._store)
        mode = request["execution"]["execution_mode"]
        if mode == "scripted_control_only":
            from mult_agents.incident.collaboration_fake import scripted_models
            models = scripted_models()
        else:
            from mult_agents.incident.agents import build_roles
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
