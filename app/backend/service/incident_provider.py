import re
from backend.auth import Principal
from mult_agents.incident.contracts import IncidentScope, Ticket
from mult_agents.incident.providers import FixtureProvider, ProviderError, ROOT, visible
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
    return Ticket(incident_id=snapshot["id"], title=snapshot["title"], symptoms=snapshot["symptoms"], scope=scope)


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
        terms = set(re.findall(r"\w+", args.symptoms.casefold()))
        private = []
        for row in self.store.confirmed_cases(self.principal):
            incident, confirmed = row["document"]["incident"], row["document"]["confirmation"]
            if (incident["service"], incident["environment"], incident["service_version"]) != (scope.service, scope.environment, scope.service_version):
                continue
            text = confirmed["confirmed_cause"] + " " + confirmed["resolution"]
            if not terms.intersection(re.findall(r"\w+", text.casefold())) or args.error_code and args.error_code not in confirmed["error_codes"] or args.version and args.version != scope.service_version:
                continue
            private.append({"id": row["id"], "timestamp": row["confirmed_at"], "service": scope.service, "environment": scope.environment,
                "data_version": f"manual-{row['revision']}", "text": text[:1800], "resolution": confirmed["resolution"][:1800],
                "confirmed": True, "confirmed_at": row["confirmed_at"], "versions": [scope.service_version], "validity": "active"})
        combined = [*private, *rows]
        return combined[:4], truncated or len(combined) > 4
