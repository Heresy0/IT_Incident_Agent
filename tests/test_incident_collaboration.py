import copy
import io
import json
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from mult_agents.harness.runtime import Limits
from mult_agents.incident.collaboration import collaborate
from mult_agents.incident.collaboration_fake import scripted_models
from mult_agents.incident.providers import FixtureProvider
from tests.test_incident_tools import P


class AlterOutput:
    """Mutate real scripted role outputs to exercise coordinator trust boundaries."""
    def __init__(self, model, alter):
        self.model, self.alter, self.calls = model, alter, 0

    def bind_tools(self, tools):
        return AlterOutput(self.model.bind_tools(tools), self.alter)

    def invoke(self, messages):
        self.calls += 1
        response = self.model.invoke(messages)
        return self.alter(response, messages, self.calls)


def json_response(value):
    return AIMessage(content=json.dumps(value, ensure_ascii=False))


class IncidentCollaborationTests(unittest.TestCase):
    def run_case(self, case="case_001", models=None, **kwargs):
        return collaborate(models or scripted_models(), FixtureProvider(case, P), P, **kwargs)

    def test_simple_cases_skip_knowledge_and_fit_explicit_small_budget(self):
        for case in ("case_001", "case_002"):
            with self.subTest(case=case):
                result = self.run_case(case, limits=Limits(model_calls=6, tool_calls=2,
                    reserve_model_calls=3, reserve_seconds=20))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["review_status"], "passed")
                self.assertEqual([t["role"] for t in result["task_results"]], ["investigation"])
                self.assertEqual(result["run_summary"]["model_calls"], 6)
                self.assertEqual(result["run_summary"]["tool_calls"], 2)
                self.assertEqual(result["rework_rounds"], 0)
                self.assertEqual(len(result["reviews"]), 1)
                self.assertEqual(result["output"]["hypotheses"][0]["status"], "supported")

    def test_conflicting_history_drives_one_specific_rework_and_revision(self):
        result = self.run_case("case_003")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["rework_rounds"], 1)
        self.assertEqual(result["supervisor_decisions"], 3)
        self.assertEqual(len(result["reviews"]), 2)
        self.assertEqual(result["run_summary"]["model_calls"], 13)
        self.assertEqual(result["run_summary"]["tool_calls"], 5)
        self.assertEqual([t["role"] for t in result["task_results"]], ["investigation", "knowledge", "investigation"])
        self.assertEqual(result["task_results"][-1]["challenge_id"], "C1")
        self.assertEqual(sum(e["type"] == "review_completed" for e in result["events"]), 2)
        self.assertIn("数据库 CPU", result["drafts"][0]["draft"]["hypotheses"][0]["cause"])
        self.assertIn("连接池", result["output"]["hypotheses"][0]["cause"])
        request = result["reviews"][0]["review"]["request_evidence"]
        self.assertTrue(request["missing_observation"] and request["proposed_check"] and request["expected_value"])
        kinds = {e["evidence_id"]: e["kind"] for e in result["evidence"]}
        for finding in result["output"]["findings"]:
            self.assertTrue(all(kinds[r["evidence_id"]] == "observation" for r in finding["refs"]))
        for key in ("challenge", "request_evidence", "revise", "accept"):
            self.assertIn(key, [n["type"] for n in result["negotiation"]])

    def test_one_context_counts_all_steps_and_isolates_task_conversations(self):
        models = scripted_models()
        result = self.run_case("case_003", models)
        events = result["events"]
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        starts = [e for e in events if e["type"] == "call_start"]
        known = {e["span_id"] for e in starts}
        for event in events:
            self.assertEqual(event["run_id"], result["run_id"])
            if event["type"] == "call_start" and event["kind"] != "workflow":
                self.assertIn(event["parent_span_id"], known)
        for kind in ("model", "tool"):
            self.assertEqual(sum(e["kind"] == kind for e in starts), result["run_summary"][kind + "_calls"])
        self.assertEqual(sum(len(m.histories) for m in models.values()), result["run_summary"]["model_calls"])
        inv_histories = models["investigation"].histories
        initial = [json.loads(next(m.content for m in history if isinstance(m, HumanMessage)))
                   for history in inv_histories]
        rework = initial[-2]
        self.assertEqual(len(inv_histories[-2]), 2)
        self.assertEqual(rework["objective"]["challenge"]["challenge_id"], "C1")
        self.assertEqual(len(rework["objective"]["evidence"]), 1)
        self.assertNotIn("数据库 CPU 过高导致延迟", inv_histories[-2][1].content)
        review_input = json.loads(models["reviewer"].histories[-1][1].content)["input"]
        self.assertEqual(len(review_input["collection"]), 5)
        self.assertTrue(all("truncated" in row and "query_profile" in row for row in review_input["collection"]))

    def test_supervisor_cannot_finish_before_review(self):
        models = scripted_models()
        models["supervisor"] = AlterOutput(models["supervisor"], lambda *_:
            json_response({"action": "finish", "reason": "pretend success"}))
        result = self.run_case(models=models)
        self.assertEqual(result["run_summary"]["termination_reason"], "UNREVIEWED_FINISH")
        self.assertEqual(result["review_status"], "not_performed")
        self.assertEqual(result["output"]["findings"], [])
        self.assertEqual(result["run_summary"]["tool_calls"], 0)

    def test_unknown_role_and_identity_fields_are_rejected_before_dispatch(self):
        for task in ({"role": "administrator", "goal": "expand"},
                     {"role": "investigation", "goal": "expand", "tenant_id": "other"}):
            with self.subTest(task=task):
                models = scripted_models()
                models["supervisor"] = AlterOutput(models["supervisor"], lambda *_:
                    json_response({"action": "dispatch", "reason": "expand", "tasks": [task]}))
                result = self.run_case(models=models)
                self.assertEqual(result["run_summary"]["termination_reason"], "MODEL_OUTPUT_INVALID")
                self.assertEqual(result["repairs"], 1)
                self.assertEqual(result["task_results"], [])
                self.assertEqual(result["run_summary"]["tool_calls"], 0)

    def test_duplicate_tasks_do_not_consume_tool_budget(self):
        models = scripted_models()
        models["supervisor"] = AlterOutput(models["supervisor"], lambda *_:
            json_response({"action": "dispatch", "reason": "duplicate", "tasks": [
                {"role": "investigation", "goal": "Current checks"},
                {"role": "investigation", "goal": "  current   CHECKS "}]}))
        result = self.run_case(models=models)
        self.assertEqual(result["run_summary"]["termination_reason"], "DUPLICATE_TASK")
        self.assertEqual(result["run_summary"]["tool_calls"], 0)

    def test_forged_review_evidence_cannot_approve_draft(self):
        def forge(response, *_):
            data = json.loads(response.content)
            data["assessments"][0]["evidence_ids"] = ["invented-source"]
            return json_response(data)
        models = scripted_models()
        models["reviewer"] = AlterOutput(models["reviewer"], forge)
        result = self.run_case(models=models)
        self.assertEqual(result["run_summary"]["termination_reason"], "MODEL_OUTPUT_INVALID")
        self.assertEqual(result["repairs"], 1)
        self.assertEqual(result["review_status"], "not_performed")
        self.assertEqual(result["output"]["findings"], [])
        self.assertEqual(result["output"]["hypotheses"][0]["status"], "tentative")

    def test_history_only_cause_is_downgraded_even_if_reviewer_accepts(self):
        def accept(response, *_):
            data = json.loads(response.content)
            data.pop("request_evidence", None)
            for row in data["assessments"]:
                row["verdict"] = "supported"
            return json_response(data)
        models = scripted_models()
        models["reviewer"] = AlterOutput(models["reviewer"], accept)
        result = self.run_case("case_003", models)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["output"]["hypotheses"][0]["status"], "unresolved")
        self.assertEqual(result["run_summary"]["termination_reason"], "REVIEW_INCOMPLETE")
        self.assertTrue(any(e["type"] == "review_gate" for e in result["events"]))

    def test_second_review_cannot_launch_a_second_rework(self):
        saved = {}
        def repeat_request(response, *_):
            data = json.loads(response.content)
            if data.get("request_evidence"):
                saved["request"] = copy.deepcopy(data["request_evidence"])
            else:
                data["request_evidence"] = saved["request"]
                for row in data["assessments"]:
                    if row["target_id"] == "H1":
                        row["verdict"] = "uncertain"
            return json_response(data)
        models = scripted_models()
        models["reviewer"] = AlterOutput(models["reviewer"], repeat_request)
        result = self.run_case("case_003", models)
        self.assertEqual(result["run_summary"]["termination_reason"], "REWORK_LIMIT")
        self.assertEqual(result["rework_rounds"], 1)
        self.assertEqual(len(result["reviews"]), 2)
        self.assertEqual(len(result["task_results"]), 3)
        self.assertEqual(result["status"], "partial")

    def test_small_budget_keeps_review_and_refuses_unaffordable_rework(self):
        result = self.run_case("case_003", limits=Limits(model_calls=8, tool_calls=5,
            reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["run_summary"]["termination_reason"], "BUDGET_EXCEEDED")
        self.assertEqual(len(result["reviews"]), 1)
        self.assertEqual(result["rework_rounds"], 0)
        self.assertEqual(result["run_summary"]["model_calls"], 8)
        self.assertEqual(result["output"]["hypotheses"][0]["status"], "refuted")

    def test_schema_repair_allowance_is_shared_across_leaf_and_reviewer(self):
        def broken_final(response, _, calls):
            return AIMessage(content="invalid JSON") if calls == 2 else response
        models = scripted_models()
        models["investigation"] = AlterOutput(models["investigation"], broken_final)
        models["reviewer"] = AlterOutput(models["reviewer"], lambda *_: AIMessage(content="invalid JSON"))
        result = self.run_case(models=models)
        self.assertEqual(result["repairs"], 1)
        self.assertEqual(sum(e["type"] == "validation_repair" for e in result["events"]), 1)
        self.assertEqual(result["run_summary"]["termination_reason"], "MODEL_OUTPUT_INVALID")
        self.assertEqual(result["output"]["findings"], [])

    def test_dispatch_limit_stops_repeated_distinct_goals_without_unreviewed_facts(self):
        models = scripted_models()
        models["supervisor"] = AlterOutput(models["supervisor"], lambda _, __, calls:
            json_response({"action": "dispatch", "reason": "keep collecting", "tasks": [
                {"role": "investigation", "goal": f"Check round {calls}"}]}))
        result = self.run_case(models=models)
        self.assertEqual(result["supervisor_decisions"], 4)
        self.assertEqual(result["run_summary"]["termination_reason"], "SUPERVISOR_LIMIT")
        self.assertEqual(result["review_status"], "not_performed")
        self.assertEqual(result["output"]["findings"], [])
        self.assertEqual(result["run_summary"]["tool_calls"], 2)

    def test_live_cli_requires_explicit_budgets_before_building_models(self):
        from scripts.investigate_incident import main
        for args in ([], ["--model-budget", "17", "--tool-budget", "2"]):
            with self.subTest(args=args), patch("mult_agents.incident.agents.build_roles") as build, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(["--live", "--workflow", "collaboration", *args])
                self.assertEqual(exc.exception.code, 2)
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
