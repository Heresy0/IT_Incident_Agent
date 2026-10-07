"""Reuse installed ChatTongyi directly, not an outer framework Agent invoke."""
from langchain_community.chat_models import ChatTongyi
from requests.exceptions import Timeout, ConnectionError
from mult_agents.harness.runtime import ExecutionError


class ClassifiedChat:
    def __init__(self, chat):
        self.chat = chat

    def bind_tools(self, tools):
        return ClassifiedChat(self.chat.bind_tools(tools))

    def invoke(self, messages):
        try:
            return self.chat.invoke(messages)
        except (Timeout, TimeoutError):
            raise ExecutionError("TIMEOUT", "model") from None
        except ConnectionError:
            raise ExecutionError("NETWORK_ERROR", "model") from None


def build_model(api_key, model="qwen-turbo"):
    # Tenacity stop_after_attempt(1): one SDK attempt per harness step.
    # DashScope 1.25.1 passes request_timeout to requests. A scalar sets
    # both connection and read timeout. No dependency upgrade required.
    return ClassifiedChat(ChatTongyi(model=model, dashscope_api_key=api_key, temperature=0,
                     max_retries=1, model_kwargs={"request_timeout": 30, "max_tokens": 2200}))
