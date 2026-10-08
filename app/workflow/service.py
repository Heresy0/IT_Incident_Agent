import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import AsyncIterator
from uuid import uuid4

from config.incident import IncidentConfig
from runtime.context import RunContext, Limits, ExecutionError, activate, register, unregister
from observability.telemetry import METRICS
from storage.runs import PostgresRunStore

logger = logging.getLogger("incident.workflow")


class CapacityExceeded(RuntimeError):
    pass


class WorkflowService:
    def __init__(self, config_path, *, store=None, config=None, max_concurrency=None, incident_model_factory=None,
                 service_registry=None, repair_executors=None):
        self._config_path = config_path
        self._store_lock = Lock()
        self._base_config = config
        self._store = store
        self._store_started = False
        self._admission_lock = Lock()
        self._closed = False
        from providers.registry import ServiceRegistry
        self.services = service_registry if service_registry is not None else ServiceRegistry.from_environment()
        from repairs.service import RepairService
        self.repairs = RepairService(self, repair_executors)
        capacity = max_concurrency or max(1, min(4, int(os.getenv("INCIDENT_MAX_CONCURRENCY", os.getenv("RESEARCH_MAX_CONCURRENCY", 2)))))
        self._capacity = BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="workflow")
        from workflow.execution import IncidentAdapter
        self._incident = IncidentAdapter(self, incident_model_factory)

    def start(self):
        with self._store_lock:
            if self._store_started:
                return
            if self._base_config is None:
                self._base_config = IncidentConfig.from_file(self._config_path)
            if self._store is None:
                self._store = PostgresRunStore(self._base_config.postgres_dsn)
            count = self._store.fail_interrupted()
            self._store.recover_repairs()
            if count:
                logger.warning("interrupted_runs_marked_failed | count=%d", count)
            self._store_started = True

    def close(self):
        with self._admission_lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        if self._store:
            self._store.close()

    def _execute(self, run_id, request):
        adapter = self._incident
        context = RunContext(run_id, limits=adapter.limits(request))
        try:
            events = self._store.events(run_id)
            def emit(event):
                events.append(self._store.append(run_id, event))
            context = RunContext(run_id, limits=adapter.limits(request), emit=emit, scope=adapter.scope(request))
            register(context)
            with activate(context):
                try:
                    result = adapter.execute(request, context, events)
                except Exception as exc:
                    code = exc.code if isinstance(exc, ExecutionError) else "INTERNAL_ERROR"
                    logger.error("run_failed | run_id=%s exception_type=%s", run_id, type(exc).__name__)
                    result = adapter.failure(request, context, code)
                self._store.finish(run_id, result)
                METRICS.finish_run(result["status"], result.get("run_summary", {}))
        except Exception as exc:
            logger.error("run_finish_storage_failed | run_id=%s exception_type=%s", run_id, type(exc).__name__)
            try:
                self._store.finish(run_id, adapter.failure(request, context, "INTERNAL_ERROR"))
            except Exception:
                logger.error("run_recovery_storage_failed | run_id=%s", run_id)
        finally:
            unregister(run_id)
            with METRICS.lock:
                METRICS.active -= 1
            self._capacity.release()

    def get_run(self, run_id, principal=None):
        self.start()
        return self._store.get(run_id, principal)

    def create_incident(self, document, principal):
        self.start()
        from providers.bound import validate_demo
        validate_demo(document, principal)
        return self._store.create_incident(document, principal)

    def list_incidents(self, principal, offset=0, limit=20):
        self.start()
        return self._store.list_incidents(principal, offset, limit)

    def get_incident(self, iid, principal):
        self.start()
        from incidents.support import IncidentError
        row = self._store.get_incident(iid, principal)
        if not row:
            raise IncidentError("INCIDENT_NOT_FOUND", 404)
        return row

    def patch_incident(self, iid, document, revision, principal):
        self.get_incident(iid, principal)
        from providers.bound import validate_demo
        validate_demo(document, principal)
        return self._store.patch_incident(iid, document, revision, principal)

    def start_diagnosis(self, iid, execution, principal):
        from incidents.support import IncidentError
        with self._admission_lock:
            if self._closed:
                raise CapacityExceeded("诊断服务正在关闭。")
            incident = self.get_incident(iid, principal)
            old = self._store.find_diagnosis(iid, execution["request_key"], principal)
            if old:
                if old["config"]["execution"] != execution:
                    raise IncidentError("REQUEST_KEY_REUSED")
                return old["run_id"], True
            if incident["business_status"] in {"investigating", "resolved"}:
                raise IncidentError("INCIDENT_LOCKED")
            if not self._capacity.acquire(blocking=False):
                raise CapacityExceeded("执行池已满，请稍后重试。")
            run_id, created, active = str(uuid4()), False, False
            try:
                run_id, created, config = self._store.begin_diagnosis(iid, run_id, execution, principal,
                    self._incident.metadata(execution))
                if not created:
                    self._capacity.release()
                    return run_id, True
                request = {"task_type": "incident", "execution": config["execution"], "incident_snapshot": config["incident_snapshot"],
                    "query": config["incident_snapshot"]["symptoms"], "thread_id": iid,
                    "tenant_id": principal.tenant_id, "user_id": principal.user_id}
                with METRICS.lock:
                    METRICS.active += 1
                    active = True
                self._executor.submit(self._execute, run_id, request)
            except Exception:
                if active:
                    with METRICS.lock:
                        METRICS.active -= 1
                try:
                    if created:
                        adapter = self._incident
                        context = RunContext(run_id, limits=adapter.limits(request), scope=adapter.scope(request))
                        self._store.finish(run_id, adapter.failure(request, context, "INTERNAL_ERROR"))
                finally:
                    self._capacity.release()
                raise
            return run_id, False

    def confirm_incident(self, iid, payload, principal):
        from incidents.support import IncidentError
        if principal.role != "operator":
            raise IncidentError("OPERATOR_REQUIRED", 403)
        self.start()
        return self._store.confirm_incident(iid, payload, principal)

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
