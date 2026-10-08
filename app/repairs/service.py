"""Persisted proposal -> operator approval -> single execution -> bounded verification."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from uuid import uuid4
from threading import BoundedSemaphore
from incidents.support import IncidentError, now
from providers.fixtures import ProviderError
from repairs.contracts import ACTIONS, RepairProposal
from repairs.executors import DockerExecutor
from storage.repairs import guard_active


class RepairService:
    def __init__(self, workflow, executors=None):
        self.workflow = workflow
        self.executors = dict(executors) if executors is not None else {'docker_http': DockerExecutor()}
        self.capacity = BoundedSemaphore(2)

    def _transaction(self, iid, principal, mutation):
        self.workflow.start()
        return self.workflow._store.repair_transaction(iid, principal, mutation)

    @staticmethod
    def _operator(principal):
        if principal.role != 'operator':
            raise IncidentError('OPERATOR_REQUIRED', 403)

    def _target(self, incident, principal, action, target_name):
        binding = self.workflow.services.resolve(incident, principal)
        target = deepcopy(binding.repairs.get(target_name))
        if not target or action not in target.get('actions', []):
            raise IncidentError('REPAIR_TARGET_DENIED', 403)
        executor = self.executors.get(target.get('executor'))
        if executor is None or action not in executor.actions:
            raise IncidentError('ACTION_UNSUPPORTED', 422)
        for key, default, maximum in (('verification_attempts', 3, 6), ('verification_interval_seconds', 1, 10)):
            value = target.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise IncidentError('REPAIR_VERIFICATION_CONFIG_INVALID', 422)
        return binding, target, executor

    def capabilities(self, binding):
        items = []
        for target_name, config in binding.repairs.items():
            executor = self.executors.get(config.get('executor'))
            if executor is None:
                continue
            for action in config.get('actions', []):
                if action in executor.actions and action in ACTIONS:
                    items.append({'action': action, 'target': target_name, **ACTIONS[action],
                        'max_replicas': config.get('max_replicas', 1), 'versions': config.get('versions', []),
                        'success_condition': executor.verification, 'requires_approval': True})
        return items

    def from_recommendation(self, iid, payload, principal):
        run = self.workflow.get_run(payload['run_id'], principal)
        if not run or run['config'].get('incident_id') != iid:
            raise IncidentError('REPAIR_REQUIRES_REVIEWED_DIAGNOSIS')
        actions = (run.get('result') or {}).get('output', {}).get('recommended_actions', [])
        index = payload['action_index']
        if index >= len(actions) or not actions[index].get('repair') or not actions[index]['requires_approval']:
            raise IncidentError('NO_EXECUTABLE_RECOMMENDATION', 422)
        return self.propose(iid, {**actions[index]['repair'], 'request_key': payload['request_key'],
            'revision': payload['revision'], 'run_id': payload['run_id'], 'reason': actions[index]['action']}, principal)

    @staticmethod
    def _current(incident, proposal):
        if (incident['revision'] != proposal['revision'] or incident['latest_run_id'] != proposal['run_id']
                or incident['business_status'] in {'investigating', 'resolved'}):
            raise IncidentError('REPAIR_SOURCE_CHANGED')

    @staticmethod
    def _event(plan, kind, **fields):
        plan['events'].append({'type': kind, 'at': now(), **fields})

    def propose(self, iid, payload, principal):
        payload = RepairProposal.model_validate(payload).model_dump()
        run = self.workflow.get_run(payload['run_id'], principal)
        if (not run or run['status'] != 'completed' or run['config'].get('incident_id') != iid
                or (run.get('result') or {}).get('review_status') != 'passed'):
            raise IncidentError('REPAIR_REQUIRES_REVIEWED_DIAGNOSIS')
        visible = {item['evidence_id'] for item in run['result'].get('evidence', []) if item.get('kind') == 'observation'}
        if not set(payload['evidence_ids']).issubset(visible):
            raise IncidentError('REPAIR_EVIDENCE_INVALID', 422)
        def create(incident, plans):
            old = next((p for p in plans if p['proposal']['request_key'] == payload['request_key']), None)
            if old:
                if old['proposal'] != payload:
                    raise IncidentError('REQUEST_KEY_REUSED')
                return old
            guard_active(plans)
            self._current(incident, payload)
            if len(plans) >= 100:
                raise IncidentError('REPAIR_PLAN_LIMIT')
            binding, target, executor = self._target(incident, principal, payload['action'], payload['target'])
            if executor.live and (incident.get('demo_case_id')
                    or run['config']['execution']['execution_mode'] != 'live'):
                raise IncidentError('SYNTHETIC_DIAGNOSIS_CANNOT_REPAIR_LIVE_TARGET', 403)
            if payload['action'] == 'scale_service' and payload['parameters']['replicas'] > target.get('max_replicas', 1):
                raise IncidentError('REPAIR_PARAMETER_DENIED', 422)
            if payload['action'] == 'rollback_release' and payload['parameters']['version'] not in target.get('versions', []):
                raise IncidentError('REPAIR_PARAMETER_DENIED', 422)
            plan = {'id': str(uuid4()), 'incident_id': iid, 'proposal': payload, 'status': 'pending_approval',
                    'binding_fingerprint': binding.fingerprint, 'executor': executor.name, 'live': executor.live,
                    'impact': ACTIONS[payload['action']]['impact'], 'rollback': ACTIONS[payload['action']]['rollback'],
                    'success_condition': executor.verification, 'created_at': now(),
                    'verification_budget': {'attempts': target.get('verification_attempts', 3),
                                            'interval_seconds': target.get('verification_interval_seconds', 1)},
                    'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(), 'events': []}
            plan['digest'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
            self._event(plan, 'proposed')
            plans.append(plan)
            return plan
        return self._transaction(iid, principal, create)

    def list(self, iid, principal):
        return self._transaction(iid, principal, lambda incident, plans: plans)

    @staticmethod
    def _find(plans, pid):
        plan = next((p for p in plans if p['id'] == pid), None)
        if plan is None:
            raise IncidentError('REPAIR_NOT_FOUND', 404)
        return plan

    def _valid(self, incident, plan, principal):
        self._current(incident, plan['proposal'])
        if datetime.now(timezone.utc) >= datetime.fromisoformat(plan['expires_at']):
            raise IncidentError('REPAIR_EXPIRED')
        binding, target, executor = self._target(incident, principal, plan['proposal']['action'], plan['proposal']['target'])
        if binding.fingerprint != plan['binding_fingerprint'] or executor.name != plan['executor'] or executor.live != plan['live']:
            raise IncidentError('REPAIR_CONFIGURATION_CHANGED')
        return target, executor

    def approve(self, iid, pid, payload, principal):
        self._operator(principal)
        def change(incident, plans):
            plan = self._find(plans, pid)
            if payload['digest'] != plan['digest']:
                raise IncidentError('REPAIR_DIGEST_MISMATCH')
            desired = 'approved' if payload['decision'] == 'approve' else 'rejected'
            if plan['status'] == desired:
                return plan
            if plan['status'] != 'pending_approval':
                raise IncidentError('REPAIR_STATE_CONFLICT')
            guard_active(plans)
            if desired == 'approved':
                self._valid(incident, plan, principal)
            plan.update(status=desired, approved_by=principal.user_id)
            self._event(plan, desired)
            return plan
        return self._transaction(iid, principal, change)

    def execute(self, iid, pid, payload, principal):
        self._operator(principal)
        if not self.capacity.acquire(blocking=False):
            raise IncidentError('REPAIR_CAPACITY_EXCEEDED', 429)
        try:
            return self._execute(iid, pid, payload, principal)
        finally:
            self.capacity.release()

    def _execute(self, iid, pid, payload, principal):
        self._operator(principal)
        selected = {}
        def claim(incident, plans):
            plan = self._find(plans, pid)
            if 'execution_key' in plan:
                if plan['execution_key'] != payload['request_key']:
                    raise IncidentError('REPAIR_ALREADY_ATTEMPTED')
                return plan
            if plan['status'] != 'approved':
                raise IncidentError('REPAIR_APPROVAL_REQUIRED', 403)
            guard_active(plans)
            target, executor = self._valid(incident, plan, principal)
            selected.update(target=target, executor=executor)
            plan.update(status='executing', execution_key=payload['request_key'])
            self._event(plan, 'execution_claimed')
            return plan
        plan = self._transaction(iid, principal, claim)
        if not selected:
            return plan  # Includes in-flight, interrupted and terminal calls; never replay a write.
        executor, target, proposal = selected['executor'], selected['target'], plan['proposal']
        events, status = [], 'blocked'
        attempted = False
        try:
            before = executor.preflight(target, proposal['action'], proposal['parameters'])
            events.append(('preflight_passed', {'observation': before}))
            attempted = True
            executor.execute(target, proposal['action'], proposal['parameters'])
            events.append(('action_completed', {}))
            status = 'manual_required'
            attempts, interval = target.get('verification_attempts', 3), target.get('verification_interval_seconds', 1)
            for attempt in range(attempts):
                verification = executor.verify(target, proposal['action'], proposal['parameters'], before)
                events.append(('verification', {'attempt': attempt + 1, 'observation': verification}))
                if verification.get('passed') is True:
                    status = 'verified'
                    break
                if attempt < attempts - 1:
                    time.sleep(interval)
        except Exception as exc:
            status = 'manual_required' if attempted else 'blocked'
            events.append(('execution_error', {'code': exc.code if isinstance(exc, ProviderError) else 'EXECUTOR_FAILED'}))
        def finish(incident, plans):
            stored = self._find(plans, pid)
            if stored['status'] != 'executing':
                raise IncidentError('REPAIR_STATE_CONFLICT')
            stored['status'] = status
            for kind, fields in events:
                self._event(stored, kind, **fields)
            self._event(stored, status)
            return stored
        return self._transaction(iid, principal, finish)
