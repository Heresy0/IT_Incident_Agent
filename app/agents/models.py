"""Reuse installed ChatTongyi directly, not an outer framework Agent invoke."""
from langchain_community.chat_models import ChatTongyi
from copy import deepcopy
from config.incident import DEFAULT_MODEL
from requests.exceptions import Timeout, ConnectionError
from runtime.context import ExecutionError


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


class TextMultimodalClient:
    """Bridge DashScope multimodal text blocks to the existing text/tool protocol."""

    @staticmethod
    def call(**kwargs):
        from dashscope import MultiModalConversation
        messages = deepcopy(kwargs.pop("messages"))
        for message in messages:
            if isinstance(message.get("content"), str):
                message["content"] = [{"text": message["content"]}]
        response = deepcopy(MultiModalConversation.call(messages=messages, **kwargs))
        if response.get("status_code") == 200:
            for choice in response["output"]["choices"]:
                message = choice["message"]
                if isinstance(message.get("content"), list):
                    message["content"] = "".join(block.get("text", "") for block in message["content"])
        return response


def build_model(api_key, model=DEFAULT_MODEL):
    # Tenacity stop_after_attempt(1): one SDK attempt per harness step.
    # DashScope 1.25.1 passes request_timeout to requests. A scalar sets
    # both connection and read timeout. No dependency upgrade required.
    kwargs = {"request_timeout": 30, "max_tokens": 2200}
    multimodal = model == DEFAULT_MODEL or model.startswith(DEFAULT_MODEL + "-")
    if multimodal:
        # Keep the existing bounded, non-streaming text/tool workflow.
        kwargs["enable_thinking"] = False
        kwargs["response_format"] = {"type": "json_object"}
    chat = ChatTongyi(model=model, dashscope_api_key=api_key, temperature=0,
                     max_retries=1, model_kwargs=kwargs)
    if multimodal:
        # Installed ChatTongyi only auto-routes older qwen-vl models.
        chat.client = TextMultimodalClient
    model_chat = ClassifiedChat(chat)
    model_chat.json_output = multimodal
    return model_chat


def build_roles(api_key, model=DEFAULT_MODEL):
    # Stateless model calls; each role gets its own prompt/local message list.
    chat = build_model(api_key, model)
    chat.blind_review = True
    return {role: chat for role in ("supervisor", "investigation", "knowledge", "diagnosis", "reviewer")}
