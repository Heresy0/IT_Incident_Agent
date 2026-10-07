"""Two small PostgreSQL tables for committed runs and replayable events."""
import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class PostgresRunStore:
    def __init__(self, dsn):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=4, timeout=5,
                                   kwargs={"row_factory": dict_row, "connect_timeout": 5, "options": "-c statement_timeout=10000"})
        self.pool.wait(timeout=8)
        with self.pool.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY, checkpoint_thread_id TEXT NOT NULL UNIQUE,
                query TEXT NOT NULL, user_id TEXT NOT NULL, thread_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running','completed','partial','failed')),
                config JSONB NOT NULL DEFAULT '{}'::jsonb, result JSONB,
                next_seq BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS research_events (
                run_id TEXT NOT NULL REFERENCES research_runs(run_id) ON DELETE CASCADE,
                seq BIGINT NOT NULL, event JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(run_id, seq))""")

    def create(self, run_id, request, config):
        with self.pool.connection() as conn:
            conn.execute("""INSERT INTO research_runs
                (run_id,checkpoint_thread_id,query,user_id,thread_id,tenant_id,status,config)
                VALUES (%s,%s,%s,%s,%s,%s,'running',%s)""",
                         (run_id, run_id, request["query"], request["user_id"], request["thread_id"], request["tenant_id"], Jsonb(config)))

    def _append(self, conn, run_id, event):
        row = conn.execute("UPDATE research_runs SET next_seq=next_seq+1 WHERE run_id=%s RETURNING next_seq", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        event = {**event, "run_id": run_id, "seq": row["next_seq"]}
        conn.execute("INSERT INTO research_events(run_id,seq,event) VALUES (%s,%s,%s)", (run_id, event["seq"], Jsonb(event)))
        return event

    def append(self, run_id, event):
        with self.pool.connection() as conn:
            return self._append(conn, run_id, event)

    def finish(self, run_id, result):
        with self.pool.connection() as conn:
            # Finish state and the last client event commit together exactly once.
            row = conn.execute("SELECT status FROM research_runs WHERE run_id=%s FOR UPDATE", (run_id,)).fetchone()
            if not row or row["status"] != "running":
                return False
            conn.execute("UPDATE research_runs SET status=%s,result=%s,finished_at=NOW() WHERE run_id=%s",
                         (result["status"], Jsonb(result), run_id))
            self._append(conn, run_id, {"type": "final", **result})
            return True

    def get(self, run_id, principal=None):
        with self.pool.connection() as conn:
            if principal is None:  # Trusted in-process worker / maintenance only.
                row = conn.execute("SELECT * FROM research_runs WHERE run_id=%s", (run_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM research_runs WHERE run_id=%s AND tenant_id=%s AND user_id=%s",
                                   (run_id, principal.tenant_id, principal.user_id)).fetchone()
        if row:
            for key in ("created_at", "finished_at"):
                if row[key]:
                    row[key] = row[key].isoformat()
        return row

    def events(self, run_id, after=0, limit=100, principal=None):
        with self.pool.connection() as conn:
            if principal is None:
                rows = conn.execute("SELECT event FROM research_events WHERE run_id=%s AND seq>%s ORDER BY seq LIMIT %s",
                                    (run_id, after, min(limit, 100))).fetchall()
            else:
                rows = conn.execute("""SELECT e.event FROM research_events e JOIN research_runs r USING(run_id)
                    WHERE e.run_id=%s AND r.tenant_id=%s AND r.user_id=%s AND e.seq>%s ORDER BY e.seq LIMIT %s""",
                                    (run_id, principal.tenant_id, principal.user_id, after, min(limit, 100))).fetchall()
        return [r["event"] for r in rows]

    def fail_interrupted(self):
        # The application explicitly supports one backend process, no multi-worker deployment.
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT run_id,query,user_id,thread_id,tenant_id FROM research_runs WHERE status='running' FOR UPDATE").fetchall()
            for row in rows:
                result = {**row, "status": "failed", "final": "后端进程中断，本次研究未完成；请重新提交研究。",
                          "run_summary": {"termination_reason": "PROCESS_INTERRUPTED"}, "sources": [], "route": "unknown"}
                conn.execute("UPDATE research_runs SET status='failed',result=%s,finished_at=NOW() WHERE run_id=%s", (Jsonb(result), row["run_id"]))
                self._append(conn, row["run_id"], {"type": "final", **result})
        return len(rows)

    def cleanup(self, days=14):
        with self.pool.connection() as conn:
            result = conn.execute("DELETE FROM research_runs WHERE status <> 'running' AND created_at < NOW() - (%s * INTERVAL '1 day')", (days,))
            return result.rowcount

    def close(self):
        self.pool.close()


class MemoryRunStore:
    """Explicit test adapter. Production never silently falls back to this store."""
    def __init__(self):
        from threading import RLock
        self.lock, self.runs, self.history = RLock(), {}, {}

    def create(self, run_id, request, config):
        with self.lock:
            self.runs[run_id] = {**request, "run_id": run_id, "checkpoint_thread_id": run_id,
                                 "status": "running", "config": config, "result": None,
                                 "created_at": datetime.now(timezone.utc).isoformat()}
            self.history[run_id] = []

    def append(self, run_id, event):
        with self.lock:
            event = {**event, "run_id": run_id, "seq": len(self.history[run_id]) + 1}
            self.history[run_id].append(event)
            return event

    def finish(self, run_id, result):
        with self.lock:
            if self.runs[run_id]["status"] != "running":
                return False
            self.runs[run_id].update(status=result["status"], result=result)
            self.append(run_id, {"type": "final", **result})
            return True

    def get(self, run_id, principal=None):
        from copy import deepcopy
        with self.lock:
            row = self.runs.get(run_id)
            if row and principal is not None and (row['tenant_id'], row['user_id']) != (principal.tenant_id, principal.user_id):
                return None
            return deepcopy(row)

    def events(self, run_id, after=0, limit=100, principal=None):
        from copy import deepcopy
        with self.lock:
            if not self.get(run_id, principal):
                return []
            return deepcopy(self.history.get(run_id, [])[after:after + limit])

    def fail_interrupted(self):
        return 0

    def close(self):
        pass
