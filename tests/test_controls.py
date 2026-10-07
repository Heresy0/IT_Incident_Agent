import asyncio
import json
import io
import os
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from backend.router.research_router import router
from backend.auth import Principal, get_current_principal
from backend.service import get_workflow_service
from backend.service.run_store import MemoryRunStore
from backend.service.workflow_service import WorkflowService, CapacityExceeded
from mult_agents.graph import build_app
from mult_agents.harness.runtime import ExecutionError, Limits, RunContext, activate
from mult_agents.nodes import _derive_search_plan
from mult_agents.harness.coverage import assess_coverage
from tests.support import WEB, LOCAL, REQUEST, TestConfig, bundle, execute


class Controls(unittest.TestCase):
    def test_01_auth_failure_stops_provider_and_writer(self):
        agents = bundle()
        error = urllib.error.HTTPError("https://api.bocha.cn", 401, "unauthorized", {}, None)
        with patch.dict(os.environ, {"BOCHA_API_KEY": "test-only"}), patch("urllib.request.urlopen", side_effect=error) as http, patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, runtime, _ = execute(agents)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(runtime.counts["web_calls"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(agents.writer.calls, 0)
        self.assertIn("认证失败", result["final"])
        self.assertEqual(result["web_retrieval_stats"]["query_count"], 1)
        self.assertEqual(result["web_search_trace"][0]["error_code"], "AUTH_ERROR")

    def test_02_empty_results_are_partial_not_invented(self):
        agents = bundle({"route": "direct"})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, _, _ = execute(agents)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["retrieval_errors"])
        self.assertEqual(agents.writer.calls, 0)
        self.assertEqual(agents.analyst.calls, 0)

    def test_03_rate_limit_has_bounded_retries(self):
        from mult_agents.tools import bocha_web_search_records
        runtime = RunContext()
        error = urllib.error.HTTPError("https://api.bocha.cn", 429, "limited", {}, None)
        with patch.dict(os.environ, {"BOCHA_API_KEY": "test-only"}), patch("urllib.request.urlopen", side_effect=error) as http, patch("mult_agents.tools.time.sleep"), activate(runtime):
            with self.assertRaises(ExecutionError) as raised:
                bocha_web_search_records("query")
        self.assertEqual(raised.exception.code, "RATE_LIMIT")
        self.assertEqual(http.call_count, 3)
        self.assertEqual(runtime.counts["web_calls"], 3)

    def test_04_invalid_analysis_repairs_once_then_fails_closed(self):
        agents = bundle({"invalid_role": "analyst"})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            result, _, _ = execute(agents)
        self.assertEqual(agents.analyst.calls, 2)
        self.assertEqual(agents.writer.calls, 0)
        self.assertEqual(result["termination_reason"], "MODEL_OUTPUT_INVALID")
        self.assertFalse(result["verified_findings"])

    def test_05_invalid_citation_and_unsupported_claim_are_removed(self):
        for scenario in ({"analysis_sources": ["WEB9_9-9"]}, {"reject_support": True}, {"bad_layout": True},
                         {"scout_reject": True}, {"bad_quote": True}, {"claim": "模拟方案 A 支持私有部署吗？"}):
            with self.subTest(scenario=scenario):
                agents = bundle(scenario)
                with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
                    result, _, _ = execute(agents)
                self.assertNotIn("WEB9_9-9", result["final"])
                self.assertNotIn("c_bad", result["final"])
                self.assertNotEqual(result["status"], "completed")
                if scenario.get("reject_support") or scenario.get("bad_quote"):
                    self.assertNotIn("模拟方案 A 支持私有部署。", result["final"])

    def test_06_budget_is_atomic_and_supplement_does_not_repeat(self):
        runtime = RunContext(limits=Limits(tool_calls=3))
        def reserve(_):
            try:
                runtime.reserve("local")
                return True
            except ExecutionError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(20))), 3)
        self.assertEqual(runtime.counts["tool_calls"], 3)
        agents = bundle({"needs_more": True, "supplement_query": "模拟方案的部署说明"})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]) as web, patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            result, _, _ = execute(agents, max_iterations=1)
        self.assertEqual(web.call_count, 1)
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(result["status"], "partial")
        reflection = agents.planner.prompts[-1]
        self.assertIn("补搜计划", reflection[0].content)
        low = bundle()
        result, runtime, _ = execute(low, limits=Limits(model_calls=3))
        self.assertLessEqual(runtime.counts["model_calls"], 3)
        self.assertEqual(result["termination_reason"], "BUDGET_EXCEEDED")

    def test_07_runs_use_unique_checkpoint_threads(self):
        agents = bundle({"route": "direct"})
        graph = build_app(agents, InMemorySaver())
        service = WorkflowService("unused", store=MemoryRunStore(), workflow=graph, config=TestConfig())
        try:
            first = service.start_run({**REQUEST, "query": "你好"})
            second = service.start_run({**REQUEST, "query": "另一个问题"})
            asyncio.run(service.wait_result(first)); asyncio.run(service.wait_result(second))
            self.assertNotEqual(first, second)
            one = graph.get_state({"configurable": {"thread_id": first}}).values
            two = graph.get_state({"configurable": {"thread_id": second}}).values
            self.assertEqual(one["query"], "你好")
            self.assertEqual(two["query"], "另一个问题")
        finally:
            service.close()

    def test_08_parallel_join_runs_judgment_and_analysis_once(self):
        agents = bundle()
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            result, runtime, events = execute(agents)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(agents.analyst.calls, 1)
        starts = [e["name"] for e in events if e["type"] == "call_start" and e["kind"] == "node"]
        self.assertEqual(starts.count("deep_dive"), 1)
        self.assertEqual(starts.count("verify"), 1)
        self.assertEqual(runtime.tokens["known_calls"], runtime.counts["model_calls"])
        self.assertIn("WEB1_1-1", result["final"])
        refs = result["final"].split("## 参考资料")[1]
        self.assertNotIn("LOC1_1-1", refs)
        plan = _derive_search_plan([{"id": "a", "search_queries": ["甲", "甲二"]}, {"id": "b", "search_queries": ["乙"]}], [], [], "原问题")
        self.assertEqual([q["query"] for q in plan[:2]], ["甲", "乙"])

    def test_09_capacity_rejects_before_queue_or_sse_headers(self):
        entered, release = Event(), Event()
        class BlockingGraph:
            def stream(self, state, config, **kwargs):
                entered.set(); release.wait(5)
                yield {"direct_answer": {"final": "done", "intent": "direct", "status": "completed"}}
        store = MemoryRunStore()
        service = WorkflowService("unused", store=store, workflow=BlockingGraph(), config=TestConfig(), max_concurrency=1)
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_workflow_service] = lambda: service
        app.dependency_overrides[get_current_principal] = lambda: Principal(REQUEST['tenant_id'], REQUEST['user_id'])
        try:
            run_id = service.start_run(REQUEST)
            self.assertTrue(entered.wait(2))
            with TestClient(app) as client:
                response = client.post("/api/v1/research/stream", json=REQUEST)
                self.assertEqual(response.status_code, 429)
            self.assertEqual(len(store.runs), 1)
            release.set(); asyncio.run(service.wait_result(run_id))
        finally:
            release.set(); service.close()

    def test_10_sse_replay_does_not_invoke_again(self):
        agents = bundle({"route": "direct"})
        store = MemoryRunStore()
        service = WorkflowService("unused", store=store, workflow=build_app(agents, InMemorySaver()), config=TestConfig())
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_workflow_service] = lambda: service
        app.dependency_overrides[get_current_principal] = lambda: Principal(REQUEST['tenant_id'], REQUEST['user_id'])
        try:
            with TestClient(app) as client:
                response = client.post("/api/v1/research/stream", json={**REQUEST, "query": "你好"})
                self.assertEqual(response.status_code, 200)
                run_id = response.headers["X-Research-Run-ID"]
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                self.assertEqual(events[-1]["type"], "final")
                replay = client.get(f"/api/v1/research/runs/{run_id}/events?after_seq=1")
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(agents.direct_responder.calls, 1)
                self.assertEqual(sum(e["type"] == "final" for e in store.history[run_id]), 1)
                self.assertEqual(client.get(f"/api/v1/research/runs/{run_id}").json()["status"], "completed")
                self.assertEqual(client.get(f"/api/v1/research/runs/{run_id}/events", headers={"Last-Event-ID": "-1"}).status_code, 400)
        finally:
            service.close()

    def research(self, scenario=None, **kwargs):
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            return execute(bundle(scenario), **kwargs)[0]

    def test_11_reviewed_claims_determine_question_coverage(self):
        result = self.research({"sub_questions": ["部署要求", "未说明的成本"], "question_ids": ["q1"]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["status"] for r in result["question_coverage"]], ["answered", "unanswered"])
        self.assertEqual(result["question_coverage"][0]["question"], "研究模拟方案的部署")
        self.assertIn("未说明的成本", " ".join(result["missing_gaps"]))
        self.assertIn("## 研究问题覆盖", result["final"])
        rejected = self.research({"reject_support": True})
        self.assertEqual(rejected["question_coverage"][0]["status"], "unanswered")
        self.assertEqual(rejected["question_coverage"][0]["claim_ids"], [])

    def test_12_duplicate_and_unlinked_assessments_fail_closed(self):
        task = {"question_id": "q1", "question": "部署"}
        finding = {"claim_id": "c1", "question_ids": ["q1"]}
        assessment = {"question_id": "q1", "status": "answered", "claim_ids": ["c1"]}
        for rows, claims in (([assessment] * 2, [finding]), ([assessment], [{**finding, "question_ids": ["q2"]}]),
                             ([{**assessment, "claim_ids": ["fake"]}], [finding]), ([], [finding])):
            self.assertEqual(assess_coverage([task], rows, claims)[0]["status"], "unanswered")
        partial = assess_coverage([task], [{**assessment, "claim_ids": ["c1", "missing"]}], [finding])
        self.assertEqual(partial[0]["status"], "partial")

    def test_13_unverified_analysis_never_appears_as_answered(self):
        result = self.research({"invalid_verify": True})
        self.assertEqual(result["termination_reason"], "MODEL_OUTPUT_INVALID")
        self.assertEqual(result["question_coverage"][0]["status"], "unanswered")
        self.assertEqual(result["coverage_stage"], "not_verified")
        self.assertNotIn("：已覆盖", result["final"])

    def test_14_qualifiers_and_duplicate_decisions_are_rejected(self):
        for scenario in ({"claim": "方案 A 支持私有部署。"}, {"claim": "模拟方案 A 通常支持私有部署。"}, {"duplicate_decisions": True}):
            with self.subTest(scenario=scenario):
                result = self.research(scenario)
                self.assertFalse(result["verified_findings"])
                self.assertEqual(len(result["verification_rejections"]), 1)
                self.assertEqual(result["question_coverage"][0]["status"], "unanswered")

    def test_15_final_review_clears_resolved_provisional_gaps(self):
        result = self.research({"needs_more": True, "analysis_gaps": ["初步判断尚需核对"]})
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["missing_gaps"])
        still_missing = self.research({"review_gaps": ["尚未说明模型费用"]})
        self.assertEqual(still_missing["status"], "partial")
        self.assertIn("尚未说明模型费用", still_missing["final"])
        stale = self.research({"review_gaps": ["研究问题 q1：旧的未覆盖判断"]})
        self.assertEqual(stale["status"], "completed")
        self.assertEqual(stale["final"].count("## 核心结论"), 1)

    def test_37_rejected_extra_claim_is_audit_history_not_an_unanswered_task(self):
        findings = [
            {"claim_id": "c_1", "claim": "模拟方案 A 支持私有部署。", "source_ids": ["WEB1_1-1"], "question_ids": ["q1"], "kind": "fact"},
            {"claim_id": "c_extra", "claim": "模拟方案 A 整体月成本为 1000 元。", "source_ids": ["WEB1_1-1"], "question_ids": ["q1"], "kind": "fact"},
        ]
        scenario = {"findings": findings, "reject_ids": ["c_extra"],
                    "review_coverage": [{"question_id": "q1", "status": "answered", "claim_ids": ["c_1"], "reason": "部署问题已回答"}]}
        query = "研究模拟方案 A 是否支持私有部署。只核实这一项。"
        result = self.research(scenario, query=query)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["missing_gaps"], [])
        self.assertEqual(result["verification_rejections"][0]["claim_id"], "c_extra")
        self.assertIn("## 复核排除", result["final"])
        self.assertNotIn("整体月成本为 1000 元", result["final"])
        self.assertNotIn("尚未完整回答", result["final"])
        necessary = self.research({**scenario, "review_coverage": [{"question_id": "q1", "status": "answered", "claim_ids": ["c_extra"], "reason": "fixture"}]}, query=query)
        self.assertEqual(necessary["status"], "partial")
        self.assertEqual(necessary["question_coverage"][0]["status"], "unanswered")
        self.assertTrue(necessary["missing_gaps"])

    def test_16_selection_cannot_bypass_research_gate(self):
        agents = bundle({"route": "direct"})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            result, _, _ = execute(agents, query="企业平台选型：比较 A 和 B")
        self.assertEqual(result["intent"], "multiagent")
        self.assertEqual(agents.direct_responder.calls, 0)

    def test_17_coverage_survives_sync_result_get_and_sse_replay(self):
        agents = bundle({"sub_questions": ["部署", "成本"], "question_ids": ["q1"]})
        store = MemoryRunStore()
        service = WorkflowService("unused", store=store, workflow=build_app(agents, InMemorySaver()), config=TestConfig())
        app = FastAPI(); app.include_router(router)
        app.dependency_overrides[get_workflow_service] = lambda: service
        app.dependency_overrides[get_current_principal] = lambda: Principal(REQUEST['tenant_id'], REQUEST['user_id'])
        try:
            with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]), TestClient(app) as client:
                response = client.post("/api/v1/research/run", json=REQUEST)
                self.assertEqual(response.status_code, 200)
                result = response.json()
                run_id = result["run_id"]
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["run_summary"]["question_coverage"], {"total": 2, "answered": 1, "partial": 0, "unanswered": 1, "stage": "verified"})
                stored = client.get(f"/api/v1/research/runs/{run_id}").json()["result"]
                replay = client.get(f"/api/v1/research/runs/{run_id}/events").text
                final = [json.loads(line[6:]) for line in replay.splitlines() if line.startswith("data: ")][-1]
                for field in ("question_coverage", "coverage_stage", "missing_gaps", "verification_rejections"):
                    self.assertEqual(result[field], stored[field])
                    self.assertEqual(result[field], final[field])
                self.assertEqual(agents.analyst.calls, 1)
        finally:
            service.close()

    def test_18_review_can_prune_redundant_citations_but_cannot_add_sources(self):
        result = self.research({"analysis_sources": ["WEB1_1-1", "LOC1_1-1"], "review_sources": ["WEB1_1-1"]})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verified_findings"][0]["source_ids"], ["WEB1_1-1"])
        extra = self.research({"analysis_sources": ["WEB1_1-1"], "review_sources": ["LOC1_1-1"]})
        self.assertFalse(extra["verified_findings"])
        empty = self.research({"review_sources": []})
        self.assertFalse(empty["verified_findings"])

    def test_19_local_only_never_calls_web_including_supplement(self):
        agents = bundle({"needs_more": True})
        with patch("mult_agents.nodes.bocha_web_search_records", side_effect=AssertionError("local-only must not search web")) as web, patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            result, runtime, _ = execute(agents, max_iterations=1, query="只依据本地知识库研究模拟方案的部署")
        self.assertEqual(web.call_count, 0)
        self.assertEqual(runtime.counts["web_calls"], 0)
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(result["web_retrieval_stats"]["query_count"], 0)
        self.assertFalse(result["retrieval_errors"])
        self.assertEqual(len(result["evidence_pool"]), 1)

    def test_20_trusted_simulation_scope_is_preserved_by_renderer(self):
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[{**WEB, "is_simulated": True}]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, _, _ = execute(bundle({"claim": "方案 A 支持私有部署。"}))
        self.assertEqual(result["status"], "completed")
        self.assertIn("【模拟事实】方案 A 支持私有部署。", result["final"])
        self.assertIn("不描述真实商业产品", result["final"])

    def test_21_verbatim_quote_repair_is_bounded_and_counted(self):
        agents = bundle({"bad_quote_once": True})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[LOCAL]):
            result, runtime, events = execute(agents)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(agents.evidence_judge.calls, 3)
        self.assertEqual(sum(e["type"] == "validation_repair" and e["node"] == "verify" for e in events), 1)
        self.assertEqual(runtime.counts["model_calls"], 9)

    def test_22_quote_selector_uses_actual_text_and_rejects_fake_or_conflicting_quotes(self):
        result = self.research({"quote_selector": True})
        self.assertEqual(result["status"], "completed")
        quote = result["verified_findings"][0]["support"]["evidence_quotes"][0]
        self.assertEqual(quote["quote"], WEB["snippet"])
        for scenario in ({"quote_selector": True, "bad_quote_id": True}, {"quote_selector": True, "bad_quote": True}):
            rejected = self.research(scenario)
            self.assertFalse(rejected["verified_findings"])

    def test_23_background_facts_cannot_complete_an_explicit_advice_request(self):
        query = "请给出模拟方案 A 的选型建议"
        background = self.research(query=query)
        self.assertEqual(background["status"], "partial")
        self.assertEqual(background["question_coverage"][0]["status"], "partial")
        advice = self.research({"kind": "recommendation", "claim": "建议优先验证模拟方案 A，因为其支持私有部署。"}, query=query)
        self.assertEqual(advice["status"], "completed")


    def test_24_budget_ceiling_is_not_verified_solution_cost(self):
        source = {**WEB, "snippet": "团队每月预算上限为 2000 元；资料没有说明这笔预算是否包含服务器和模型费用。"}
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[source]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            rejected, _, _ = execute(bundle({"claim": "方案 A 满足企业预算。"}))
            uncertain, _, _ = execute(bundle({"claim": "无法确认方案 A 是否符合企业预算。"}))
        self.assertFalse(rejected["verified_findings"])
        self.assertIn("预算", str(rejected["verification_rejections"]))
        self.assertEqual(uncertain["status"], "completed")


    def test_25_bocha_quota_failure_is_not_auth_and_blocks_further_searches(self):
        from mult_agents.tools import bocha_web_search_records
        body = io.BytesIO(json.dumps({"code": "403", "message": "You do not have enough money or package quota"}).encode())
        error = urllib.error.HTTPError("https://api.bochaai.com/v1/web-search", 403, "forbidden", {}, body)
        with patch.dict(os.environ, {"BOCHA_API_KEY": "test-only"}), patch("urllib.request.urlopen", side_effect=error) as http, patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, runtime, _ = execute(bundle())
            with activate(runtime), self.assertRaises(ExecutionError) as raised:
                bocha_web_search_records("another query")
        self.assertEqual(raised.exception.code, "QUOTA_EXCEEDED")
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0].full_url, "https://api.bochaai.com/v1/web-search")
        self.assertEqual(runtime.counts["web_calls"], 1)
        self.assertEqual(result["web_search_trace"][0]["error_code"], "QUOTA_EXCEEDED")
        self.assertIn("套餐额度不足", result["final"])
        self.assertNotIn("认证失败", result["final"])

    def test_26_bocha_other_forbidden_errors_remain_auth(self):
        from mult_agents.tools import bocha_web_search_records
        for body in (b'{"message":"Invalid API key"}', b'<html>Forbidden</html>', b'[]', b''):
            with self.subTest(body=body):
                error = urllib.error.HTTPError("https://api.bochaai.com/v1/web-search", 403, "forbidden", {}, io.BytesIO(body))
                with patch.dict(os.environ, {"BOCHA_API_KEY": "test-only"}), patch("urllib.request.urlopen", side_effect=error) as http, activate(RunContext()):
                    with self.assertRaises(ExecutionError) as raised:
                        bocha_web_search_records("query")
                self.assertEqual(raised.exception.code, "AUTH_ERROR")
                self.assertEqual(http.call_count, 1)

    def test_27_compact_selection_preserves_ten_long_sources_through_the_graph(self):
        records = [{**WEB, "url": f"https://example.com/plan-a/{i}", "title": f"实际标题 {i}",
                    "snippet": WEB["snippet"] + str(i) + "原始资料内容。" * 300} for i in range(10)]
        agents = bundle({"quote_selector": True})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=records), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, _, events = execute(agents)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["evidence_pool"]), 10)
        for expected, actual in zip(records, result["evidence_pool"]):
            self.assertEqual(actual["url"], expected["url"])
            self.assertEqual(actual["title"], expected["title"])
            self.assertEqual(actual["snippet"], expected["snippet"][:1200])
        for role in ("scout_web", "evidence_judge"):
            self.assertIn('"additionalProperties": false', agents.__dict__[role].prompts[0][-1].content)
        self.assertFalse(any(e["type"] == "validation_repair" for e in events))
        self.assertEqual(agents.scout_web.calls, 1)

    def test_28_selection_contract_rejects_copied_fields_and_oversized_output(self):
        from mult_agents.harness.validation import parse_output
        invalid = [
            ("web_search", {"evidence": [{"source_id": "WEB1_1-1", "snippet": "模型改写的原文"}]}),
            ("local_rag", {"evidence": [{"source_id": "LOC1_1-1", "url": "https://invented.example"}]}),
            ("web_search", {"evidence": [{"source_id": "WEB1_1-1", "notes": "长" * 81}]}),
            ("web_search", {"evidence": [{"source_id": "WEB1_1-1"}] * 11}),
            ("deep_dive", {"evidence_pool": [{"source_id": "WEB1_1-1", "title": "伪造标题"}]}),
            ("deep_dive", {"evidence_pool": [{"source_id": "WEB1_1-1", "reliability_score": 2}]}),
        ]
        for node, payload in invalid:
            with self.subTest(node=node, payload=payload), self.assertRaises(ExecutionError) as raised:
                parse_output(json.dumps(payload, ensure_ascii=False), node)
            self.assertEqual(raised.exception.code, "MODEL_OUTPUT_INVALID")

    def test_29_bad_selections_remain_rejected_after_one_repair(self):
        for scenario in ({"scout_bad_id": True}, {"duplicate_selection": True}, {"conflicting_selection": True}, {"audit_bad_id": True}):
            agents = bundle(scenario)
            with self.subTest(scenario=scenario), patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
                result, _, events = execute(agents)
            self.assertFalse(result.get("verified_findings"))
            self.assertEqual(agents.writer.calls, 0)
            self.assertNotIn("WEB9_9-999", result["final"])
            role = agents.evidence_judge if scenario.get("audit_bad_id") else agents.scout_web
            self.assertEqual(role.calls, 2)
            failures = [e for e in events if e["type"] == "validation_failure"]
            self.assertEqual([e["attempt"] for e in failures], [1, 2])
            self.assertTrue(all(e["reason"] == "invalid_selection_or_quote" for e in failures))

    def test_30_valid_repaired_selection_keeps_original_evidence_and_counts_calls(self):
        agents = bundle({"scout_bad_id_once": True})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, runtime, events = execute(agents)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_pool"][0]["url"], WEB["url"])
        self.assertEqual(result["evidence_pool"][0]["snippet"], WEB["snippet"])
        self.assertEqual(agents.scout_web.calls, 2)
        self.assertEqual(runtime.counts["model_calls"], 8)
        outputs = [e for e in events if e["type"] == "model_output" and e["node"] == "web_search"]
        self.assertEqual([e["attempt"] for e in outputs], [1, 2])
        self.assertTrue(all(set(e) <= {"type", "node", "attempt", "output_chars", "finish_reason", "run_id", "trace_id", "timestamp"} for e in outputs))

    def test_31_negated_advice_does_not_downgrade_a_factual_question(self):
        from mult_agents.harness.coverage import requires_advice
        questions = [
            "Dify 是否提供 Docker Compose 自部署方式？给出来源引用；不要扩展到功能、性能或采购推荐。",
            "请核实部署方式，不要给出选型建议。",
            "请核实部署方式；无需推荐方案。",
        ]
        findings = [{"claim_id": "c1", "claim": "资料说明部署方式。", "kind": "fact", "question_ids": ["q1"]}]
        assessments = [{"question_id": "q1", "status": "answered", "claim_ids": ["c1"]}]
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(requires_advice(question))
                row = assess_coverage([{"question_id": "q1", "question": question}], assessments, findings)[0]
                self.assertEqual(row["status"], "answered")
        for question in ("请给出选型建议", "不要介绍背景；请给出选型建议", "无需讨论价格，请推荐方案"):
            with self.subTest(question=question):
                self.assertTrue(requires_advice(question))
                row = assess_coverage([{"question_id": "q1", "question": question}], assessments, findings)[0]
                self.assertEqual(row["status"], "partial")

    def test_32_official_origin_policy_rejects_forks_transcripts_and_spoofed_urls(self):
        from mult_agents.harness.source_policy import approved_source, requires_official_sources
        query = "以 Dify 官方文档或官方 GitHub 为依据核实部署方式"
        accepted = ["https://docs.dify.ai/en/self-host/deploy/quick-start/docker-compose",
                    "https://github.com/langgenius/dify", "https://github.com/langgenius/dify/tree/main/docker"]
        rejected = ["https://github.com/koorlan/dify/tree/main/docker", "https://github.com/langgenius/dify-fork",
                    "https://docs.dify.ai.evil.example/path", "https://docs.dify.ai@evil.example/path",
                    "https://evil.example/?url=https://docs.dify.ai", "https://github.com/langgenius/dify/issues/1",
                    "https://github.com/langgenius/dify/tree/../../other/repo", "https://docs.dify.ai:8443/path",
                    "http://docs.dify.ai/path", "https://m.blog.csdn.net/tutorial"]
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(approved_source({"url": url}, query))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(approved_source({"url": url, "title": "官方文档", "snippet": "官网链接 https://docs.dify.ai"}, query))
        self.assertFalse(approved_source({"url": accepted[0]}, "以 UnknownProduct 官方文档为依据"))
        self.assertFalse(requires_official_sources("不要官方文档，只查第三方教程"))

    def test_33_official_only_graph_cannot_complete_from_a_repository_fork(self):
        query = "只以 Dify 官方 GitHub 为依据核实部署方式"
        fork = {**WEB, "url": "https://github.com/koorlan/dify/tree/main/docker", "title": "官方部署文档"}
        agents = bundle()
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[fork]) as web, patch("mult_agents.nodes.search_knowledge_base_records", side_effect=AssertionError("unattributed local notes are not official sources")):
            result, _, events = execute(agents, query=query)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result.get("verified_findings"))
        self.assertEqual(agents.scout_web.calls, 0)
        self.assertEqual(agents.analyst.calls, 0)
        self.assertIn("官方来源", result["final"])
        self.assertTrue(all("site:" in call.args[0] for call in web.call_args_list))
        self.assertTrue(any(e["type"] == "source_policy" and e["dropped_count"] > 0 for e in events))
        official = {**WEB, "url": "https://github.com/langgenius/dify/tree/main/docker"}
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[official]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, _, _ = execute(bundle(), query=query)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_pool"][0]["url"], official["url"])

    def test_34_bocha_domain_scope_uses_native_include_not_only_query_text(self):
        from mult_agents.tools import bocha_web_search_records
        body = json.dumps({"data": {"webPages": {"value": []}}}).encode()
        for domains in (None, ["docs.dify.ai", "docs.dify.ai"]):
            with self.subTest(domains=domains), patch.dict(os.environ, {"BOCHA_API_KEY": "test-only"}), patch("urllib.request.urlopen", return_value=io.BytesIO(body)) as http, activate(RunContext()):
                records = bocha_web_search_records("Dify Docker Compose site:docs.dify.ai", include_domains=domains)
                request = json.loads(http.call_args.args[0].data)
                self.assertEqual(records, [])
                self.assertEqual(http.call_count, 1)
                if domains:
                    self.assertEqual(request["include"], "docs.dify.ai")
                    self.assertNotIn("site:", request["query"])
                else:
                    self.assertNotIn("include", request)
                    self.assertIn("site:", request["query"])

    def test_35_explicit_single_fact_scope_cannot_expand_into_extra_required_tasks(self):
        query = "只核实这一项：模拟方案 A 是否支持私有部署？不要给出选型建议。"
        agents = bundle({"sub_questions": ["原问题", "官方文档是否说明", "官方仓库是否包含配置"]})
        with patch("mult_agents.nodes.bocha_web_search_records", return_value=[WEB]), patch("mult_agents.nodes.search_knowledge_base_records", return_value=[]):
            result, _, events = execute(agents, query=query)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["research_tasks"], [{"question_id": "q1", "question": query}])
        self.assertEqual(len(result["question_coverage"]), 1)
        self.assertTrue(any(e["type"] == "plan_scope" and e["task_count"] == 1 for e in events))

    def test_36_schema_repair_reports_field_paths_without_logging_input_text(self):
        from types import SimpleNamespace
        from langchain_core.messages import AIMessage
        from mult_agents.nodes import _invoke_json_agent
        copied_text = "PRIVATE_INPUT " * 50
        invalid = {"decisions": [{"claim_id": "c1", "verdict": "supported", "evidence_quotes": [{"source_id": "WEB1_1-1", "quote": copied_text}]}]}
        responses = iter([invalid, {"decisions": []}])
        prompts, events = [], []
        def invoke(payload):
            prompts.append(payload["messages"])
            return {"messages": [AIMessage(content=json.dumps(next(responses)))]}
        runtime = RunContext(emit=events.append)
        with activate(runtime):
            payload, _, _ = _invoke_json_agent({}, "fixture", SimpleNamespace(invoke=invoke), "fixture", "verify")
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(runtime.counts["model_calls"], 2)
        failure = next(e for e in events if e["type"] == "validation_failure")
        self.assertEqual(failure["schema_errors"], [{"path": "decisions.0.evidence_quotes.0.quote", "type": "string_too_long"}])
        self.assertNotIn("PRIVATE_INPUT", json.dumps(events))
        self.assertIn("decisions.0.evidence_quotes.0.quote", prompts[1][-1].content)


if __name__ == "__main__":
    unittest.main()
