"""SCRIPTED control demonstration only. Not a model or autonomy evaluation."""
import json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class ScriptedModel:
    def __init__(self):
        self.calls = 0
        self.history = []

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.calls += 1
        self.history.append(list(messages))
        ticket = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))["ticket"]
        scope = ticket["scope"]
        window = {"start": scope["start"], "end": scope["end"]}
        symptoms = ticket["symptoms"].casefold()
        if self.calls == 1:
            if "audience" in symptoms:
                name, args = "get_recent_changes", {**window, "category": "deployment"}
            elif "waiting" in symptoms:
                name, args = "get_service_metrics", {**window, "metrics": ["pool_usage", "pool_wait", "db_cpu"]}
            else:
                name, args = "get_service_logs", {**window, "category": "dependency", "level": "ERROR"}
        elif self.calls == 2:
            if "audience" in symptoms:
                name, args = "get_service_logs", {**window, "category": "configuration", "error_code": "AUTH_AUDIENCE_MISMATCH"}
            elif "waiting" in symptoms:
                name, args = "get_service_logs", {**window, "category": "resource", "level": "WARN"}
            else:
                name, args = "get_service_metrics", {**window, "metrics": ["dependency_error_rate", "request_latency"]}
        else:
            evidence = [e for m in messages if isinstance(m, ToolMessage) for e in json.loads(m.content)["evidence"]]
            findings = []
            for e in evidence[:6]:
                row = e["payload"]
                field = "value" if "value" in row else "message" if "message" in row else "summary"
                reference = next(o for o in e["reference_options"] if o["field_path"] == field)
                findings.append({"statement": f"Observed {row.get('metric', row['id'])}: {row[field]}",
                    "refs": [{"reference_id": reference["reference_id"]}]})
            return AIMessage(content=json.dumps({"findings": findings, "tentative_hypotheses": [],
                "missing_information": ["Scripted control output; real model investigation and independent review remain unverified."],
                "escalation_ref": None}))
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"fake-{self.calls}", "type": "tool_call"}])
