"""Incident-row locks serialize repair state and protect concurrent business edits."""
from copy import deepcopy
from psycopg.types.json import Jsonb
from incidents.support import IncidentError, now
from storage.incidents import public_incident


def guard_active(plans):
    if any(plan['status'] == 'executing' for plan in plans):
        raise IncidentError('REPAIR_IN_PROGRESS')


class PostgresRepairs:
    def repair_transaction(self, iid, principal, mutation):
        with self.pool.connection() as conn:
            row = self._incident_lock(conn, iid, principal, allow_repair=True)
            record = conn.execute('SELECT document FROM incident_repairs WHERE incident_id=%s', (iid,)).fetchone()
            plans = record['document'] if record else []
            result = mutation(public_incident(row), plans)
            conn.execute('INSERT INTO incident_repairs(incident_id,document) VALUES(%s,%s) '
                         'ON CONFLICT(incident_id) DO UPDATE SET document=EXCLUDED.document', (iid, Jsonb(plans)))
            return deepcopy(result)

    def _guard_repair(self, conn, iid):
        record = conn.execute('SELECT document FROM incident_repairs WHERE incident_id=%s', (iid,)).fetchone()
        guard_active(record['document'] if record else [])

    def recover_repairs(self):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM incident_repairs WHERE document @> '[{\"status\":\"executing\"}]'::jsonb FOR UPDATE").fetchall()
            for row in rows:
                for plan in row['document']:
                    if plan['status'] == 'executing':
                        plan['status'] = 'manual_required'
                        plan['events'].append({'type': 'interrupted', 'code': 'PROCESS_INTERRUPTED',
                                               'at': now()})
                conn.execute('UPDATE incident_repairs SET document=%s WHERE incident_id=%s',
                             (Jsonb(row['document']), row['incident_id']))


class MemoryRepairs:
    def repair_transaction(self, iid, principal, mutation):
        with self.lock:
            row = self._incident(iid, principal, allow_repair=True)
            plans = deepcopy(self.repair_plans.get(iid, []))
            result = mutation(deepcopy(row), plans)
            self.repair_plans[iid] = plans
            return deepcopy(result)

    def recover_repairs(self):
        from incidents.support import now
        with self.lock:
            for plans in self.repair_plans.values():
                for plan in plans:
                    if plan['status'] == 'executing':
                        plan['status'] = 'manual_required'
                        plan['events'].append({'type': 'interrupted', 'code': 'PROCESS_INTERRUPTED', 'at': now()})
