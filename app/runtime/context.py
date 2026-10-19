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
    "RAG_UNAVAILABLE": "排障手册检索后端不可用。",
    "RAG_INDEX_NOT_READY": "排障手册索引尚未完整建立，请先完成导入。",
    "RAG_SCOPE_MISMATCH": "知识库返回了不符合当前服务范围的片段，已拒绝使用。",
    "RAG_INDEX_INCOMPATIBLE": "排障手册索引与登记的向量模型或维度不一致。",
    "RAG_RESULT_INVALID": "排障手册索引格式无效，请重新导入。",
    "RAG_DATABASE_NOT_CONFIGURED": "未配置排障手册数据库连接环境变量。",
    "RAG_INDEX_CHANGED": "导入期间手册资料已变化，未覆盖新资料；请核对后重新导入。",
    "RAG_CHUNK_LIMIT": "当前服务范围最多保存 200 个手册片段，请缩减本篇内容或通过完整清单整理资料。",
    "EMBEDDING_FORMAT_INVALID": "向量接口返回格式或维度与登记不一致。",
    "MEMORY_UNAVAILABLE": "本次记忆服务不可用，诊断将继续，但不会读写个人记忆。",
    "BUDGET_EXCEEDED": "本次诊断已达到输入长度、调用次数或时间限制。",
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
    input_chars: int = 48000
    snippet_chars: int = 1800
    max_sources: int = 10

    @classmethod
    def from_env(cls):
        return cls(model_calls=max(3, min(64, int(os.getenv("INCIDENT_MAX_MODEL_CALLS", 16)))),
                   web_calls=max(1, min(48, int(os.getenv("INCIDENT_MAX_WEB_CALLS", 12)))),
                   tool_calls=max(1, min(96, int(os.getenv("INCIDENT_MAX_TOOL_CALLS", 24)))),
                   seconds=max(20, min(300, float(os.getenv("INCIDENT_MAX_SECONDS", 180)))),
                   # Complete request characters, including schema and history;
                   # this is separate from provider input/output token limits.
                   input_chars=max(4000, min(128000, int(os.getenv("INCIDENT_MAX_INPUT_CHARS") or cls.input_chars))))


_CURRENT = ContextVar("incident_runtime", default=None)
_SPAN = ContextVar("incident_parent_span", default=None)


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
        self.stop_reason = ""
        self.emit_callback = emit
        self.root_span = str(uuid4())
        self.journal = None

    def elapsed(self):
        return self.journal.elapsed() if self.journal is not None else time.monotonic() - self.started

    def emit(self, event):
        event = {"run_id": self.run_id, "trace_id": self.run_id, "timestamp": time.time(), **event}
        # Persist first: clients only see committed events.
        with self.event_lock:
            if self.journal is not None and self.journal.emit(event):
                return
            if self.emit_callback:
                self.emit_callback(event)
            record_event(event)

    def claim_repair(self, state, *, reviewer=False):
        """Allow one collection correction and one reviewer correction within the run budget."""
        with self.lock:
            reviewed = state.get('reviewer_repairs', 0)
            used = reviewed if reviewer else state['repairs'] - reviewed
            if used >= 1:
                return False
            if self.journal is not None and not self.journal.claim('reviewer_repair' if reviewer else 'collection_repair'):
                return False
            state['repairs'] += 1
            if reviewer:
                state['reviewer_repairs'] = reviewed + 1
            return True

    def add_usage(self, usage):
        with self.lock:
            for key in ('input_tokens', 'output_tokens'):
                self.tokens[key] += int(usage[key])
            self.tokens['known_calls'] += 1

    def reserve(self, kind, terminal=False):
        if self.journal is not None:
            return self.journal.reserve(kind, terminal)
        return self._reserve_memory(kind, terminal)

    def _reserve_memory(self, kind, terminal=False):
        rejected = []
        with self.lock:
            elapsed = self.elapsed()
            deadline = self.limits.seconds - (0 if terminal else self.limits.reserve_seconds)
            key = "model_calls" if kind == "model" else "tool_calls"
            cap = self.limits.model_calls if kind == "model" else self.limits.tool_calls
            if kind == "model" and not terminal:
                cap -= self.limits.reserve_model_calls
            if elapsed >= deadline or self.counts[key] >= cap or (kind == "web" and self.counts["web_calls"] >= self.limits.web_calls):
                self.stop_reason = "BUDGET_EXCEEDED"
                if elapsed >= deadline:
                    rejected.append(('seconds', elapsed, deadline))
                if self.counts[key] >= cap:
                    rejected.append((key, self.counts[key], cap))
                if kind == 'web' and self.counts['web_calls'] >= self.limits.web_calls:
                    rejected.append(('web_calls', self.counts['web_calls'], self.limits.web_calls))
            else:
                self.counts[key] += 1
                if kind == "web":
                    self.counts["web_calls"] += 1
        # Emit outside the accounting lock: callbacks may read the run summary.
        for dimension, actual, limit in rejected:
            self.emit({'type': 'budget_rejected', 'kind': kind,
                       'dimension': dimension, 'actual': actual, 'limit': limit})
        if rejected:
            raise ExecutionError('BUDGET_EXCEEDED')

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
                    "duration_ms": round(self.elapsed() * 1000), "termination_reason": self.stop_reason,
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


def current():
    return _CURRENT.get()


@contextmanager
def activate(context):
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def invoke_model(agent, messages, name, terminal=False):
    context = current()
    if context and context.journal is not None and context.journal.active is None:
        request = {'name': name, 'terminal': terminal,
                   'messages': [{'type': message.type, 'content': message.content} for message in messages]}
        with context.journal.operation('model', request):
            return invoke_model(agent, messages, name, terminal)
    if context:
        actual = sum(len(str(m.content)) for m in messages)
        if actual > context.limits.input_chars:
            context.stop_reason = "BUDGET_EXCEEDED"
            context.emit({'type': 'budget_rejected', 'node': name, 'kind': 'model',
                'dimension': 'input_chars', 'actual': actual, 'limit': context.limits.input_chars})
            raise ExecutionError("BUDGET_EXCEEDED")
        context.reserve("model", terminal)
    def call():
        if context and context.journal is not None:
            response = context.journal.response(lambda: agent.invoke({"messages": messages}), kind='model')
            result = {'messages': [*messages, response]}
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
        from storage.recovery import RecoveryInterrupted
        if isinstance(exc, RecoveryInterrupted):
            raise
        raise ExecutionError("MODEL_ERROR", "model") from exc


def invoke_chat_model(model, messages, name, terminal=False):
    class Adapter:
        def invoke(self, payload):
            return {"messages": [*payload["messages"], model.invoke(payload["messages"])]}
    return invoke_model(Adapter(), messages, name, terminal)["messages"][-1]
