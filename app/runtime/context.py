import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock, RLock
from uuid import uuid4

from observability.telemetry import record_event


MESSAGES = {
    "AUTH_ERROR": "检索凭据未配置或认证失败，请检查对应 API Key。",
    "QUOTA_EXCEEDED": "外部服务账户余额或套餐额度不足，请检查对应平台的账户额度。",
    "RATE_LIMIT": "外部服务限流，有限重试后仍未成功。",
    "TIMEOUT": "外部服务请求超时。",
    "NETWORK_ERROR": "外部服务网络连接失败。",
    "PARSE_ERROR": "外部服务返回的数据格式不正确。",
    "MODEL_OUTPUT_INVALID": "模型结构化输出校验失败。",
    "MODEL_ERROR": "模型请求失败，请检查模型服务与账户状态。",
    "RAG_UNAVAILABLE": "本地知识库服务不可用。",
    "MEMORY_UNAVAILABLE": "本次记忆服务不可用，诊断将继续，但不会读写个人记忆。",
    "BUDGET_EXCEEDED": "本次诊断已达到调用或时间预算。",
    "NO_EVIDENCE": "没有取得可支持结论的证据。",
    "NO_TOOL_PROGRESS": "取证任务未执行声明的只读检查，有限纠正后仍无进展或没有足够纠正预算；已保留信息缺口。",
    "NO_NEW_QUERIES": "没有新的补搜查询，停止重复检索。",
    "INTERNAL_ERROR": "诊断执行失败，请根据运行 ID 查看日志。",
    "PROCESS_INTERRUPTED": "后端进程中断，本次诊断未完成。",
}


class ExecutionError(RuntimeError):
    def __init__(self, code, provider="workflow", retryable=False):
        self.code, self.provider, self.retryable = code, provider, retryable
        super().__init__(MESSAGES.get(code, MESSAGES["INTERNAL_ERROR"]))

    def as_dict(self):
        return {"code": self.code, "provider": self.provider, "message": str(self), "retryable": self.retryable}


@dataclass(frozen=True)
class Limits:
    model_calls: int = 16
    web_calls: int = 12
    tool_calls: int = 24
    seconds: float = 180
    reserve_model_calls: int = 2
    reserve_seconds: float = 15
    input_chars: int = 28000
    snippet_chars: int = 1800
    max_sources: int = 10

    @classmethod
    def from_env(cls):
        return cls(model_calls=max(3, min(64, int(os.getenv("INCIDENT_MAX_MODEL_CALLS", 16)))),
                   web_calls=max(1, min(48, int(os.getenv("INCIDENT_MAX_WEB_CALLS", 12)))),
                   tool_calls=max(1, min(96, int(os.getenv("INCIDENT_MAX_TOOL_CALLS", 24)))),
                   seconds=max(20, min(300, float(os.getenv("INCIDENT_MAX_SECONDS", 180)))))


_CURRENT = ContextVar("incident_runtime", default=None)
_SPAN = ContextVar("incident_parent_span", default=None)
_RUNS = {}
_RUNS_LOCK = Lock()


class RunContext:
    def __init__(self, run_id=None, limits=None, emit=None, scope=None):
        self.run_id = run_id or str(uuid4())
        self.scope = scope
        self.limits = limits or Limits.from_env()
        self.started = time.monotonic()
        self.lock = Lock()
        self.event_lock = RLock()
        self.counts = {"model_calls": 0, "web_calls": 0, "tool_calls": 0}
        self.tokens = {"input_tokens": 0, "output_tokens": 0, "known_calls": 0}
        self.errors = []
        self.blocked_providers = set()
        self.seen_queries = set()
        self.stop_reason = ""
        self.emit_callback = emit
        self.root_span = str(uuid4())

    def emit(self, event):
        event = {"run_id": self.run_id, "trace_id": self.run_id, "timestamp": time.time(), **event}
        # Persist first: clients only see committed events.
        with self.event_lock:
            if self.emit_callback:
                self.emit_callback(event)
            record_event(event)

    def claim_repair(self, state):
        """The one structural correction belongs to the entire run."""
        with self.lock:
            if state['repairs'] >= 1:
                return False
            state['repairs'] += 1
            return True

    def add_usage(self, usage):
        with self.lock:
            for key in ('input_tokens', 'output_tokens'):
                self.tokens[key] += int(usage[key])
            self.tokens['known_calls'] += 1

    def reserve(self, kind, terminal=False):
        with self.lock:
            elapsed = time.monotonic() - self.started
            deadline = self.limits.seconds - (0 if terminal else self.limits.reserve_seconds)
            key = "model_calls" if kind == "model" else "tool_calls"
            cap = self.limits.model_calls if kind == "model" else self.limits.tool_calls
            if kind == "model" and not terminal:
                cap -= self.limits.reserve_model_calls
            if elapsed >= deadline or self.counts[key] >= cap or (kind == "web" and self.counts["web_calls"] >= self.limits.web_calls):
                self.stop_reason = "BUDGET_EXCEEDED"
                raise ExecutionError("BUDGET_EXCEEDED")
            self.counts[key] += 1
            if kind == "web":
                self.counts["web_calls"] += 1

    def can_research(self):
        with self.lock:
            allowed = (not self.stop_reason and time.monotonic() - self.started < self.limits.seconds - self.limits.reserve_seconds
                    and self.counts["model_calls"] < self.limits.model_calls - self.limits.reserve_model_calls
                    and self.counts["tool_calls"] < self.limits.tool_calls)
            if not allowed and not self.stop_reason:
                self.stop_reason = "BUDGET_EXCEEDED"
            return allowed

    def query_once(self, provider, query):
        key = (provider, " ".join(query.split()).casefold())
        with self.lock:
            if key in self.seen_queries or provider in self.blocked_providers:
                return False
            self.seen_queries.add(key)
            return True

    def error(self, exc, node=""):
        item = {**exc.as_dict(), "node": node}
        with self.lock:
            if item not in self.errors:
                self.errors.append(item)
            if exc.code in {"AUTH_ERROR", "QUOTA_EXCEEDED"}:
                self.blocked_providers.add(exc.provider)
        self.emit({"type": "warning", **item})

    def summary(self):
        with self.lock:
            return {**self.counts, "token_usage": {**self.tokens, "status": "known" if self.tokens["known_calls"] == self.counts["model_calls"] else "partial_or_unknown"},
                    "provider_http_attempts": "unknown_for_model_and_embedding", "estimated_cost": None,
                    "duration_ms": round((time.monotonic() - self.started) * 1000), "termination_reason": self.stop_reason,
                    "errors": list(self.errors), "limits": self.limits.__dict__}

    @contextmanager
    def span(self, kind, name):
        span_id = str(uuid4())
        parent = _SPAN.get() or self.root_span
        self.emit({"type": "call_start", "kind": kind, "name": name, "span_id": span_id, "parent_span_id": parent})
        token = _SPAN.set(span_id)
        start, status = time.monotonic(), "ok"
        try:
            yield {"span_id": span_id, "parent_span_id": parent}
        except Exception:
            status = "error"
            raise
        finally:
            _SPAN.reset(token)
            self.emit({"type": "call_end", "kind": kind, "name": name, "span_id": span_id,
                       "parent_span_id": parent, "status": status, "duration_ms": round((time.monotonic() - start) * 1000)})


def register(context):
    with _RUNS_LOCK:
        _RUNS[context.run_id] = context


def unregister(run_id):
    with _RUNS_LOCK:
        _RUNS.pop(run_id, None)


def lookup(run_id):
    with _RUNS_LOCK:
        return _RUNS.get(run_id)


def current():
    return _CURRENT.get()


@contextmanager
def activate(context):
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def invoke_model(agent, messages, name, terminal=False, system_prompt=None):
    context = current()
    if context:
        if sum(len(str(m.content)) for m in messages) + len(system_prompt or "") > context.limits.input_chars:
            context.stop_reason = "BUDGET_EXCEEDED"
            raise ExecutionError("BUDGET_EXCEEDED")
        context.reserve("model", terminal)
    def call():
        if system_prompt:
            from langchain_core.messages import SystemMessage
            raw = getattr(agent, "_harness_llm", None)
            payload = [SystemMessage(content=system_prompt), *messages]
            result = {"messages": [*messages, raw.invoke(payload)]} if raw else agent.invoke({"messages": payload})
        else:
            result = agent.invoke({"messages": messages})
        if context:
            response = result["messages"][-1]
            usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get("token_usage")
            if usage and "input_tokens" in usage and "output_tokens" in usage:
                context.add_usage(usage)
                context.emit({"type": "usage", "node": name, "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]})
        return result
    try:
        if context:
            with context.span("model", name):
                return call()
        return call()
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError("MODEL_ERROR", "model") from exc


def invoke_chat_model(model, messages, name, terminal=False):
    class Adapter:
        def invoke(self, payload):
            return {"messages": [*payload["messages"], model.invoke(payload["messages"])]}
    return invoke_model(Adapter(), messages, name, terminal)["messages"][-1]
