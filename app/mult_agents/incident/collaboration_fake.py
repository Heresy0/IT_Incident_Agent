"""Scripted collaboration control cases. No model autonomy/quality claims and no gold reads."""
import json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def selector(evidence, field):
    option = next(o for o in evidence["reference_options"] if o["field_path"] == field)
    return {"reference_id": option["reference_id"]}


def latest_metrics(evidence):
    return {e["payload"]["metric"]: e for e in sorted(evidence, key=lambda e: e["observed_at"])
            if "metric" in e["payload"]}


class ScriptedRole:
    def __init__(self, role):
        self.role = role
        self.histories = []

    def bind_tools(self, tools):
        model = ScriptedRole(self.role)
        model.tools = tools
        # Histories are instrumentation only, never read as conversation context.
        model.histories = self.histories
        return model

    def invoke(self, messages):
        self.histories.append(list(messages))
        data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        if self.role in {"investigation", "knowledge"}:
            return self.observe(messages, data)
        payload = data["input"]
        if self.role == "supervisor":
            challenge = payload["pending_challenge"]
            if challenge:
                result = {"action": "dispatch", "reason": "补查有区分价值的当前观测", "tasks": [{
                    "role": challenge["target_role"], "goal": challenge["proposed_check"], "evidence_ids": challenge["evidence_ids"]}]}
            elif not payload["tasks"]:
                tasks = [{"role": "investigation", "goal": "取得与症状有关的当前观测"}]
                if "waiting" in payload["ticket"]["symptoms"].casefold():
                    tasks.append({"role": "knowledge", "goal": "检索适用手册及相似的已确认历史线索"})
                result = {"action": "dispatch", "reason": "根据症状选择必要角色", "tasks": tasks}
            else:
                result = {"action": "diagnose", "reason": "已完成初始取证，交由诊断和复核"}
        elif self.role == "diagnosis":
            result = self.diagnose(payload)
        else:
            result = self.review(payload)
        return AIMessage(content=json.dumps(result, ensure_ascii=False))

    def observe(self, messages, data):
        scope = data["ticket"]["scope"]
        window = {"start": scope["start"], "end": scope["end"]}
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        if not results:
            objective = data.get("objective") or {}
            symptoms = data["ticket"]["symptoms"].casefold()
            if self.role == "knowledge":
                queries = [("search_runbooks", {"query": "pool waiting latency database", "category": "resource"}),
                           ("search_incidents", {"symptoms": "latency database pool"})]
            elif objective.get("challenge"):
                queries = [("get_service_metrics", {**window, "metrics": ["pool_usage", "pool_wait", "db_cpu"]}),
                           ("get_service_logs", {**window, "category": "resource"})]
            elif "audience" in symptoms:
                queries = [("get_recent_changes", window), ("get_service_logs", {**window, "category": "configuration"})]
            elif "waiting" in symptoms:
                queries = [("get_service_metrics", {**window, "metrics": ["db_cpu"]})]
            else:
                queries = [("get_service_logs", {**window, "category": "dependency"}),
                           ("get_service_metrics", {**window, "metrics": ["dependency_error_rate"]})]
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"control-{n}", "type": "tool_call"}
                for n, (name, args) in enumerate(queries)])
        evidence = [e for result in results for e in result["evidence"]]
        findings = []
        for e in evidence[:8]:
            field = "text" if self.role == "knowledge" else "value" if "value" in e["payload"] else "message" if "message" in e["payload"] else "summary"
            findings.append({"statement": f"来源记录：{e['payload'][field]}", "refs": [selector(e, field)]})
        return AIMessage(content=json.dumps({"findings": findings, "tentative_hypotheses": [],
            "missing_information": []}, ensure_ascii=False))

    def diagnose(self, payload):
        evidence = payload["evidence"]
        metrics = latest_metrics(evidence)
        errors = {e["payload"]["error_code"]: e for e in evidence if "error_code" in e["payload"]}
        history = next((e for e in evidence if e["kind"] == "past_incident"), None)
        if "pool_usage" in metrics:
            pool, wait, db = metrics["pool_usage"], metrics["pool_wait"], metrics["db_cpu"]
            facts = [{"statement": f"应用池使用率 {pool['payload']['value']}，等待 {wait['payload']['value']}ms，DB CPU {db['payload']['value']}%。",
                      "refs": [selector(pool, "value"), selector(wait, "value"), selector(db, "value")]}]
            hypothesis = {"cause": "应用连接池在当前负载下耗尽", "support_refs": [selector(pool, "value"), selector(wait, "value"),
                selector(errors["POOL_ACQUIRE_TIMEOUT"], "message")], "counter_refs": [selector(db, "value")]}
        elif history:
            db = metrics["db_cpu"]
            facts = [{"statement": f"当前 DB CPU 为 {db['payload']['value']}%。", "refs": [selector(db, "value")]}]
            # Deliberately weak initial draft for the review-driven revision control case.
            hypothesis = {"cause": "数据库 CPU 过高导致延迟", "support_refs": [selector(history, "text")],
                          "counter_refs": [selector(db, "value")]}
        elif "AUTH_AUDIENCE_MISMATCH" in errors:
            log = errors["AUTH_AUDIENCE_MISMATCH"]
            change = next(e for e in evidence if "config_summary" in e["payload"])
            facts = [{"statement": log["payload"]["message"], "refs": [selector(log, "message"), selector(change, "config_summary.auth_audience")]}]
            hypothesis = {"cause": "发布配置的 token audience 与依赖期望不一致", "support_refs": facts[0]["refs"]}
        else:
            log = errors["UPSTREAM_UNREACHABLE"]
            metric = metrics["dependency_error_rate"]
            facts = [{"statement": log["payload"]["message"], "refs": [selector(log, "message"), selector(metric, "value")]}]
            hypothesis = {"cause": "inventory 依赖不可达", "support_refs": facts[0]["refs"]}
        return {"findings": facts, "hypotheses": [hypothesis], "recommended_actions": [{
            "action": "由值班人员核查假设并选择处置", "condition": "确认观测与版本适用且完成独立复核",
            "expected_result": "处置后错误率和等待下降", "risk": "改配置或容量可能影响服务，需要人工批准",
            "requires_approval": True}], "missing_information": []}

    def review(self, payload):
        targets, evidence = payload["targets"], payload["evidence"]
        metrics = latest_metrics(evidence)
        weak = any(t.get("cause") == "数据库 CPU 过高导致延迟" for t in targets.values())
        ids = [e["evidence_id"] for e in evidence if e["kind"] == "observation"][-4:]
        assessments = [{"target_id": key, "verdict": "not_supported" if weak and key.startswith("H") else "supported",
            "reason": "历史线索与当前正常 DB CPU 矛盾" if weak and key.startswith("H") else "已核对当前来源与建议适用条件",
            "evidence_ids": ids} for key in targets]
        result = {"assessments": assessments}
        if weak:
            db = metrics["db_cpu"]
            result["request_evidence"] = {"hypothesis_id": "H1", "evidence_ids": [db["evidence_id"]],
                "missing_observation": "缺少当前应用连接池使用率、等待和获取连接日志",
                "proposed_check": "检查 pool_usage、pool_wait、db_cpu，并查询 resource 日志，保留 WARN/INFO",
                "expected_value": "区分数据库负载异常与应用池耗尽", "target_role": "investigation"}
        return result


def scripted_models():
    return {role: ScriptedRole(role) for role in ("supervisor", "investigation", "knowledge", "diagnosis", "reviewer")}
