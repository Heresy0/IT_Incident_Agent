"""Synthetic observations only. No gold imports, network, arbitrary paths or queries."""
import copy
import json
import re
from datetime import datetime
from pathlib import Path
from api.auth import Principal
from evidence.contracts import IncidentScope, Ticket

ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "incident"


class ProviderError(Exception):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable


def visible(row, principal):
    return row.get("visibility") == "public_synthetic" or (
        row.get("tenant_id") == principal.tenant_id and row.get("user_id") == principal.user_id)


class FixtureProvider:
    name = "synthetic_fixture"

    def __init__(self, case_id, principal: Principal):
        # A server-maintained catalog, never a model-supplied filesystem path.
        catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
        entry = next((r for r in catalog if r["case_id"] == case_id and visible(r, principal)), None)
        if entry is None:
            raise ProviderError("CASE_NOT_VISIBLE")
        self._data = json.loads((ROOT / entry["file"]).read_text(encoding="utf-8"))
        self.principal = principal

    def ticket(self):
        ticket = copy.deepcopy(self._data["ticket"])
        ticket["scope"].update(tenant_id=self.principal.tenant_id, user_id=self.principal.user_id)
        return Ticket.model_validate_json(json.dumps(ticket))

    def authorize(self, principal, scope):
        expected = self.ticket().scope
        if principal != self.principal or scope != expected:
            raise ProviderError("SCOPE_DENIED")

    def query(self, name, args, scope: IncidentScope):
        data = self._data
        if name == "get_service_owner":
            rows = [r for r in data["owners"] if args.alias in r["aliases"]]
            cap = 1
        elif name == "get_service_metrics":
            rows = [r for r in data["metrics"] if r["metric"] in args.metrics and r["granularity"] == args.granularity]
            cap = None
        elif name == "get_service_logs":
            rows = [r for r in data["logs"] if r["category"] == args.category
                    and (args.level is None or r["level"] == args.level)
                    and (args.error_code is None or r["error_code"] == args.error_code)]
            cap = 50
        elif name == "get_recent_changes":
            rows = [r for r in data["changes"] if args.category is None or r["category"] == args.category]
            cap = 10
        else:
            key = "runbooks" if name == "search_runbooks" else "incidents"
            query = args.query if key == "runbooks" else args.symptoms
            terms = set(re.findall(r"[\w]+", query.casefold()))
            rows = [r for r in data[key] if terms.intersection(set(re.findall(r"[\w]+", r["search_text"].casefold())))
                    and visible(r, self.principal) and scope.service_version in r["versions"]]
            if key == "runbooks":
                rows = [r for r in rows if args.category is None or r["category"] == args.category]
            else:
                rows = [r for r in rows if r["confirmed"] and r["validity"] == "active"
                        and (args.error_code is None or args.error_code in r["error_codes"])
                        and (args.version is None or args.version in r["versions"])]
            cap = 4
        services = {scope.service}
        if name == "get_service_owner":
            services.update(scope.allowed_dependencies)
        rows = [r for r in rows if r["service"] in services and r["environment"] == scope.environment
                and visible(r, self.principal)]
        if hasattr(args, "start"):
            rows = [r for r in rows if args.start <= datetime.fromisoformat(r["timestamp"]) <= args.end]
        # Recent measurements must survive both the per-metric cap and the message cap.
        # Oldest-first truncation can hide the incident entirely behind healthy baselines.
        rows = sorted(rows, key=lambda r: (-datetime.fromisoformat(r["timestamp"]).timestamp(), r["id"])) \
            if name == "get_service_metrics" else sorted(rows, key=lambda r: (r["timestamp"], r["id"]))
        if name == "get_service_metrics":
            selected, counts = [], {}
            for row in rows:
                metric = row["metric"]
                if counts.get(metric, 0) < 60:
                    selected.append(row)
                    counts[metric] = counts.get(metric, 0) + 1
        else:
            selected = rows[:cap]
        # Only safe allowlisted fields can leave this provider.
        fields = {"id", "timestamp", "service", "environment", "data_version", "metric", "value", "unit",
                  "aggregation", "granularity", "category", "level", "error_code", "message", "version",
                  "summary", "config_summary", "team", "escalation", "aliases", "text", "versions",
                  "updated_at", "confirmed_at", "confirmed", "resolution", "validity"}
        result = [{k: copy.deepcopy(v) for k, v in r.items() if k in fields} for r in selected]
        # Configuration summaries are independently allowlisted, secrets are never summarized.
        for row in result:
            if "config_summary" in row:
                row["config_summary"] = {k: v for k, v in row["config_summary"].items()
                                         if k in {"auth_mode", "auth_audience", "pool_max", "timeout_ms"}}
            for key in ("message", "summary", "text", "resolution", "escalation"):
                if key in row and len(row[key]) > 1800:
                    row[key] = row[key][:1800]
                    row["content_truncated"] = True
        return result, len(selected) < len(rows)
