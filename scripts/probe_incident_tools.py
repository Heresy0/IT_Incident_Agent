"""Free protocol/counter probe; never makes network requests."""
import json
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mult_agents.harness.runtime import Limits, RunContext, activate, invoke_chat_model


class ProbeModel:
    def invoke(self, messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="probe complete")
        return AIMessage(content="", tool_calls=[{"name": "probe_read", "args": {}, "id": "probe-1", "type": "tool_call"}])


def probe():
    from langchain_community.chat_models import ChatTongyi
    from pydantic import BaseModel
    class ProbeArgs(BaseModel):
        pass
    # Binding is local serialization, no provider request.
    bound = ChatTongyi(model="qwen-turbo", dashscope_api_key="test-only", max_retries=1,
                      model_kwargs={"request_timeout": 30}).bind_tools([ProbeArgs])
    assert len(bound.kwargs["tools"]) == 1
    events = []
    context = RunContext(limits=Limits(reserve_model_calls=3, reserve_seconds=20), emit=events.append)
    with activate(context):
        messages = [HumanMessage(content="Use the controlled read tool once.")]
        response = invoke_chat_model(ProbeModel(), messages, "investigation")
        assert response.tool_calls[0]["name"] == "probe_read"
        context.reserve("tool")
        messages.extend([response, ToolMessage(content='{"status":"ok"}', tool_call_id="probe-1")])
        invoke_chat_model(ProbeModel(), messages, "investigation")
    assert context.counts == {"model_calls": 2, "tool_calls": 1, "web_calls": 0}
    return {"mode": "fake_protocol_control_only", "counts": context.counts,
            "model_spans": sum(e["type"] == "call_start" for e in events),
            "chat_tongyi_binding": "ok", "live_verified": False}


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
