import copy
import json
import unittest
from datetime import datetime

from evals.incident.acceptance import assess, ROOT
from agents.scripted_investigation import ScriptedModel
from agents.investigation import investigate, resolve_output, ReferenceValidationError
from runtime.context import Limits
from tests.tools.test_incident_tools import executor, P, window
from tests.agents.test_incident_investigation import Responses, selected
from langchain_core.messages import AIMessage


class IncidentAcceptanceTests(unittest.TestCase):
    def report(self):
        ex = executor()
        result = investigate(ScriptedModel(), ex.provider, P,
                             limits=Limits(model_calls=3, tool_calls=6, reserve_model_calls=0), max_steps=3)
        result.update(execution_mode="scripted_control_only", model="scripted")
        gold = json.loads((ROOT / "evals/incident/gold/case_001.json").read_text(encoding="utf-8"))
        return result, gold

    def test_scripted_and_completed_are_not_business_acceptance(self):
        report, gold = self.report()
        self.assertEqual(report["status"], "completed")
        result = assess(report, gold)
        self.assertFalse(result["checks"]["live"])
        self.assertFalse(result["mechanical_pass"])
        self.assertEqual(result["semantic_review"], "required")
        gold["necessary_checks"].append("unobserved_metric")
        self.assertIn("unobserved_metric", assess(report, gold)["missing_checks"])

    def test_tampered_snapshot_reference_and_counts_fail(self):
        report, gold = self.report()
        for mutate, check in [
            (lambda r: r["evidence"][0]["payload"].update(message="tampered"), "snapshot_integrity"),
            (lambda r: r["output"]["findings"][0]["refs"][0].update(value="tampered"), "references_valid"),
            (lambda r: r["run_summary"].update(tool_calls=4), "step_accounting"),
            (lambda r: r["run_summary"]["limits"].update(model_calls=16), "small_budget"),
            (lambda r: r["events"][0].update(seq=99), "event_sequence"),
        ]:
            changed = copy.deepcopy(report)
            mutate(changed)
            self.assertFalse(assess(changed, gold)["checks"][check])

    def test_mechanical_pass_still_requires_semantic_review(self):
        report, _ = self.report()
        # Synthetic checker input, not a live model result or a business score.
        report["execution_mode"] = "live"
        result = assess(report, {"necessary_checks": ["dependency_error_rate"]})
        self.assertTrue(result["mechanical_pass"])
        self.assertEqual(result["semantic_review"], "required")
        report["output"]["findings"][0]["refs"][0]["value"] = "invalid"
        changed = assess(report, {"necessary_checks": ["dependency_error_rate"]})
        self.assertTrue(changed["checks"]["snapshot_integrity"])
        self.assertFalse(changed["checks"]["references_valid"])

    def test_query_profiles_only_contain_validated_filters_and_window(self):
        ex = executor()
        ex.execute("get_service_logs", {**window(ex), "category": "dependency", "error_code": "PRIVATE_CODE"})
        self.assertEqual(set(ex.last_query_profile), {'start', 'end', 'category', 'level'})
        self.assertEqual(ex.last_query_profile['category'], 'dependency')
        self.assertIsNone(ex.last_query_profile['level'])
        self.assertEqual(datetime.fromisoformat(ex.last_query_profile['start']), ex.scope.start)
        self.assertEqual(datetime.fromisoformat(ex.last_query_profile['end']), ex.scope.end)
        self.assertNotIn('error_code', ex.last_query_profile)
        ex.execute("get_service_owner", {"alias": "TEST_SECRET_CANARY"})
        self.assertEqual(ex.last_query_profile, {})
        ex.execute("get_service_logs", {**window(ex), "category": "TEST_SECRET_CANARY"})
        self.assertEqual(ex.last_query_profile, {})

    def test_metadata_only_finding_rejected_and_repair_can_select_real_field(self):
        ex = executor()
        item = ex.execute("get_service_owner", {"alias": "checkout-api"}).evidence[0]
        options = {o.field_path: o.reference_id for o in item.reference_options}
        def content(path):
            return json.dumps({"findings": [{"statement": "Observed owner", "refs": [{"reference_id": options[path]}]}]})
        with self.assertRaises(ReferenceValidationError) as caught:
            resolve_output(content("service"), ex)
        self.assertEqual(caught.exception.reason, "metadata_only_reference")
        result = investigate(Responses([selected(), AIMessage(content=content("service")), AIMessage(content=content("team"))]),
                             ex.provider, P, limits=Limits(model_calls=3, tool_calls=6, reserve_model_calls=0), max_steps=3)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_summary"]["model_calls"], 3)
        self.assertEqual(result["repairs"], 1)

    def test_truncated_observations_prompt_focus_and_cannot_finish_completed(self):
        ex = executor("case_003")
        args = {**window(ex), "metrics": ["db_cpu", "dependency_error_rate", "pool_usage", "pool_wait",
                                         "request_error_rate", "request_latency", "request_rate"]}
        observation = ex.execute("get_service_metrics", args)
        item = next(e for e in observation.evidence if e.payload["metric"] == "pool_usage")
        rid = next(o.reference_id for o in item.reference_options if o.field_path == "value")
        model = Responses([selected("get_service_metrics", args), AIMessage(content=json.dumps({
            "findings": [{"statement": "A pool usage sample", "refs": [{"reference_id": rid}]}]}))])
        result = investigate(model, ex.provider, P, limits=Limits(model_calls=3, tool_calls=6, reserve_model_calls=0), max_steps=3)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["observation_coverage"], "limited_by_truncation")
        self.assertIn("focused query", model.history[1][-1].content)
        self.assertEqual(result["run_summary"]["model_calls"], 2)
        self.assertEqual(result["run_summary"]["tool_calls"], 1)

    def test_empty_error_filter_feedback_allows_model_to_choose_warn_observation(self):
        ex = executor("case_003")
        broad = {**window(ex), "category": "resource"}
        observed = ex.execute("get_service_logs", broad)
        item = next(e for e in observed.evidence if e.payload.get("error_code") == "POOL_ACQUIRE_TIMEOUT")
        rid = next(o.reference_id for o in item.reference_options if o.field_path == "message")
        model = Responses([selected("get_service_logs", {**broad, "level": "ERROR"}),
            selected("get_service_logs", broad, "t2"), AIMessage(content=json.dumps({"findings": [
                {"statement": "Observed pool acquisition wait", "refs": [{"reference_id": rid}]}]}))])
        result = investigate(model, ex.provider, P, limits=Limits(model_calls=3, tool_calls=6, reserve_model_calls=0), max_steps=3)
        self.assertEqual(result["status"], "completed")
        self.assertIn("WARN/INFO", model.history[1][-1].content)
        self.assertIn("omitting level", model.history[1][-1].content)
        self.assertIn('"level": "ERROR"', model.history[1][-1].content)
        self.assertEqual(result["run_summary"]["model_calls"], 3)
        self.assertEqual(result["run_summary"]["tool_calls"], 2)
        self.assertEqual([e["status"] for e in result["events"] if e["type"] == "tool_result"], ["empty", "ok"])


if __name__ == "__main__":
    unittest.main()
