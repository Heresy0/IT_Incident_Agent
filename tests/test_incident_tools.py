import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from backend.auth import Principal
from mult_agents.harness.runtime import RunContext, Limits
from mult_agents.incident.providers import FixtureProvider, ProviderError
from mult_agents.incident.tools import ToolExecutor
from mult_agents.incident.contracts import TOOL_ARGS

P = Principal("demo", "reader")


def executor(case="case_001", role="investigation", principal=P, limits=None):
    provider = FixtureProvider(case, principal)
    return ToolExecutor(provider, principal, provider.ticket().scope, RunContext(limits=limits), role)


def window(ex):
    return {"start": ex.scope.start.isoformat(), "end": ex.scope.end.isoformat()}


class IncidentToolsTests(unittest.TestCase):
    def test_catalog_unknown_path_and_identity(self):
        for case in ("../case_001", "case_999", "/etc/passwd"):
            with self.assertRaises(ProviderError):
                FixtureProvider(case, P)
        ex = executor()
        ex.principal = Principal("other", "reader")
        self.assertEqual(ex.execute("get_service_owner", {"alias": "checkout-api"}).error.code, "SCOPE_DENIED")
        self.assertEqual(ex.context.counts["tool_calls"], 0)

    def test_role_unknown_tools_and_extra_fields(self):
        for name, args in [("shell", {}), ("search_runbooks", {"query": "timeout"}),
                           ("get_service_metrics", {**window(executor()), "metrics": ["db_cpu"], "tenant_id": "x"}),
                           ("get_service_logs", {**window(executor()), "category": "dependency", "query": "*"})]:
            ex = executor()
            self.assertEqual(ex.execute(name, args).status, "error")
            self.assertEqual(ex.context.counts["tool_calls"], 0)
        ex = executor(role="unknown")
        self.assertEqual(ex.schemas(), [])
        self.assertEqual(ex.execute("get_service_owner", {"alias": "checkout-api"}).error.code, "TOOL_DENIED")

    def test_window_timezone_service_and_scope_expansion(self):
        ex = executor()
        args = {**window(ex), "metrics": ["db_cpu"]}
        args["start"] = (ex.scope.start - timedelta(seconds=1)).isoformat()
        self.assertEqual(ex.execute("get_service_metrics", args).error.code, "WINDOW_DENIED")
        args["start"] = "2026-09-14T09:50:00"
        self.assertEqual(ex.execute("get_service_metrics", args).error.code, "INVALID_ARGUMENTS")
        self.assertEqual(ex.execute("get_service_owner", {"alias": "unregistered"}).error.code, "SERVICE_DENIED")
        ex.scope = ex.scope.model_copy(update={"allowed_dependencies": ("other-db",)})
        self.assertEqual(ex.execute("get_service_owner", {"alias": "other-db"}).error.code, "SCOPE_DENIED")
        self.assertEqual(ex.context.counts["tool_calls"], 0)

    def test_metrics_real_filter_units_granularity_and_time(self):
        ex = executor()
        args = {**window(ex), "metrics": ["db_cpu"]}
        args["start"] = "2026-09-14T10:04:00+00:00"
        result = ex.execute("get_service_metrics", args)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].payload["metric"], "db_cpu")
        self.assertEqual(result.evidence[0].payload["unit"], "percent")
        self.assertIn("value", result.evidence[0].allowed_field_paths)
        self.assertNotIn("payload.value", result.evidence[0].allowed_field_paths)
        result = ex.execute("get_service_metrics", {**window(ex), "metrics": ["db_cpu"], "granularity": "5m"})
        self.assertEqual(result.status, "empty")
        self.assertIsNone(result.error)

    def test_truncation_keeps_incident_measurements_instead_of_only_baselines(self):
        metrics = ["request_error_rate", "request_latency", "dependency_error_rate", "db_cpu",
                   "pool_usage", "pool_wait", "request_rate"]
        for case, expected in [("case_001", {"dependency_error_rate": .92}),
                               ("case_003", {"pool_usage": 1.0, "pool_wait": 1400, "db_cpu": 26})]:
            ex = executor(case)
            result = ex.execute("get_service_metrics", {**window(ex), "metrics": metrics})
            self.assertTrue(result.truncated)
            self.assertEqual(result.sample_order, "latest_first")
            self.assertLessEqual(len(result.model_dump_json()), 12000)
            observed = {e.payload["metric"]: e.payload["value"] for e in result.evidence}
            for metric, value in expected.items():
                self.assertEqual(observed[metric], value)
            self.assertTrue(all(e.payload["timestamp"] == "2026-09-14T10:05:00+00:00" for e in result.evidence))
            self.assertEqual(ex.context.counts["tool_calls"], 1)

    def test_provider_metric_cap_keeps_latest_sixty_points(self):
        ex = executor()
        template = next(r for r in ex.provider._data["metrics"] if r["metric"] == "db_cpu")
        ex.provider._data["metrics"] = [{**template, "id": f"point-{n:03}", "value": n,
            "timestamp": (ex.scope.start + timedelta(seconds=n)).isoformat()} for n in range(62)]
        args = TOOL_ARGS["get_service_metrics"].model_validate_json(json.dumps({**window(ex), "metrics": ["db_cpu"]}))
        rows, truncated = ex.provider.query("get_service_metrics", args, ex.scope)
        self.assertTrue(truncated)
        self.assertEqual(len(rows), 60)
        self.assertEqual([row["value"] for row in rows], list(range(61, 1, -1)))

    def test_logs_changes_and_configuration_redaction(self):
        ex = executor("case_002")
        args = {**window(ex), "category": "configuration", "level": "ERROR", "error_code": "AUTH_AUDIENCE_MISMATCH"}
        result = ex.execute("get_service_logs", args)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(ex.execute("get_service_logs", {**args, "level": "INFO"}).status, "empty")
        ex.provider._data["changes"][0]["config_summary"]["api_key"] = "TEST_SECRET_CANARY"
        ex.provider._data["changes"][0]["credentials"] = "TEST_SECRET_CANARY"
        result = ex.execute("get_recent_changes", {**window(ex), "category": "deployment"})
        self.assertNotIn("TEST_SECRET_CANARY", result.model_dump_json())
        self.assertIn("checkout-preview", result.model_dump_json())
        self.assertIn("config_summary.auth_audience", result.evidence[0].allowed_field_paths)
        self.assertNotIn("config_summary.api_key", result.evidence[0].allowed_field_paths)
        self.assertEqual(ex.execute("get_recent_changes", {**window(ex), "category": "scaling"}).status, "empty")

    def test_owner_dependency_registration(self):
        ex = executor()
        result = ex.execute("get_service_owner", {"alias": "inventory-api"})
        self.assertEqual(result.evidence[0].payload["team"], "Inventory on-call")
        self.assertEqual(result.evidence[0].service, "inventory-api")

    def test_knowledge_version_confirmed_visibility_filters(self):
        ex = executor("case_003", role="knowledge")
        rows = ex.provider._data["incidents"]
        private = copy.deepcopy(rows[0]); private.update(id="private", visibility="private", tenant_id="other", user_id="reader")
        rows.append(private)
        result = ex.execute("search_incidents", {"symptoms": "latency pool"})
        self.assertEqual([e.payload["id"] for e in result.evidence], ["hist-1"])
        self.assertEqual(result.evidence[0].kind, "past_incident")
        self.assertEqual(ex.execute("search_incidents", {"symptoms": "latency", "version": "1.3.0"}).status, "empty")
        self.assertEqual(ex.execute("search_incidents", {"symptoms": "latency", "error_code": "AUTH_AUDIENCE_MISMATCH"}).status, "empty")
        result = ex.execute("search_runbooks", {"query": "pool", "category": "resource"})
        self.assertEqual([e.payload["id"] for e in result.evidence], ["rb-3"])
        self.assertEqual(ex.execute("get_service_metrics", {**window(ex), "metrics": ["db_cpu"]}).error.code, "TOOL_DENIED")

    def test_all_caps_and_truncation(self):
        scenarios = [("metrics", "get_service_metrics", {"metrics": ["db_cpu"]}, 60),
                     ("logs", "get_service_logs", {"category": "resource"}, 50),
                     ("changes", "get_recent_changes", {}, 10),
                     ("runbooks", "search_runbooks", {"query": "timeout"}, 4),
                     ("incidents", "search_incidents", {"symptoms": "latency"}, 4)]
        for key, name, args, cap in scenarios:
            ex = executor(role="knowledge" if key in {"runbooks", "incidents"} else "investigation")
            template = next(r for r in ex.provider._data[key] if key != "metrics" or r["metric"] == "db_cpu")
            ex.provider._data[key] = [{**copy.deepcopy(template), "id": f"r{n:03}"} for n in range(cap + 2)]
            if key not in {"runbooks", "incidents"}:
                args = {**window(ex), **args}
            result = ex.execute(name, args)
            self.assertLessEqual(len(result.evidence), cap)
            self.assertGreater(len(result.evidence), 0)
            self.assertTrue(result.truncated)

    def test_provider_row_caps_independent_of_message_cap(self):
        ex = executor()
        template = next(r for r in ex.provider._data["metrics"] if r["metric"] == "db_cpu")
        ex.provider._data["metrics"] = [{**template, "id": str(n)} for n in range(62)]
        args = TOOL_ARGS["get_service_metrics"].model_validate_json(json.dumps({**window(ex), "metrics": ["db_cpu"]}))
        rows, truncated = ex.provider.query("get_service_metrics", args, ex.scope)
        self.assertEqual(len(rows), 60)
        self.assertTrue(truncated)

    def test_large_content_and_equivalent_queries_are_bounded(self):
        ex = executor()
        ex.provider._data["logs"][1]["message"] = "x" * 50000
        result = ex.execute("get_service_logs", {**window(ex), "category": "dependency"})
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.model_dump_json()), 12000)
        ex.execute("get_service_metrics", {**window(ex), "metrics": ["db_cpu", "pool_wait"]})
        result = ex.execute("get_service_metrics", {**window(ex), "metrics": ["pool_wait", "db_cpu", "db_cpu"]})
        self.assertEqual(result.error.code, "DUPLICATE_TOOL")

    def test_catalog_bound_and_registration_only_for_returned_evidence(self):
        ex = executor()
        template = next(r for r in ex.provider._data["metrics"] if r["metric"] == "db_cpu")
        ex.provider._data["metrics"] = [{**template, "id": f"r{n:03}"} for n in range(60)]
        result = ex.execute("get_service_metrics", {**window(ex), "metrics": ["db_cpu"]})
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.model_dump_json()), 12000)
        self.assertEqual(set(ex.evidence), {e.evidence_id for e in result.evidence})
        options = {o.reference_id for e in result.evidence for o in e.reference_options}
        self.assertEqual(set(ex.references), options)
        first = result.evidence[0]
        self.assertTrue(all(o.field_path in first.allowed_field_paths for o in first.reference_options))

    def test_duplicate_no_double_count_and_snapshot_immutability(self):
        ex = executor(); args = {**window(ex), "metrics": ["db_cpu"]}
        result = ex.execute("get_service_metrics", args)
        result.evidence[0].payload["value"] = 999
        self.assertNotEqual(next(iter(ex.evidence.values())).payload["value"], 999)
        self.assertEqual(ex.execute("get_service_metrics", args).error.code, "DUPLICATE_TOOL")
        self.assertEqual(ex.context.counts["tool_calls"], 1)
        ex.seen.clear()
        refreshed = ex.execute("get_service_metrics", args)
        self.assertEqual(len(ex.evidence), 2)
        self.assertEqual(refreshed.evidence[0].retrieved_at, next(iter(ex.evidence.values())).retrieved_at)
        ex.seen.clear(); ex.provider._data["metrics"][6]["value"] = 99
        ex.execute("get_service_metrics", args)
        self.assertEqual(len(ex.evidence), 3)

    def test_retry_count_error_sanitization_and_limit(self):
        ex = executor()
        with patch.object(ex.provider, "query", side_effect=ProviderError("TIMEOUT", True)) as query:
            result = ex.execute("get_service_owner", {"alias": "checkout-api"})
        self.assertEqual(query.call_count, 2)
        self.assertEqual(ex.context.counts["tool_calls"], 2)
        self.assertEqual(result.error.code, "TIMEOUT")
        ex = executor(limits=Limits(tool_calls=1))
        with patch.object(ex.provider, "query", side_effect=ProviderError("TIMEOUT", True)) as query:
            result = ex.execute("get_service_owner", {"alias": "checkout-api"})
        self.assertEqual(query.call_count, 1)
        self.assertEqual(result.error.code, "BUDGET_EXCEEDED")
        ex = executor()
        with patch.object(ex.provider, "query", side_effect=RuntimeError("TEST_SECRET_CANARY")):
            self.assertNotIn("TEST_SECRET_CANARY", ex.execute("get_service_owner", {"alias": "checkout-api"}).model_dump_json())

    def test_budget_atomicity(self):
        context = RunContext(limits=Limits(tool_calls=7))
        def reserve(_):
            try:
                context.reserve("tool"); return 1
            except Exception:
                return 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(reserve, range(50))), 7)
        self.assertEqual(context.counts["tool_calls"], 7)

    def test_business_provider_never_opens_gold(self):
        from pathlib import Path
        original = Path.read_text
        paths = []
        def tracked(path, *args, **kwargs):
            paths.append(str(path)); return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", tracked):
            ex = executor()
            ex.execute("get_service_metrics", {**window(ex), "metrics": ["db_cpu"]})
        self.assertTrue(paths)
        self.assertTrue(all("evals" not in path and "gold" not in path for path in paths))
        self.assertEqual(len(TOOL_ARGS), 6)


if __name__ == "__main__":
    unittest.main()
