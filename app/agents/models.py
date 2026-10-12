"""OpenAI-compatible Chat Completions through LangChain; no vendor SDK routing."""
from langchain_openai import ChatOpenAI
from openai import APITimeoutError, APIConnectionError, AuthenticationError, PermissionDeniedError, RateLimitError, APIStatusError
from config.incident import DEFAULT_MODEL, IncidentConfig
from runtime.context import ExecutionError


class ClassifiedChat:
    def __init__(self, chat):
        self.chat = chat
        self.json_output = True

    def bind_tools(self, tools):
        return ClassifiedChat(self.chat.bind_tools(tools))

    def invoke(self, messages):
        try:
            return self.chat.invoke(messages)
        except (APITimeoutError, TimeoutError):
            raise ExecutionError('TIMEOUT', 'model') from None
        except APIConnectionError:
            raise ExecutionError('NETWORK_ERROR', 'model') from None
        except (AuthenticationError, PermissionDeniedError):
            raise ExecutionError('AUTH_ERROR', 'model') from None
        except RateLimitError as exc:
            code = str(getattr(exc, 'code', '') or '').lower()
            reason = 'QUOTA_EXCEEDED' if code in {'insufficient_quota', 'quota_exceeded'} else 'RATE_LIMIT'
            raise ExecutionError(reason, 'model') from None
        except APIStatusError as exc:
            code = str(getattr(exc, 'code', '') or '').lower()
            reason = 'QUOTA_EXCEEDED' if code in {'arrearage', 'insufficient_quota', 'quota_exceeded'} else 'MODEL_ERROR'
            raise ExecutionError(reason, 'model') from None


def build_model(api_key, model=DEFAULT_MODEL, *, base_url=None, extra_body=None):
    config = IncidentConfig.from_file()
    chat = ChatOpenAI(model=model, api_key=api_key, base_url=base_url or config.base_url,
                     temperature=0, timeout=30, max_retries=0, max_tokens=2200,
                     extra_body={**(config.extra_body if extra_body is None else extra_body),
                                 'response_format': {'type': 'json_object'}},
                     use_responses_api=False)
    return ClassifiedChat(chat)


def build_roles(api_key, model=DEFAULT_MODEL, *, base_url=None, extra_body=None):
    chat = build_model(api_key, model, base_url=base_url, extra_body=extra_body)
    chat.blind_review = True
    return {role: chat for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}
