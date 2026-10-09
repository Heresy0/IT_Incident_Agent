import re
from api.auth import Principal
from evidence.contracts import IncidentScope, Ticket
from providers.fixtures import FixtureProvider, ProviderError, ROOT, visible
from providers.history import private_history
import json


def catalog(principal):
    entries = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    result = []
    for entry in entries:
        if visible(entry, principal):
            ticket = FixtureProvider(entry["case_id"], principal).ticket().model_dump(mode="json")
            scope = ticket["scope"]
            result.append({"case_id": entry["case_id"], "synthetic": True,
                "fields": {"title": ticket["title"], "symptoms": ticket["symptoms"],
                    **{k: scope[k] for k in ("service", "environment", "service_version", "start", "end")},
                    "occurred_at": scope["end"], "demo_case_id": entry["case_id"], "impact": "合成开发案例", "reported_severity": "unknown"}})
    return result


def validate_demo(document, principal):
    cid = document.get("demo_case_id")
    if cid:
        expected = FixtureProvider(cid, principal).ticket().scope.model_dump(mode="json")
        if any(document[k] != expected[k] for k in ("service", "environment", "service_version", "start", "end")):
            raise ProviderError("DEMO_SCOPE_MISMATCH")


def ticket_for(snapshot, principal):
    dependencies = FixtureProvider(snapshot["demo_case_id"], principal).ticket().scope.allowed_dependencies if snapshot.get("demo_case_id") else ()
    scope = IncidentScope.model_validate_json(json.dumps({**{k: snapshot[k] for k in ("service", "environment", "service_version", "start", "end")},
        "tenant_id": principal.tenant_id, "user_id": principal.user_id, "allowed_dependencies": list(dependencies)}))
    return Ticket(incident_id=snapshot["id"], title=snapshot["title"], symptoms=snapshot["symptoms"],
                  purpose=snapshot.get('purpose', 'diagnosis'), scope=scope)


class BoundIncidentProvider:
    """Exact catalog scope, new business incident ID; confirmed history stays owner-only."""
    name = "synthetic_fixture_with_private_history"

    def __init__(self, snapshot, principal, store):
        validate_demo(snapshot, principal)
        self.base = FixtureProvider(snapshot["demo_case_id"], principal)
        self._ticket, self.principal, self.store = ticket_for(snapshot, principal), principal, store

    def ticket(self):
        return self._ticket.model_copy(deep=True)

    def authorize(self, principal, scope):
        if principal != self.principal or scope != self._ticket.scope:
            raise ProviderError("SCOPE_DENIED")
        self.base.authorize(principal, self.base.ticket().scope)

    def query(self, name, args, scope):
        self.authorize(self.principal, scope)
        rows, truncated = self.base.query(name, args, self.base.ticket().scope)
        if name != "search_incidents":
            return rows, truncated
        private = private_history(self.store, self.principal, scope, args)
        combined = [*private, *rows]
        return combined[:4], truncated or len(combined) > 4
