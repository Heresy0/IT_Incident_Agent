import asyncio
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import AsyncIterator
from uuid import uuid4

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.main import build_agents, build_checkpointer
from mult_agents.memory.scoped import ScopedMemoryManager
from mult_agents.prompts import PROMPTS
from mult_agents.state import create_initial_state
from mult_agents.harness.runtime import RunContext, Limits, ExecutionError, activate, register, unregister
from mult_agents.harness.validation import render_report
from mult_agents.harness.coverage import final_coverage, coverage_summary
from mult_agents.harness.telemetry import METRICS
from mult_agents.harness.version import WORKFLOW_VERSION
from .run_store import PostgresRunStore

logger = logging.getLogger("backend.workflow")


class CapacityExceeded(RuntimeError):
    pass


class WorkflowService:
    def __init__(self, config_path, *, store=None, workflow=None, config=None, max_concurrency=None):
        self._config_path = config_path
        self._lock, self._store_lock = Lock(), Lock()
        self._initialized = workflow is not None
        self._base_config = config
        self._memory_manager = None
        self._memory_lock = Lock()
        self._app = workflow
        self._store = store
        self._store_started = False
        self._admission_lock = Lock()
        self._closed = False
        self._checkpointer_context = None
        self._limits = Limits.from_env()
        capacity = max_concurrency or max(1, min(4, int(os.getenv("RESEARCH_MAX_CONCURRENCY", 2))))
        self._capacity = BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="research")

    def start(self):
        with self._store_lock:
            if self._store_started:
                return
            if self._base_config is None:
                self._base_config = AppConfig.from_file(self._config_path)
            if self._store is None:
                self._store = PostgresRunStore(self._base_config.postgres_dsn)
            count = self._store.fail_interrupted()
            if count:
                logger.warning("interrupted_runs_marked_failed | count=%d", count)
            self._store_started = True

    def close(self):
        with self._admission_lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        if self._checkpointer_context:
            self._checkpointer_context.__exit__(None, None, None)
            self._checkpointer_context = None
        if self._store:
            self._store.close()
        if self._memory_manager:
            self._memory_manager.close()

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            config = self._base_config or AppConfig.from_file(self._config_path)
            self._base_config = config
            agents = build_agents(config.model, config.api_key, config)
            self._app = build_workflow_app(agents, build_checkpointer(config))
            from mult_agents import main as workflow_main
            self._checkpointer_context = workflow_main.CHECKPOINTER_CONTEXT
            self._initialized = True

    def get_memory_manager(self):
        self.start()
        with self._memory_lock:
            if not self._base_config.enable_memory:
                raise RuntimeError("Memory is disabled on the server")
            if self._memory_manager is None:
                c = self._base_config
                self._memory_manager = ScopedMemoryManager(
                    postgres_dsn=c.postgres_dsn, short_term_ttl=c.short_term_ttl_seconds,
                    short_term_max_messages=c.short_term_max_messages, short_term_summary_threshold=c.short_term_summary_threshold,
                    enable_milvus=c.enable_milvus, embedding_api_key=c.api_key, milvus_host=c.milvus_host,
                    milvus_port=c.milvus_port, milvus_collection=c.milvus_collection,
                    save_conversation_task=c.save_conversation_task, long_term_scope=c.long_term_scope)
            return self._memory_manager

    def start_run(self, request, principal=None):
        if principal is not None:
            request = principal.bind(request)
        with self._admission_lock:
            if self._closed:
                raise CapacityExceeded("研究服务正在关闭，请稍后重试。")
            return self._admit(request)

    def _admit(self, request):
        self.start()
        if not self._capacity.acquire(blocking=False):
            raise CapacityExceeded("研究服务繁忙，请等待正在运行的研究完成后重试。")
        run_id = str(uuid4())
        active = False
        try:
            from mult_agents.nodes import SUPPORT_PROMPT
            from mult_agents.harness.validation import SCHEMAS
            contracts = {name: schema.model_json_schema() for name, schema in SCHEMAS.items() if schema}
            prompt_version = hashlib.sha256(json.dumps([PROMPTS, SUPPORT_PROMPT, contracts], sort_keys=True,
                                                        ensure_ascii=False).encode()).hexdigest()[:12]
            self._store.create(run_id, request, {"model": self._base_config.model, "prompt_version": prompt_version,
                                                "workflow_version": WORKFLOW_VERSION, "limits": self._limits.__dict__,
                                                "memory_enabled": bool(request.get("enable_memory", False))})
            self._store.append(run_id, {"type": "status", "message": "研究已接收", "status": "running"})
            with METRICS.lock:
                METRICS.active += 1
                active = True
            self._executor.submit(self._execute, run_id, request)
        except Exception:
            if active:
                with METRICS.lock:
                    METRICS.active -= 1
                try:
                    self._store.finish(run_id, {"run_id": run_id, "status": "failed", "final": "研究未能启动。",
                                                "run_summary": {"termination_reason": "INTERNAL_ERROR"}, "sources": []})
                except Exception:
                    logger.error("run_start_storage_failed | run_id=%s", run_id)
            self._capacity.release()
            raise
        return run_id

    def _execute(self, run_id, request):
        context = RunContext(run_id, limits=self._limits, emit=lambda event: self._store.append(run_id, event),
                             scope=(request["tenant_id"], request["user_id"]))
        register(context)
        state = {}
        with activate(context):
            try:
                with context.span("run", "research"):
                    self._ensure_initialized()
                    config = self._base_config.with_overrides(
                        user_id=request["user_id"], thread_id=request["thread_id"], tenant_id=request["tenant_id"],
                        max_iterations=min(1, max(0, request.get("max_iterations") if request.get("max_iterations") is not None else self._base_config.max_iterations)),
                        enable_memory=bool(request.get("enable_memory", False)))
                    memory_context = ""
                    memory_version = None
                    if config.enable_memory and self._memory_manager is None:
                        try:
                            self.get_memory_manager()
                        except Exception as exc:
                            logger.warning("memory_init_failed | exception_type=%s", type(exc).__name__)
                    if self._memory_manager and config.enable_memory:
                        try:
                            context.reserve("local")
                            with context.span("tool", "memory_context"):
                                memory_version = self._memory_manager.version(config.tenant_id, config.user_id)
                                memory_context = self._memory_manager.build_personalized_prompt_context(
                                    user_id=config.user_id, thread_id=config.thread_id, query=request["query"], tenant_id=config.tenant_id,
                                    max_memories=config.memory_top_k)[:3000]
                        except Exception:
                            context.error(ExecutionError("MEMORY_UNAVAILABLE", "memory"), "memory_context")
                    elif config.enable_memory:
                        context.error(ExecutionError("MEMORY_UNAVAILABLE", "memory"), "memory_context")
                    state = create_initial_state(query=request["query"], max_iterations=config.max_iterations,
                                                 user_id=config.user_id, tenant_id=config.tenant_id, memory_context=memory_context, run_id=run_id)
                    graph_config = {"configurable": {"thread_id": run_id, "run_id": run_id}, "recursion_limit": 64}
                    for update in self._app.stream(state, graph_config, stream_mode="updates"):
                        for node, output in update.items():
                            if not isinstance(output, dict):
                                continue
                            for key, value in output.items():
                                state[key] = state.get(key, []) + value if key in {"messages", "retrieval_errors"} else value
                            context.emit({"type": "phase", "node": node, "message": f"{node} 阶段已完成"})
                    # Reading a checkpoint is safe; never invoke the initial state again.
                    if not state.get("final") and hasattr(self._app, "get_state"):
                        snapshot = self._app.get_state(graph_config)
                        if snapshot and snapshot.values:
                            state.update(snapshot.values)
                    if not state.get("final"):
                        raise ExecutionError("INTERNAL_ERROR")
                    if self._memory_manager and config.enable_memory and memory_version is not None and state.get("status") != "failed":
                        try:
                            context.reserve("local", terminal=True)
                            with context.span("tool", "memory_persist"):
                                self._memory_manager.persist_turn(tenant_id=config.tenant_id, user_id=config.user_id,
                                                                 thread_id=config.thread_id, query=request["query"], answer=state["final"],
                                                                 expected_version=memory_version, run_id=run_id, status=state.get("status"))
                        except Exception:
                            context.error(ExecutionError("MEMORY_UNAVAILABLE", "memory"), "memory_persist")
            except Exception as exc:
                error = exc if isinstance(exc, ExecutionError) else ExecutionError("INTERNAL_ERROR")
                logger.error("run_failed | run_id=%s exception_type=%s", run_id, type(exc).__name__)
                context.error(error, "workflow")
                state.update(status="partial" if state.get("verified_findings") else "failed", termination_reason=error.code,
                             retrieval_errors=context.errors)
                state["final"] = render_report(state, limited=True)
            finally:
                summary = context.summary()
                summary["source_count"] = len(state.get("evidence_pool", []))
                summary["verified_finding_count"] = len(state.get("verified_findings", []))
                coverage = final_coverage(state)
                stage = state.get("coverage_stage", "not_assessed") if state.get("research_tasks") else "not_applicable"
                summary["question_coverage"] = coverage_summary(coverage, stage)
                summary["rejected_claim_count"] = len(state.get("verification_rejections", []))
                summary["termination_reason"] = state.get("termination_reason") or summary["termination_reason"]
                from mult_agents.nodes import _extract_citation_ids
                used = set(_extract_citation_ids(state.get("final", "")))
                sources = [{**{k: e.get(k, "") for k in ("source_id", "title", "url", "doc_id", "snippet", "retrieval_level")},
                            "is_simulated": e.get("is_simulated") is True}
                           for e in state.get("evidence_pool", []) if e.get("source_id") in used]
                result = {"run_id": run_id, **{k: request[k] for k in ("query", "user_id", "thread_id", "tenant_id")},
                          "status": state.get("status", "failed"), "route": state.get("intent", "unknown"),
                          "final": state.get("final", "研究执行失败。"), "run_summary": summary, "sources": sources,
                          "question_coverage": coverage, "coverage_stage": stage,
                          "missing_gaps": state.get("missing_gaps", []),
                          "verification_rejections": state.get("verification_rejections", [])}
                try:
                    self._store.finish(run_id, result)
                    METRICS.finish_run(result["status"], summary)
                except Exception:
                    logger.error("run_finish_storage_failed | run_id=%s", run_id)
                finally:
                    unregister(run_id)
                    with METRICS.lock:
                        METRICS.active -= 1
                    self._capacity.release()

    def get_run(self, run_id, principal=None):
        self.start()
        return self._store.get(run_id, principal)

    async def wait_result(self, run_id, principal=None):
        while True:
            run = await asyncio.to_thread(self.get_run, run_id, principal)
            if not run:
                raise KeyError(run_id)
            if run["status"] != "running":
                return run["result"]
            await asyncio.sleep(.15)

    async def events(self, run_id, after_seq=0, principal=None) -> AsyncIterator[dict]:
        while True:
            batch = await asyncio.to_thread(self._store.events, run_id, after_seq, 100, principal)
            for event in batch:
                after_seq = event["seq"]
                yield event
            run = await asyncio.to_thread(self.get_run, run_id, principal)
            if not run:
                return
            if run["status"] != "running" and len(batch) < 100:
                # Re-read after status: terminal event may commit between the two reads.
                tail = await asyncio.to_thread(self._store.events, run_id, after_seq, 100, principal)
                if not tail:
                    return
                for event in tail:
                    after_seq = event["seq"]
                    yield event
            else:
                await asyncio.sleep(.2)

    async def run(self, **request):
        run_id = await asyncio.to_thread(self.start_run, request)
        return await self.wait_result(run_id)

    async def stream_events(self, **request):
        run_id = await asyncio.to_thread(self.start_run, request)
        async for event in self.events(run_id):
            yield event
