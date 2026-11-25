"""API tests use real routes/intent parsing/dispatcher/runners/JSON memory.
Only model, external storage connections and initialization are replaced.
Known product defects deliberately remain failing regression tests.
"""
import asyncio
from contextlib import asynccontextmanager
import copy
import importlib
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx
import support
from support import IsolatedCase
from context.memory_manager import MemoryManager
from utils.execution_tracer import ExecutionTracer
from utils.skill_loader import SkillLoader
from utils.skill_runners import build_llm_agent


def intention(name="budget_check", **entities):
    return {"intents": [{"type": name}], "key_entities": entities,
            "rewritten_query": "测试需求", "complexity": "simple",
            "agent_schedule": [{"agent_name": name, "priority": 1}]}


class Model:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.inputs = []

    async def __call__(self, messages):
        self.inputs.append(copy.deepcopy(messages))
        if not self.outputs:
            raise AssertionError("Unplanned model call")
        item = self.outputs.pop(0)
        if callable(item): item = await item()
        if isinstance(item, Exception): raise item
        return SimpleNamespace(content=json.dumps(item, ensure_ascii=False))


class ApiTests(IsolatedCase):
    def test_lost_durable_submit_ack_does_not_run_local_dispatcher(self):
        from utils.pi_durable_client import PiDurableClient

        async def scenario():
            async with self.api([intention(), intention()]) as (c, s, _):
                tasks = {}
                submissions = []
                waits = []

                def node(request):
                    if request.url.path == "/health":
                        return httpx.Response(503)
                    payload = json.loads(request.content)
                    if request.url.path.endswith("/submit"):
                        key = (payload["user_id"], payload["request_id"])
                        duplicate = key in tasks
                        task_id = tasks.setdefault(key, f"task-{len(tasks) + 1}")
                        submissions.append((key, task_id))
                        if len(submissions) <= 2:
                            raise httpx.ReadTimeout("response lost after commit", request=request)
                        data = {"task_id": task_id, "duplicate": duplicate}
                    else:
                        waits.append(payload["task_id"])
                        data = {"status": "completed", "result": {"results": []}}
                    return httpx.Response(200, json={"code": 0, "data": data})

                durable = PiDurableClient({"base_url": "http://offline.invalid"})
                durable._client = httpx.AsyncClient(transport=httpx.MockTransport(node))
                s.state.pi_client = durable
                pipeline = s.get_pipeline("u", "A")
                request = {"message": "查预算", "user_id": "u", "session_id": "A", "request_id": "stable"}
                with patch.object(pipeline.orchestrator, "reply", new_callable=AsyncMock) as local:
                    first = await c.post("/chat", json=request)
                    self.assertEqual(first.status_code, 503, first.text)
                    retried = await c.post("/chat", json=request)
                    self.assertEqual(retried.status_code, 200, retried.text)
                    local.assert_not_awaited()
                self.assertEqual(submissions, [(("u", "stable"), "task-1")] * 3)
                self.assertEqual(len(tasks), 1)
                self.assertEqual(waits, ["task-1"])
        self.async_run(scenario())

    def test_inflight_wait_expiry_does_not_start_another_execution(self):
        async def scenario():
            async with self.api([]) as (c, s, _):
                with patch.object(s.state.idempotency, "begin", AsyncMock(return_value=("inflight", None))), \
                     patch.object(s.state.idempotency, "wait_done", AsyncMock(return_value=None)), \
                     patch.object(s, "_chat_execute", new_callable=AsyncMock) as execute:
                    result = await c.post("/chat", json={"message": "查预算", "request_id": "stable"})
                    self.assertEqual(result.status_code, 503, result.text)
                    self.assertEqual(result.headers["retry-after"], "2")
                    execute.assert_not_awaited()
        self.async_run(scenario())

    def test_lost_redis_claim_ack_returns_503_without_running_chat(self):
        async def scenario():
            async with self.api([]) as (c, s, _):
                redis = SimpleNamespace(
                    set=AsyncMock(side_effect=[TimeoutError("SET response lost"), False]),
                    get=AsyncMock(side_effect=["0:original-owner", None]),
                    aclose=AsyncMock(),
                )
                s.state.idempotency._redis = redis
                body = {"message": "查预算", "request_id": "lost-claim"}
                with patch.object(s, "_chat_execute", new_callable=AsyncMock) as execute:
                    first = await c.post("/chat", json=body)
                    self.assertEqual(first.status_code, 503, first.text)
                    self.assertIn("无法确认幂等状态", first.json()["detail"])
                    second = await c.post("/chat", json=body)
                    self.assertEqual(second.status_code, 503, second.text)
                    execute.assert_not_awaited()
                self.assertEqual(redis.set.await_count, 2)
                self.assertEqual(redis.get.await_count, 2)
                self.assertEqual(s.state.idempotency._mem, {})
        self.async_run(scenario())

    def test_chat_renews_lease_and_stops_heartbeat_when_cancelled(self):
        async def scenario():
            async with self.api([]) as (_, s, _):
                from utils.idempotency import make_key
                guard = s.state.idempotency
                guard._renew_interval = 0.01
                renewed = asyncio.Event()
                renewal_task = None
                renew = guard.renew

                async def observed_renew(key, token):
                    nonlocal renewal_task
                    renewal_task = asyncio.current_task()
                    result = await renew(key, token)
                    self.assertTrue(result)
                    renewed.set()
                    return result

                req = s.ChatRequest(message="查预算", user_id="u", session_id="A", request_id="long")
                key = make_key(req.user_id, req.session_id, req.message, req.request_id)
                clock = [100.0]
                async def long_execution(request):
                    clock[0] = 390
                    await renewed.wait()
                    clock[0] = 401
                    self.assertEqual(await guard.begin(key), ("inflight", None))
                    raise asyncio.CancelledError

                with patch("utils.idempotency.monotonic", side_effect=lambda: clock[0]), \
                     patch.object(guard, "renew", side_effect=observed_renew), \
                     patch.object(s, "_chat_execute", side_effect=long_execution):
                    with self.assertRaises(asyncio.CancelledError):
                        await s.chat(req)
                    self.assertTrue(renewal_task.done())
                    self.assertEqual((await guard.begin(key))[0], "fresh")
        self.async_run(scenario())

    def test_missing_model_keeps_history_available(self):
        async def scenario():
            async with self.api([]) as (c, s, m):
                s.state.model = None
                self.assertEqual((await c.get("/health")).status_code, 200)
                self.assertEqual((await c.get("/users/u/sessions")).status_code, 200)
                r = await c.post("/chat", json={"message": "x"})
                self.assertEqual(r.status_code, 503)
                self.assertEqual(m.inputs, [])
        self.async_run(scenario())

    def test_sensitive_route_bypasses_missing_model_and_open_circuit(self):
        async def scenario():
            async with self.api([]) as (c, s, m):
                s.state.model = None
                unavailable = await c.post("/chat", json={
                    "user_id": "safety_no_model", "session_id": "S1",
                    "message": "我要投诉差旅平台", "request_id": "safety-1",
                })
                self.assertEqual(unavailable.status_code, 200, unavailable.text)
                self.assertEqual(unavailable.json()["result"], {
                    "status": "escalated", "category": "complaint"})

                from utils.circuit_breaker import CircuitState
                s.state.model = m
                s.state.circuit_breaker._state = CircuitState.OPEN
                s.state.circuit_breaker._opened_at = None
                open_circuit = await c.post("/chat", json={
                    "user_id": "safety_open_circuit", "session_id": "S2",
                    "message": "我遇到危险，需要报警", "request_id": "safety-2",
                })
                self.assertEqual(open_circuit.status_code, 200, open_circuit.text)
                self.assertEqual(open_circuit.json()["result"], {
                    "status": "escalated", "category": "personal_safety"})
                self.assertEqual(m.inputs, [])
        self.async_run(scenario())

    @asynccontextmanager
    async def api(self, outputs):
        config = ModuleType("config")
        config.LLM_CONFIG = {"model_name": "offline", "api_key": "not-a-key"}
        config.SYSTEM_CONFIG = {"timeout": 1}
        config.RESILIENCE_CONFIG = {"max_retries": 0}
        config.PI_DURABLE_CONFIG = {"enabled": False}
        config.REDIS_CONFIG = {}
        model = Model(outputs)
        from utils.idempotency import IdempotencyGuard
        with tempfile.TemporaryDirectory() as folder, patch.dict("sys.modules", {"config": config}):
            server = importlib.import_module("server")
            def memory(**kw):
                return MemoryManager(kw["user_id"], kw["session_id"], folder,
                                     postgres_config=False, redis_config=False)
            def registry(model, cache, mm):
                loader = SkillLoader()
                return {name: build_llm_agent(model, skill, loader, mm) for name, skill in
                        [("budget_check", "budget-check"), ("preference", "preference")]}
            with patch.object(server, "state", server.AppState()), \
                 patch.object(server.state, "pipelines", {}), \
                 patch.object(server, "init_agentscope"), \
                 patch.object(server, "build_model", return_value=model), \
                 patch.object(server, "MemoryManager", side_effect=memory), \
                 patch("utils.idempotency.IdempotencyGuard", side_effect=lambda: IdempotencyGuard(redis_config=False)), \
                 patch.object(server, "ShortTermMemory", side_effect=lambda **kw: __import__(
                     "context.short_term_memory", fromlist=["ShortTermMemory"]).ShortTermMemory(
                         **{**kw, "redis_config": False})), \
                 patch.object(server, "LazyAgentRegistry", side_effect=registry), \
                 patch("agents.skill_dispatcher.ExecutionTracer", side_effect=lambda: ExecutionTracer(False)):
                async with server.app.router.lifespan_context(server.app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                                                 base_url="http://offline.invalid") as client:
                        yield client, server, model

    def test_parameter_boundaries_no_execution(self):
        async def scenario():
            async with self.api([]) as (c, s, m):
                for payload in ({}, {"message": ""}, {"message": None}, {"message": []},
                                {"message": "a" * 20001}, {"message": "x", "request_id": "a" * 129}):
                    with self.subTest(payload_type=str(type(payload.get("message")))):
                        self.assertEqual((await c.post("/chat", json=payload)).status_code, 422)
                self.assertEqual(m.inputs, [])
                self.assertEqual(s.state.pipelines, {})
        self.async_run(scenario())

    def test_whitespace_should_be_rejected(self):
        async def scenario():
            async with self.api([intention(), {"answer": "unwanted"}]) as (c, s, m):
                response = await c.post("/chat", json={"message": "   "})
                self.assertEqual(response.status_code, 422, "REQ-API-01: blank content must not enter pipeline")
                self.assertEqual(m.inputs, [])
        self.async_run(scenario())

    def test_preference_write_history_and_idempotent_replay(self):
        async def scenario():
            pref = {"preferences": [{"type": "hotel_brands", "value": ["万豪"], "action": "replace"}]}
            async with self.api([intention("preference"), pref]) as (c, s, m):
                body = {"message": "我喜欢万豪", "user_id": "test_u", "session_id": "A", "request_id": "r1"}
                first = await c.post("/chat", json=body)
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual((await c.post("/chat", json=body)).json(), first.json())
                self.assertEqual(len(m.inputs), 2)
                self.assertEqual((await c.get("/users/test_u/prefs")).json()["preferences"]["hotel_brands"], ["万豪"])
                history = (await c.get("/users/test_u/chats", params={"session_id": "A"})).json()
                self.assertEqual([x["role"] for x in history], ["user", "assistant"])
                self.assertEqual((await c.get("/users/other/prefs")).json()["preferences"], {})
        self.async_run(scenario())

    def test_gate_then_resume_keeps_context(self):
        async def scenario():
            missing = intention("itinerary_planning", destination="杭州")
            complete = intention("itinerary_planning", destination="杭州", duration_days=3, start_date="2027-05-10")
            async with self.api([missing, complete]) as (c, s, m):
                body = {"user_id": "u", "session_id": "A", "message": "杭州行程", "request_id": "1"}
                first = await c.post("/chat", json=body)
                self.assertEqual(first.json()["result"]["status"], "needs_input")
                body.update(message="三天，5月10日出发", request_id="2")
                await c.post("/chat", json=body)
                self.assertIn("杭州行程", m.inputs[1][-1]["content"])
                # Unsupported planning runner must be explicit, not a fake success.
                self.assertEqual(len(m.inputs), 2)
        self.async_run(scenario())

    def test_same_user_sessions_interleave_without_context_leak(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def delayed_skill():
                entered.set()
                await release.wait()
                return {"answer": "A result"}
            async with self.api([intention(), delayed_skill, intention(), {"answer": "B result"}]) as (c, s, m):
                a = asyncio.create_task(c.post("/chat", json={"user_id": "u", "session_id": "A", "message": "A", "request_id": "A"}))
                await entered.wait()
                b = await c.post("/chat", json={"user_id": "u", "session_id": "B", "message": "B", "request_id": "B"})
                release.set()
                ra = await a
                self.assertEqual(b.json()["session_id"], "B")
                self.assertEqual(ra.json()["session_id"], "A", "REQ-STATE-01: A response was rebound to B")
                history = (await c.get("/users/u/chats", params={"session_id": "A"})).json()
                self.assertEqual(len(history), 2)
                history_b = (await c.get("/users/u/chats", params={"session_id": "B"})).json()
                self.assertEqual(len(history_b), 2)
                self.assertEqual(history[0]["content"], "A")
                self.assertEqual(history_b[0]["content"], "B")
                self.assertEqual(s.get_pipeline("u", "A").orchestrator._trace_ctx["session_id"], "A")
                self.assertEqual(s.get_pipeline("u", "B").orchestrator._trace_ctx["session_id"], "B")
        self.async_run(scenario())

    def test_inflight_duplicate_runs_business_once(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def delayed():
                entered.set()
                await release.wait()
                return {"answer": "same"}
            async with self.api([intention(), delayed]) as (c, s, m):
                body = {"message": "x", "user_id": "u", "session_id": "A", "request_id": "r"}
                a = asyncio.create_task(c.post("/chat", json=body))
                await entered.wait()
                b = asyncio.create_task(c.post("/chat", json=body))
                await asyncio.sleep(0)
                release.set()
                ra, rb = await asyncio.gather(a, b)
                self.assertEqual(ra.json(), rb.json())
                self.assertEqual(len(m.inputs), 2)
        self.async_run(scenario())

    def test_pipeline_exception_releases_key_and_gauge(self):
        async def scenario():
            from utils.metrics import IN_FLIGHT
            async with self.api([intention()]) as (c, s, m):
                pipeline = s.get_pipeline("u", "A")
                before = IN_FLIGHT._value.get()
                with patch.object(pipeline.orchestrator, "reply", AsyncMock(side_effect=ValueError("injected failure"))):
                    r = await c.post("/chat", json={"message": "x", "user_id": "u", "session_id": "A", "request_id": "r"})
                self.assertEqual(r.status_code, 502)
                from utils.idempotency import make_key
                self.assertEqual((await s.state.idempotency.begin(make_key("u", "A", "x", "r")))[0], "fresh")
                actual = IN_FLIGHT._value.get()
                IN_FLIGHT.set(before)  # restore even when recording the defect
                self.assertEqual(actual, before, "REQ-FAIL-01: in-flight leaked after error")
        self.async_run(scenario())

    def test_json_history_survives_restart_and_delete_is_scoped(self):
        async def scenario():
            async with self.api([]) as (c, s, m):
                for sid in ("A", "B"):
                    r = await c.post("/chat", json={"user_id": "u", "session_id": sid,
                        "message": "我要退款", "request_id": sid})
                    self.assertEqual(r.status_code, 200)
                # Rebuild all pipelines: new MemoryManager reads the JSON file.
                s.state.pipelines.clear()
                s.state.session_pipelines.clear()
                sessions = (await c.get("/users/u/sessions")).json()
                self.assertEqual({x["session_id"] for x in sessions}, {"A", "B"})
                self.assertTrue(all(x["message_count"] == 2 for x in sessions))
                self.assertEqual(len((await c.get("/users/u/chats", params={"session_id": "A"})).json()), 2)
                r = await c.delete("/users/u/sessions/A")
                self.assertEqual(r.status_code, 200)
                self.assertEqual((await c.get("/users/u/chats", params={"session_id": "A"})).json(), [])
                self.assertEqual(len((await c.get("/users/u/chats", params={"session_id": "B"})).json()), 2)
                self.assertEqual([x["session_id"] for x in (await c.get("/users/u/sessions")).json()], ["B"])
                self.assertEqual(m.inputs, [])
        self.async_run(scenario())


class ConsistencyTests(IsolatedCase):
    def test_reuse_only_when_entity_snapshot_matches(self):
        from agents.skill_dispatcher import SkillDispatcher
        with patch("agents.skill_dispatcher.ExecutionTracer", side_effect=lambda: ExecutionTracer(False)):
            d = SkillDispatcher()
        old = intention("itinerary_planning", destination="杭州", duration_days=3, start_date="2027-05-10")
        d.finalize_turn(old, [{"agent_name": "budget_check", "priority": 1,
                              "result": {"status": "success", "data": {"city": "杭州"}}}])
        new = copy.deepcopy(old)
        new["replan"] = True
        new["agent_schedule"] = [{"agent_name": "budget_check", "priority": 1},
                                 {"agent_name": "itinerary_planning", "priority": 2}]
        self.assertEqual(len(d.plan_batches(new)["reused"]), 1)
        new["key_entities"]["start_date"] = "2027-05-11"
        self.assertEqual(d.plan_batches(new)["reused"], [])

    def test_changed_destination_must_not_reuse_old_city_data(self):
        from agents.skill_dispatcher import SkillDispatcher
        with patch("agents.skill_dispatcher.ExecutionTracer", side_effect=lambda: ExecutionTracer(False)):
            dispatcher = SkillDispatcher()
        dispatcher._last_results = {"budget_check": {"status": "success", "data": {"city": "杭州"}}}
        new = intention("itinerary_planning", destination="北京", duration_days=3, start_date="2027-05-10")
        new["replan"] = True
        new["agent_schedule"] = [{"agent_name": "budget_check", "priority": 1},
                                 {"agent_name": "itinerary_planning", "priority": 2}]
        plan = dispatcher.plan_batches(new)
        self.assertEqual(plan["reused"], [], "REQ-CACHE-01: Hangzhou budget reused for Beijing")

    def test_preference_write_must_not_leave_stale_cache(self):
        from context.memory_manager import _CachedLongTermProxy
        # Deterministic interleaving: cache deletion -> concurrent read of old
        # storage -> write new storage. No actual Redis/PG connection.
        class Cache:
            value = None
            def get(self, key): return self.value
            def setex(self, key, ttl, value): self.value = value
            def delete(self, key): self.value = None
        class Store:
            value = {"hotel_brands": ["如家"]}
            on_write = None
            def get_preference(self, kind=None):
                return copy.deepcopy(self.value if kind is None else self.value.get(kind))
            def save_preference(self, kind, value):
                self.on_write()
                self.value[kind] = value
        store, cache = Store(), Cache()
        config = ModuleType("config")
        config.REDIS_CONFIG = {}
        with patch.dict("sys.modules", {"config": config}):
            proxy = _CachedLongTermProxy(store, cache, "offline")
        self.assertEqual(proxy.get_preference()["hotel_brands"], ["如家"])
        store.on_write = lambda: proxy.get_preference()  # old read refills deleted cache
        proxy.save_preference("hotel_brands", ["万豪"])
        self.assertEqual(store.value["hotel_brands"], ["万豪"])
        self.assertEqual(proxy.get_preference()["hotel_brands"], ["万豪"],
                         "REQ-MEM-01: successful write left old value cached")
