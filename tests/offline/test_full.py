"""Real project modules, fixed agent outputs. Requires project dependencies.

This module is loaded only by run.py --full after its dependency preflight.
No fake AgentScope/OR-Tools modules are substituted to make tests pass.
"""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import httpx
from agentscope.message import Msg
from agents.skill_dispatcher import SkillDispatcher
from utils.execution_tracer import ExecutionTracer
from utils.pi_durable_client import DurableSubmitRejected, PiDurableClient, PiDurableUnavailable, DurableTurnTimeout
from support import IsolatedCase, ScriptedAgent

PROJECT = Path(__file__).resolve().parents[2]


def task(name, priority=1):
    return {"agent_name": name, "priority": priority, "reason": "offline test", "expected_output": "JSON"}


def decision(schedule, **entities):
    return {"agent_schedule": schedule, "key_entities": entities,
            "rewritten_query": "离线固定输入", "complexity": "standard"}


class DispatcherTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        # Keep the real tracer implementation, but explicitly disable PG.
        self.tracer_patch = patch("agents.skill_dispatcher.ExecutionTracer",
                                  side_effect=lambda: ExecutionTracer(postgres_config=False))
        self.tracer_patch.start()
        self.addCleanup(self.tracer_patch.stop)

    def test_gate_stops_execution_then_complete_input_runs(self):
        agent = ScriptedAgent([{"itinerary": {"daily_plans": []}}])
        d = SkillDispatcher(agent_registry={"itinerary_planning": agent})
        raw = decision([task("itinerary_planning", 2)], destination="杭州", duration_days=3)
        result = self.async_run(d.reply(Msg(name="test", content=json.dumps(raw), role="user")))
        gate = json.loads(result.content)
        self.assertEqual(gate["status"], "needs_input")
        self.assertEqual(gate["missing_info"], ["start_date"])
        self.assertEqual(agent.inputs, [])
        raw["key_entities"]["start_date"] = "2027-05-10"
        self.assertEqual(d.plan_batches(raw)["kind"], "batches")

    def test_required_fields_and_derived_end_date(self):
        d = SkillDispatcher()
        schedule = [task("conflict_check", 2)]
        complete = decision(schedule, destination="杭州", start_date="2027-05-10", duration_days=3)
        self.assertIsNone(d._check_preconditions(complete, schedule))
        missing = decision(schedule, destination="杭州")
        self.assertEqual(set(d._check_preconditions(missing, schedule)["missing_info"]), {"start_date", "end_date"})

    def test_consumes_and_fast_path(self):
        d = SkillDispatcher()
        d._skill_meta = {"a": {"consumes": []}, "b": {"consumes": ["a"]}}
        raw = decision([task("a")])
        raw["complexity"] = "simple"
        self.assertTrue(d.plan_batches(raw)["fast_path"])
        raw["agent_schedule"] = [task("b")]
        self.assertFalse(d.plan_batches(raw)["fast_path"])
        prev = [{"agent_name": "a", "result": {"x": 1}}, {"agent_name": "private", "result": {"secret": "test"}}]
        self.assertEqual(d._filter_previous_results("b", prev), prev[:1])

    def test_replan_reuses_success_only_and_needs_consumer(self):
        d = SkillDispatcher()
        d._last_results = {"a": {"status": "success"}, "b": {"status": "error"}}
        schedule = [task("a"), task("b"), task("analysis", 2)]
        reused, remaining = d._apply_replan(schedule)
        self.assertEqual([r["agent_name"] for r in reused], ["a"])
        self.assertEqual([r["agent_name"] for r in remaining], ["b", "analysis"])
        self.assertEqual(d._apply_replan(schedule[:2]), ([], schedule[:2]))

    def test_parallel_batch_and_p2_dependency(self):
        async def scenario():
            started, both_started = set(), asyncio.Event()
            calls = []
            class Agent:
                def __init__(self, name): self.name = name
                async def reply(self, message):
                    payload = json.loads(message.content)
                    calls.append(self.name)
                    if self.name in ("a", "b"):
                        started.add(self.name)
                        if len(started) == 2: both_started.set()
                        await both_started.wait()  # serial implementation times out
                    else:
                        self_test.assertEqual(started, {"a", "b"})
                        self_test.assertEqual({r["agent_name"] for r in payload["previous_results"]}, {"a", "b"})
                    return Msg(name=self.name, content=json.dumps({"answer": self.name}), role="assistant")
            self_test = self
            d = SkillDispatcher(agent_registry={n: Agent(n) for n in ("a", "b", "c")})
            d._skill_meta = {"c": {"consumes": ["a", "b"]}}
            raw = decision([task("c", 2), task("a"), task("b")])
            await d.reply(Msg(name="test", content=json.dumps(raw), role="user"))
            self.assertEqual(calls[-1], "c")
            self.assertEqual(len(calls), 3)
        self.async_run(scenario())

    def test_execution_failure_does_not_remove_other_results(self):
        d = SkillDispatcher(agent_registry={"a": ScriptedAgent([RuntimeError("synthetic failure")]),
                                           "b": ScriptedAgent([{"answer": "ok"}])})
        result = self.async_run(d._execute_parallel_agents([task("a"), task("b")], {}, []))
        statuses = {r["agent_name"]: r["result"]["status"] for r in result}
        self.assertEqual(statuses, {"a": "error", "b": "success"})

    def test_memory_trace_session_filter(self):
        tracer = ExecutionTracer(postgres_config=False)
        for session, agent in [("A", "a"), ("B", "b")]:
            tracer.record(user_id="offline", session_id=session, agent_name=agent, status="success",
                          duration_ms=1, priority=1, turn_id=session,
                          payload={"status": "success", "data": {"session": session}})
        self.assertEqual(set(tracer.latest_turn_results("A")), {"a"})
        self.assertEqual(set(tracer.latest_turn_results("B")), {"b"})


class SolverTests(IsolatedCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("offline_solver", PROJECT / "skills/plan-trip/script/solver.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        if not cls.module.SOLVER_AVAILABLE:
            raise RuntimeError("OR-Tools unavailable; solver tests cannot be accepted")

    def test_budget_and_time_constraints_independently(self):
        activities = [{"name": "A", "duration_min": 60, "cost": 100, "rating": 8, "user_marked": True},
                      {"name": "B", "duration_min": 60, "cost": 50, "rating": 6, "user_marked": True},
                      {"name": "C", "duration_min": 60, "cost": 1, "rating": 10}]
        r = self.module.solve_itinerary({"days": 1, "budget_cny": 150, "daily_window": [540, 750],
                                         "travel_time_min": 30}, copy.deepcopy(activities))
        self.assertEqual(r["status"], "solved")
        self.assertEqual(set(r["scheduled"]), {"A", "B"})
        self.assertEqual(r["total_cost"], 150)
        items = r["days"][0]["items"]
        minutes = lambda s: int(s[:2]) * 60 + int(s[3:])
        for item in items:
            self.assertGreaterEqual(minutes(item["start"]), 540)
            self.assertLessEqual(minutes(item["end"]), 750)
            self.assertEqual(minutes(item["end"]) - minutes(item["start"]), 60)
        self.assertGreaterEqual(minutes(items[1]["start"]) - minutes(items[0]["end"]), 30)

    def test_impossible_required_activity_is_not_dropped(self):
        for constraints in ({"days": 1, "budget_cny": 99}, {"days": 1, "daily_window": [540, 570]}):
            with self.subTest(constraints=constraints):
                r = self.module.solve_itinerary(constraints, [{"name": "required", "duration_min": 60,
                                                              "cost": 100, "user_marked": True}])
                self.assertEqual(r["status"], "infeasible")


class ItineraryAgentTests(IsolatedCase):
    def test_full_extract_solve_narrate_and_evaluate_flow(self):
        class ScriptedModel:
            def __init__(self):
                self.calls = []

            async def __call__(self, messages):
                prompt = messages[0]["content"]
                self.calls.append(prompt)
                if len(self.calls) == 1:
                    result = {
                        "stage": "extract",
                        "constraints": {"days": 1, "daily_window": [540, 900],
                                        "travel_time_min": 30, "budget_cny": 70},
                        "activities": [
                            {"name": "西湖", "duration_min": 60, "rating": 9,
                             "cost": 30, "user_marked": True},
                            {"name": "灵隐寺", "duration_min": 60, "rating": 8,
                             "cost": 40, "user_marked": True},
                        ],
                    }
                else:
                    section = prompt.rsplit("【求解结果】", 1)[1]
                    schedule_text = section.split("\n", 1)[1].split("\n\n请按", 1)[0]
                    schedule = json.loads(schedule_text)
                    daily_plans = []
                    for day in schedule["days"]:
                        activities = [{"time": f"{item['start']}-{item['end']}",
                                       "location": item["name"], "description": "测试活动",
                                       "cost": item["cost"]}
                                      for item in day["items"]]
                        daily_plans.append({"day": day["date_index"] + 1,
                                            "activities": activities})
                    result = {"stage": "narrate", "status": "success",
                              "itinerary": {"daily_plans": daily_plans}}
                return SimpleNamespace(content=json.dumps(result, ensure_ascii=False))

        async def scenario():
            from agentscope.message import Msg
            spec = importlib.util.spec_from_file_location(
                "offline_plan_agent", PROJECT / "skills/plan-trip/script/agent.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            model = ScriptedModel()
            agent = module.ItineraryPlanningAgent(model=model)
            payload = {"context": {"rewritten_query": "杭州一日游，必去西湖和灵隐寺",
                                    "key_entities": {"duration_days": 1},
                                    "user_preferences": {}},
                       "previous_results": []}
            config = ModuleType("config")
            config.SYSTEM_CONFIG = {"skill_timeout_sec": 30}
            with patch.dict("sys.modules", {"config": config}):
                response = await agent.reply(Msg(name="test", content=json.dumps(payload), role="user"))
            result = json.loads(response.content)

            self.assertEqual(len(model.calls), 2)
            self.assertEqual(result.get("evaluation", {}).get("verdict"), "passed",
                             json.dumps(result, ensure_ascii=False))
            scheduled = [a["location"] for day in result["itinerary"]["daily_plans"]
                         for a in day["activities"]]
            self.assertEqual(set(scheduled), {"西湖", "灵隐寺"})
        self.async_run(scenario())

    def test_impossible_required_activity_is_reported_without_narration(self):
        class ScriptedModel:
            def __init__(self): self.calls = 0

            async def __call__(self, messages):
                self.calls += 1
                return SimpleNamespace(content=json.dumps({
                    "stage": "extract",
                    "constraints": {"days": 1, "daily_window": [540, 570]},
                    "activities": [{"name": "必去景点", "duration_min": 120,
                                    "user_marked": True}],
                }, ensure_ascii=False))

        async def scenario():
            from agentscope.message import Msg
            spec = importlib.util.spec_from_file_location(
                "offline_plan_agent", PROJECT / "skills/plan-trip/script/agent.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            model = ScriptedModel()
            agent = module.ItineraryPlanningAgent(model=model)
            payload = {"context": {"rewritten_query": "安排必去景点",
                                    "key_entities": {"duration_days": 1},
                                    "user_preferences": {}},
                       "previous_results": []}
            config = ModuleType("config")
            config.SYSTEM_CONFIG = {"skill_timeout_sec": 30}
            with patch.dict("sys.modules", {"config": config}):
                response = await agent.reply(Msg(name="test", content=json.dumps(payload), role="user"))
            result = json.loads(response.content)

            self.assertEqual(model.calls, 1)
            self.assertIsNone(result["itinerary"])
            self.assertIn("约束", result["solver_summary"])
            self.assertIn("必去项", result["note"])
        self.async_run(scenario())


class DurableProtocolTests(IsolatedCase):
    def client(self, handler, **cfg):
        c = PiDurableClient({"base_url": "https://offline.invalid", "token": "fake-test-token", **cfg})
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return c

    def test_submit_success_and_wait_completion(self):
        seen = []
        def handler(request):
            seen.append(request.url.path)
            self.assertEqual(request.headers["x-internal-token"], "fake-test-token")
            data = {"task_id": "test", "duplicate": False} if request.url.path.endswith("submit") else {
                "status": "completed", "result": {"results": [{"agent_name": "a"}]}}
            return httpx.Response(200, json={"code": 0, "data": data})
        async def scenario():
            c = self.client(handler)
            try:
                self.assertEqual((await c.submit_turn({"request_id": "test"}))["task_id"], "test")
                self.assertEqual((await c.wait_turn("test"))["results"], [{"agent_name": "a"}])
            finally: await c.close()
        self.async_run(scenario())
        self.assertEqual(seen, ["/v1/pipeline/submit", "/v1/pipeline/wait"])

    def test_unconfirmed_submit_and_wait_fault_both_raise(self):
        submitted = []
        def unavailable(request):
            submitted.append(request.content)
            return httpx.Response(503)
        async def scenario():
            c = self.client(unavailable)
            try:
                with self.assertRaises(PiDurableUnavailable):
                    await c.submit_turn({"request_id": "test"})
            finally: await c.close()
            c = self.client(lambda r: httpx.Response(200, json={"code": 0, "data": {"status": "faulted"}}))
            try:
                with self.assertRaises(PiDurableUnavailable): await c.wait_turn("test")
            finally: await c.close()
        self.async_run(scenario())
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0], submitted[1])

    def test_precommit_rejection_is_not_retried(self):
        attempts = []
        def reject(request):
            attempts.append(request)
            return httpx.Response(400)
        async def scenario():
            c = self.client(reject)
            try:
                with self.assertRaises(DurableSubmitRejected) as rejected:
                    await c.submit_turn({"request_id": "test"})
                self.assertEqual(rejected.exception.status_code, 400)
            finally:
                await c.close()
        self.async_run(scenario())
        self.assertEqual(len(attempts), 1)

    def test_retry_creates_task_when_first_attempt_never_reached_node(self):
        attempts = []
        def submit(request):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                raise httpx.ConnectError("first connection did not reach Node", request=request)
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "new-task", "duplicate": False,
            }})
        async def scenario():
            c = self.client(submit)
            try:
                result = await c.submit_turn({"request_id": "same-request"})
                self.assertEqual(result, {"task_id": "new-task", "duplicate": False})
            finally:
                await c.close()
        self.async_run(scenario())
        self.assertEqual(attempts, [{"request_id": "same-request"}] * 2)

    def test_timeout_without_local_resubmission(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"code": 0, "data": {"status": "running"}})
        async def scenario():
            c = self.client(handler, wait_timeout_sec=0)
            try:
                with self.assertRaises(DurableTurnTimeout): await c.wait_turn("test")
            finally: await c.close()
        self.async_run(scenario())
        self.assertEqual(calls, ["/v1/pipeline/wait"])
