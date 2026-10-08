"""Incident transactions use the existing run-store pool and terminal event commit."""
from copy import deepcopy
from uuid import uuid4
from psycopg.types.json import Jsonb
from incidents.support import IncidentError, same_owner, now


def public_incident(row):
    return {**row["document"], **{k: row[k] for k in ("id", "tenant_id", "user_id", "business_status", "revision", "latest_run_id")},
            **{k: row[k].isoformat() if hasattr(row[k], "isoformat") else row[k] for k in ("created_at", "updated_at")}}


class PostgresIncidents:
    def create_incident(self, document, principal):
        iid = str(uuid4())
        with self.pool.connection() as conn:
            row = conn.execute("INSERT INTO incidents(id,tenant_id,user_id,document) VALUES (%s,%s,%s,%s) RETURNING *",
                               (iid, principal.tenant_id, principal.user_id, Jsonb(document))).fetchone()
        return public_incident(row)

    def get_incident(self, iid, principal):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id=%s AND tenant_id=%s AND user_id=%s",
                               (iid, principal.tenant_id, principal.user_id)).fetchone()
        return public_incident(row) if row else None

    def list_incidents(self, principal, offset=0, limit=20):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM incidents WHERE tenant_id=%s AND user_id=%s ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                                (principal.tenant_id, principal.user_id, limit, offset)).fetchall()
        return [public_incident(r) for r in rows]

    def _incident_lock(self, conn, iid, principal):
        row = conn.execute("SELECT * FROM incidents WHERE id=%s AND tenant_id=%s AND user_id=%s FOR UPDATE",
                           (iid, principal.tenant_id, principal.user_id)).fetchone()
        if not row:
            raise IncidentError("INCIDENT_NOT_FOUND", 404)
        return row

    def patch_incident(self, iid, document, revision, principal):
        with self.pool.connection() as conn:
            row = self._incident_lock(conn, iid, principal)
            if row["business_status"] in {"investigating", "resolved"}:
                raise IncidentError("INCIDENT_LOCKED")
            if row["revision"] != revision:
                raise IncidentError("REVISION_CONFLICT")
            row = conn.execute("UPDATE incidents SET document=%s,revision=revision+1,business_status='open',updated_at=NOW() WHERE id=%s RETURNING *",
                               (Jsonb(document), iid)).fetchone()
        return public_incident(row)

    def find_diagnosis(self, iid, key, principal):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT run_id,config FROM research_runs WHERE tenant_id=%s AND user_id=%s AND config->>'task_type'='incident' AND config->>'incident_id'=%s AND config->>'request_key'=%s",
                               (principal.tenant_id, principal.user_id, iid, key)).fetchone()
        return row

    def begin_diagnosis(self, iid, run_id, execution, principal, metadata):
        with self.pool.connection() as conn:
            row = self._incident_lock(conn, iid, principal)
            old = conn.execute("SELECT run_id,config FROM research_runs WHERE tenant_id=%s AND user_id=%s AND config->>'task_type'='incident' AND config->>'incident_id'=%s AND config->>'request_key'=%s",
                               (principal.tenant_id, principal.user_id, iid, execution["request_key"])).fetchone()
            if old:
                if old["config"]["execution"] != execution:
                    raise IncidentError("REQUEST_KEY_REUSED")
                return old["run_id"], False, old["config"]
            if row["business_status"] in {"investigating", "resolved"}:
                raise IncidentError("INCIDENT_LOCKED")
            snapshot = public_incident(row)
            config = {**metadata, "task_type": "incident", "incident_id": iid, "request_key": execution["request_key"],
                      "execution": execution, "incident_snapshot": snapshot, "memory_enabled": False}
            conn.execute("INSERT INTO research_runs(run_id,checkpoint_thread_id,query,user_id,thread_id,tenant_id,status,config) VALUES (%s,%s,%s,%s,%s,%s,'running',%s)",
                         (run_id, run_id, snapshot["symptoms"], principal.user_id, iid, principal.tenant_id, Jsonb(config)))
            conn.execute("UPDATE incidents SET latest_run_id=%s,business_status='investigating',updated_at=NOW() WHERE id=%s", (run_id, iid))
            self._append(conn, run_id, {"type": "status", "task_type": "incident", "incident_id": iid, "status": "running", "message": "诊断已接收"})
        return run_id, True, config

    def confirm_incident(self, iid, payload, principal):
        with self.pool.connection() as conn:
            row = self._incident_lock(conn, iid, principal)
            prior = conn.execute("SELECT * FROM confirmed_incident_cases WHERE incident_id=%s", (iid,)).fetchone()
            if prior:
                if prior["request_key"] == payload["request_key"] and prior["document"]["confirmation"] == payload:
                    return {"case_id": prior["id"], "incident_id": iid, "business_status": "resolved", "replayed": True}
                raise IncidentError("ALREADY_CONFIRMED")
            if row["business_status"] == "investigating":
                raise IncidentError("INCIDENT_LOCKED")
            if row["revision"] != payload["revision"] or row["latest_run_id"] != payload["run_id"]:
                raise IncidentError("REVISION_OR_RUN_CONFLICT")
            run = conn.execute("SELECT status,config FROM research_runs WHERE run_id=%s AND tenant_id=%s AND user_id=%s",
                               (payload["run_id"], principal.tenant_id, principal.user_id)).fetchone()
            if not run or run["status"] not in {"completed", "partial"} or run["config"]["incident_snapshot"]["revision"] != row["revision"]:
                raise IncidentError("RUN_NOT_CONFIRMABLE")
            cid = str(uuid4())
            conn.execute("INSERT INTO confirmed_incident_cases(id,tenant_id,user_id,incident_id,run_id,request_key,document) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (cid, principal.tenant_id, principal.user_id, iid, payload["run_id"], payload["request_key"], Jsonb({"incident": public_incident(row), "confirmation": payload})))
            conn.execute("UPDATE incidents SET business_status='resolved',updated_at=NOW() WHERE id=%s", (iid,))
        return {"case_id": cid, "incident_id": iid, "business_status": "resolved", "replayed": False}

    def confirmed_cases(self, principal):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM confirmed_incident_cases WHERE tenant_id=%s AND user_id=%s AND validity='active' ORDER BY confirmed_at DESC LIMIT 40",
                                (principal.tenant_id, principal.user_id)).fetchall()
        return [{**r, "confirmed_at": r["confirmed_at"].isoformat()} for r in rows]


class MemoryIncidents:
    def create_incident(self, document, principal):
        with self.lock:
            iid = str(uuid4())
            row = {**deepcopy(document), "id": iid, "tenant_id": principal.tenant_id, "user_id": principal.user_id,
                   "business_status": "open", "revision": 1, "latest_run_id": None, "created_at": now(), "updated_at": now()}
            self.incidents[iid] = row
            return deepcopy(row)

    def get_incident(self, iid, principal):
        with self.lock:
            row = self.incidents.get(iid)
            return deepcopy(row) if same_owner(row, principal) else None

    def list_incidents(self, principal, offset=0, limit=20):
        with self.lock:
            rows = sorted([r for r in self.incidents.values() if same_owner(r, principal)], key=lambda r: (r["created_at"], r["id"]), reverse=True)
            return deepcopy(rows[offset:offset+limit])

    def _incident(self, iid, principal):
        row = self.incidents.get(iid)
        if not same_owner(row, principal):
            raise IncidentError("INCIDENT_NOT_FOUND", 404)
        return row

    def patch_incident(self, iid, document, revision, principal):
        with self.lock:
            row = self._incident(iid, principal)
            if row["business_status"] in {"investigating", "resolved"}:
                raise IncidentError("INCIDENT_LOCKED")
            if row["revision"] != revision:
                raise IncidentError("REVISION_CONFLICT")
            row.update(deepcopy(document), revision=revision+1, business_status="open", updated_at=now())
            return deepcopy(row)

    def find_diagnosis(self, iid, key, principal):
        with self.lock:
            for run in self.runs.values():
                cfg = run["config"]
                if same_owner(run, principal) and cfg.get("task_type") == "incident" and cfg["incident_id"] == iid and cfg["request_key"] == key:
                    return {"run_id": run["run_id"], "config": deepcopy(cfg)}

    def begin_diagnosis(self, iid, run_id, execution, principal, metadata):
        with self.lock:
            row = self._incident(iid, principal)
            old = self.find_diagnosis(iid, execution["request_key"], principal)
            if old:
                if old["config"]["execution"] != execution:
                    raise IncidentError("REQUEST_KEY_REUSED")
                return old["run_id"], False, old["config"]
            if row["business_status"] in {"investigating", "resolved"}:
                raise IncidentError("INCIDENT_LOCKED")
            cfg = {**metadata, "task_type": "incident", "incident_id": iid, "request_key": execution["request_key"],
                   "execution": deepcopy(execution), "incident_snapshot": deepcopy(row), "memory_enabled": False}
            self.create(run_id, {"query": row["symptoms"], "thread_id": iid, "tenant_id": principal.tenant_id, "user_id": principal.user_id}, cfg)
            row.update(latest_run_id=run_id, business_status="investigating", updated_at=now())
            self.append(run_id, {"type": "status", "task_type": "incident", "incident_id": iid, "status": "running", "message": "诊断已接收"})
            return run_id, True, cfg

    def confirm_incident(self, iid, payload, principal):
        with self.lock:
            row = self._incident(iid, principal)
            old = next((r for r in self.cases.values() if r["incident_id"] == iid), None)
            if old:
                if old["request_key"] == payload["request_key"] and old["document"]["confirmation"] == payload:
                    return {"case_id": old["id"], "incident_id": iid, "business_status": "resolved", "replayed": True}
                raise IncidentError("ALREADY_CONFIRMED")
            if row["business_status"] == "investigating":
                raise IncidentError("INCIDENT_LOCKED")
            if row["revision"] != payload["revision"] or row["latest_run_id"] != payload["run_id"]:
                raise IncidentError("REVISION_OR_RUN_CONFLICT")
            run = self.get(payload["run_id"], principal)
            if not run or run["status"] not in {"completed", "partial"} or run["config"]["incident_snapshot"]["revision"] != row["revision"]:
                raise IncidentError("RUN_NOT_CONFIRMABLE")
            cid = str(uuid4())
            self.cases[cid] = {"id": cid, "incident_id": iid, "tenant_id": principal.tenant_id, "user_id": principal.user_id,
                "document": {"incident": deepcopy(row), "confirmation": deepcopy(payload)}, "request_key": payload["request_key"],
                "confirmed_at": now(), "validity": "active", "revision": 1, "confirmation_source": "operator_manual"}
            row.update(business_status="resolved", updated_at=now())
            return {"case_id": cid, "incident_id": iid, "business_status": "resolved", "replayed": False}

    def confirmed_cases(self, principal):
        with self.lock:
            return deepcopy([r for r in self.cases.values() if same_owner(r, principal) and r["validity"] == "active"][-40:][::-1])
