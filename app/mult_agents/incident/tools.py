import hashlib
import json
import time
from datetime import datetime, timezone
from backend.auth import Principal
from mult_agents.harness.runtime import ExecutionError, RunContext
from .contracts import (TOOL_ARGS, INVESTIGATION_TOOLS, KNOWLEDGE_TOOLS, Evidence,
                        ToolResult, ToolError)
from .providers import ProviderError
from pydantic import ValidationError


class ToolExecutor:
    """One budget entry per real attempt; all boundaries checked before reservation."""
    def __init__(self, provider, principal: Principal, scope, context: RunContext, role="investigation"):
        self.provider, self.principal, self.scope, self.context, self.role = provider, principal, scope, context, role
        self.seen = set()
        self.evidence = {}

    def schemas(self):
        allowed = INVESTIGATION_TOOLS if self.role == "investigation" else KNOWLEDGE_TOOLS if self.role == "knowledge" else ()
        descriptions = {
            "get_service_metrics": "Read registered metrics within ticket window, at most 60 points per metric; 1m/5m granularity.",
            "get_service_logs": "Read logs filtered by category, level and error code within ticket window, at most 50 records.",
            "get_recent_changes": "Read sanitized deployments/config/scaling within ticket window, at most 10 changes.",
            "get_service_owner": "Read registered owner for ticket service or allowed dependency alias.",
            "search_runbooks": "Search applicable readable synthetic runbooks, at most 4 passages.",
            "search_incidents": "Search visible confirmed active cases of applicable version, at most 4 cases.",
        }
        return [{"type": "function", "function": {"name": n, "description": descriptions[n],
                "parameters": TOOL_ARGS[n].model_json_schema()}} for n in sorted(allowed)]

    @staticmethod
    def rejected(code, retryable=False):
        return ToolResult(status="error", error=ToolError(code=code, retryable=retryable))

    def execute(self, name, raw_args):
        allowed = INVESTIGATION_TOOLS if self.role == "investigation" else KNOWLEDGE_TOOLS if self.role == "knowledge" else ()
        if name not in allowed or self.principal.role not in {"user", "operator"}:
            return self.rejected("TOOL_DENIED")
        if (self.scope.tenant_id, self.scope.user_id) != (self.principal.tenant_id, self.principal.user_id):
            return self.rejected("SCOPE_DENIED")
        try:
            # Tool protocol supplies JSON datetime strings; strict JSON rejects coercion of numbers/enums.
            args = TOOL_ARGS[name].model_validate_json(json.dumps(raw_args))
            self.provider.authorize(self.principal, self.scope)
        except (ValidationError, TypeError, ValueError):
            return self.rejected("INVALID_ARGUMENTS")
        except ProviderError as exc:
            return self.rejected(exc.code)
        if hasattr(args, "start") and (args.start < self.scope.start or args.end > self.scope.end):
            return self.rejected("WINDOW_DENIED")
        if name == "get_service_owner" and args.alias not in {self.scope.service, *self.scope.allowed_dependencies}:
            return self.rejected("SERVICE_DENIED")
        signature = (name, args.model_dump_json())
        if signature in self.seen:
            return self.rejected("DUPLICATE_TOOL")
        self.seen.add(signature)
        for attempt in range(2):
            try:
                self.context.reserve("tool")
                with self.context.span("tool", name):
                    rows, truncated = self.provider.query(name, args, self.scope)
                if time.monotonic() - self.context.started >= self.context.limits.seconds - self.context.limits.reserve_seconds:
                    return self.rejected("BUDGET_EXCEEDED")
                items, size = [], 0
                for row in rows:
                    # Bound the entire message too, not just row count. Omitted rows are explicit.
                    record_size = 2 * len(json.dumps(row, ensure_ascii=False)) + 800
                    if size + record_size > 12000:
                        truncated = True
                        break
                    size += record_size
                    truncated = truncated or row.get("content_truncated", False)
                    items.append(self.snapshot(name, row))
                return ToolResult(status="ok" if items else "empty", evidence=items, truncated=truncated)
            except ExecutionError as exc:
                return self.rejected(exc.code)
            except ProviderError as exc:
                if attempt == 0 and exc.retryable and exc.code in {"TIMEOUT", "RATE_LIMIT", "NETWORK_ERROR"}:
                    self.context.emit({"type": "retry", "provider": "incident_fixture", "name": name})
                    continue
                return self.rejected(exc.code)
            except Exception:
                return self.rejected("PROVIDER_UNAVAILABLE")
        return self.rejected("PROVIDER_UNAVAILABLE")

    def snapshot(self, name, row):
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        locator = f"{name}/{row['id']}"
        eid = "EV_" + hashlib.sha256(f"{self.provider.name}/{locator}/{digest}".encode()).hexdigest()[:24]
        if eid not in self.evidence:
            observed = datetime.fromisoformat(row["timestamp"])
            self.evidence[eid] = Evidence(evidence_id=eid,
                kind="runbook" if name == "search_runbooks" else "past_incident" if name == "search_incidents" else "observation",
                provider=self.provider.name, service=row["service"], environment=row["environment"],
                observed_from=observed, observed_to=observed, retrieved_at=datetime.now(timezone.utc),
                payload=row, excerpt=encoded, locator=locator, data_version=row["data_version"], hash=digest)
        # Preserve the initial snapshot even if a caller mutates its returned nested payload.
        return self.evidence[eid].model_copy(deep=True)
