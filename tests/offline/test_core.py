import asyncio
import copy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from support import IsolatedCase
import support
from utils.contracts import normalize_intention, normalize_preferences, normalize_budget_result
from utils.evaluator import evaluate_itinerary, verify_expense_estimate, verify_conflict_check, verified_execute
from utils.idempotency import IdempotencyGuard, make_key
from context.long_term_memory import LongTermMemory
from context.short_term_memory import ShortTermMemory
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitState, CircuitOpenError
from utils.llm_resilience import retry_with_backoff
from unittest.mock import AsyncMock

PROJECT = Path(__file__).resolve().parents[2]


class GuardTests(IsolatedCase):
    def test_network_and_write_denied_even_if_caught(self):
        # Separate Guard instance exercises the exact installed policy without
        # consuming the real run's violation ledger.
        from guards import Guard, TestIsolationViolation
        g = Guard(support.ROOT)
        for event, args in [("socket.connect", (None, ("127.0.0.1", 8000))),
                            ("open", (str(PROJECT / "forbidden.txt"), "w", 0)),
                            ("os.rename", (str(support.ROOT / "a"), str(PROJECT / "b")))]:
            try:
                g.audit(event, args)
            except TestIsolationViolation:
                pass
        self.assertEqual(len(g.violations), 3)

    def test_temporary_write_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "allowed.json"
            p.write_text("{}", encoding="utf-8")
            self.assertEqual(p.read_text(), "{}")


class DatasetTests(IsolatedCase):
    def test_all_1604_records_match_source(self):
        root = PROJECT / "tests/eval/datasets/chinatravel"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
        ids = set()
        for item in manifest:
            split = item["split"]
            raw_path = root / "raw" / f"{split}.csv"
            self.assertEqual(hashlib.sha256(raw_path.read_bytes()).hexdigest(), item["raw_sha256"])
            with raw_path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            data = json.loads((root / f"{split}.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(len(rows), item["count"])
            self.assertEqual(len(data["cases"]), len(rows))
            for row, case in zip(rows, data["cases"]):
                with self.subTest(split=split, uid=row["uid"]):
                    self.assertNotIn(case["id"], ids)
                    ids.add(case["id"])
                    self.assertTrue(case["query"].strip())
                    self.assertEqual(case["query"], row["nature_language"])
                    self.assertEqual(case["source_record"], row)
                    if "days" in row:
                        self.assertEqual(case["reference_entities"]["duration_days"], int(row["days"]))
                    else:
                        self.assertEqual(case["reference_entities"], {})
                    self.assertEqual(case["evaluation_status"], "pending_adapter")
        self.assertEqual(len(ids), 1604)


class ContractTests(IsolatedCase):
    def test_invalid_intent_shapes(self):
        for raw in (None, [], "bad", {"agent_schedule": "bad", "complexity": "complex"}):
            with self.subTest(raw=raw):
                result = normalize_intention(raw)
                self.assertIsInstance(result["agent_schedule"], list)
                self.assertIsInstance(result["key_entities"], dict)
                self.assertEqual(result["complexity"], "standard")

    def test_null_entities_without_mutating_source(self):
        raw = {"key_entities": {"start_date": " null ", "duration_days": 3}}
        original = copy.deepcopy(raw)
        self.assertIsNone(normalize_intention(raw)["key_entities"]["start_date"])
        self.assertEqual(raw, original)

    def test_preference_shapes_and_actions(self):
        result = normalize_preferences([{"type": "hotel_brands", "value": [["万豪", "如家"]], "action": "append"},
                                        {"type": "home_location", "value": ["杭州", "上海"]},
                                        {"type": "airlines", "value": "东航", "action": "invalid"},
                                        {"type": "seat_preference", "value": "null"}])
        self.assertEqual(result, [{"type": "hotel_brands", "value": ["万豪", "如家"], "action": "append"},
                                  {"type": "home_location", "value": "杭州", "action": "replace"},
                                  {"type": "airlines", "value": ["东航"], "action": "replace"}])

    def test_money_normalization(self):
        for value, expected in [("1,200元", 1200), ("bad", None), ("", None), (400, 400)]:
            with self.subTest(value=value):
                self.assertEqual(normalize_budget_result({"estimated_total_cap": value})["estimated_total_cap"], expected)


def itinerary():
    return {"daily_plans": [{"activities": [{"name": "西湖", "time": "09:00-10:00", "cost": 100},
                                            {"name": "灵隐寺", "time": "10:30-11:30", "cost": 50}]}]}


class EvaluatorTests(IsolatedCase):
    def test_valid_plan_at_budget_and_gap_boundary(self):
        r = evaluate_itinerary(itinerary(), {"duration_days": 1}, budget_cny=150, must_visit=["西湖", "灵隐寺"])
        self.assertEqual(r["verdict"], "passed")

    def test_independent_bad_plan_mutations(self):
        for kind in ("days", "budget", "must_visit", "overlap"):
            plan = itinerary()
            entities, budget, targets = {"duration_days": 1}, 150, ["西湖"]
            if kind == "days": entities["duration_days"] = 2
            if kind == "budget": budget = 149
            if kind == "must_visit": targets = ["兵马俑"]
            if kind == "overlap": plan["daily_plans"][0]["activities"][1]["time"] = "10:29-11:30"
            with self.subTest(kind=kind):
                self.assertEqual(evaluate_itinerary(plan, entities, budget_cny=budget, must_visit=targets)["verdict"], "failed")

    def test_expense_arithmetic(self):
        self.assertEqual(verify_expense_estimate({"budget_cap": {"accommodation": 400, "meals": 100, "total": 500}})["verdict"], "passed")
        self.assertEqual(verify_expense_estimate({"budget_cap": {"accommodation": 400, "meals": 100, "total": 900}})["verdict"], "failed")

    def test_future_intersection_inclusive(self):
        for start, expected in [("2099-01-02", "failed"), ("2099-01-03", "passed")]:
            data = {"result": "no_conflict", "checked_trip": {"start_date": "2099-01-01", "end_date": "2099-01-02"},
                    "trips": [{"start_date": start, "end_date": "2099-01-05", "destination": "杭州"}]}
            with self.subTest(start=start):
                self.assertEqual(verify_conflict_check(data)["verdict"], expected)

    def test_bounded_retry_with_issue_feedback(self):
        for repaired in (True, False):
            calls = []
            async def execute(issues):
                calls.append(issues)
                return {"budget_cap": {"accommodation": 400, "meals": 100,
                                       "total": 500 if repaired and issues else 900}}
            result, verdict = self.async_run(verified_execute(execute, verify_expense_estimate))
            self.assertEqual(len(calls), 2)
            self.assertIsNone(calls[0])
            self.assertTrue(calls[1])
            self.assertEqual(verdict["verdict"], "passed" if repaired else "failed")
            self.assertEqual("evaluation_warnings" in result, not repaired)


class StateTests(IsolatedCase):
    def test_memory_manager_session_history_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            a = MemoryManager("offline_user", "A", folder, postgres_config=False, redis_config=False)
            a.add_message("user", "only A")
            b = MemoryManager("offline_user", "B", folder, postgres_config=False, redis_config=False)
            b.add_message("user", "only B")
            # Reopen after both writes; JSON instances are not claimed to be
            # concurrent writers, but session filtering must work on disk.
            c = MemoryManager("offline_user", "C", folder, postgres_config=False, redis_config=False)
            self.assertEqual([m["content"] for m in c.long_term.get_chat_history(session_id="A")], ["only A"])
            self.assertEqual([m["content"] for m in c.long_term.get_chat_history(session_id="B")], ["only B"])
            self.assertEqual(b.short_term.get_recent_context()[0]["content"], "only B")

    def test_user_memory_json_roundtrip_and_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            a = LongTermMemory("test_user_a", folder, postgres_config=False)
            b = LongTermMemory("test_user_b", folder, postgres_config=False)
            a.save_preference("hotel_brands", ["如家"])
            a.add_hotel_brand("万豪")
            a.add_hotel_brand("万豪")
            restored = LongTermMemory("test_user_a", folder, postgres_config=False)
            self.assertEqual(restored.get_preference("hotel_brands"), ["如家", "万豪"])
            self.assertIsNone(b.get_preference("hotel_brands"))

    def test_short_memory_window_and_session_objects(self):
        a = ShortTermMemory(max_turns=2, session_id="test_a", user_id="test_u", redis_config=False)
        b = ShortTermMemory(max_turns=2, session_id="test_b", user_id="test_u", redis_config=False)
        for i in range(6): a.add_message("user" if i % 2 == 0 else "assistant", str(i))
        self.assertEqual([m["content"] for m in a.get_recent_context()], ["2", "3", "4", "5"])
        self.assertEqual(b.get_recent_context(), [])
        a.clear()
        self.assertEqual(a.get_recent_context(), [])

    def test_idempotency_key_user_and_session_boundaries(self):
        self.assertNotEqual(make_key("a", "s", "q"), make_key("b", "s", "q"))
        self.assertNotEqual(make_key("a", "s", "q"), make_key("a", "t", "q"))
        self.assertNotEqual(make_key("a", "s", "q", "rid"), make_key("b", "s", "q", "rid"))
        self.assertEqual(make_key("a", "s", "q", "rid"), make_key("a", "s", "other", "rid"))

    def test_inflight_replay_and_failure_release(self):
        g = IdempotencyGuard(redis_config=False)
        async def scenario():
            status, token = await g.begin("test_key")
            self.assertEqual(status, "fresh")
            self.assertEqual((await g.begin("test_key"))[0], "inflight")
            waiting = asyncio.create_task(g.wait_done("test_key", timeout_sec=1))
            await asyncio.sleep(0)
            self.assertTrue(await g.complete("test_key", token, '{"ok":true}'))
            self.assertEqual(await waiting, '{"ok":true}')
            self.assertEqual(await g.begin("test_key"), ("done", '{"ok":true}'))
            self.assertFalse(await g.release("test_key", token))
            _, failed_token = await g.begin("failed")
            self.assertTrue(await g.release("failed", failed_token))
            self.assertEqual((await g.begin("failed"))[0], "fresh")
        self.async_run(scenario())

    def test_idempotency_ttl_with_controlled_clock(self):
        async def scenario():
            clock = [100.0]
            with patch("utils.idempotency.monotonic", side_effect=lambda: clock[0]):
                g = IdempotencyGuard(redis_config=False, ttl_sec=5)
                _, token_a = await g.begin("ttl")
                clock[0] = 103
                self.assertTrue(await g.renew("ttl", token_a))
                clock[0] = 106
                self.assertEqual(await g.begin("ttl"), ("inflight", None))
                clock[0] = 108
                status, token_b = await g.begin("ttl")
                self.assertEqual(status, "fresh")
                self.assertNotEqual(token_a, token_b)
                self.assertFalse(await g.complete("ttl", token_a, "stale"))
                self.assertFalse(await g.release("ttl", token_a))
                self.assertFalse(await g.renew("ttl", token_a))
                self.assertEqual(await g.begin("ttl"), ("inflight", None))
                self.assertTrue(await g.complete("ttl", token_b, "new"))
                self.assertEqual(await g.begin("ttl"), ("done", "new"))
                clock[0] = 113
                self.assertEqual((await g.begin("ttl"))[0], "fresh")
        self.async_run(scenario())


class ResilienceTests(IsolatedCase):
    def test_circuit_open_half_open_and_recovery(self):
        clock = [100.0]
        with patch("utils.circuit_breaker.time.monotonic", side_effect=lambda: clock[0]):
            c = CircuitBreaker(failure_threshold=2, recovery_timeout_sec=10, half_open_successes=2)
            c.record_failure()
            self.assertTrue(c.allow_call())
            c.record_failure()
            with self.assertRaises(CircuitOpenError): c.raise_if_open()
            clock[0] = 110
            self.assertEqual(c.state, CircuitState.HALF_OPEN)
            c.record_success()
            self.assertEqual(c.state, CircuitState.HALF_OPEN)
            c.record_success()
            self.assertEqual(c.state, CircuitState.CLOSED)

    def test_backoff_cap_and_attempt_limit(self):
        calls = []
        async def failing():
            calls.append(1)
            raise TimeoutError("synthetic timeout")
        sleep = AsyncMock()
        with patch("utils.llm_resilience.asyncio.sleep", sleep):
            with self.assertRaises(TimeoutError):
                self.async_run(retry_with_backoff(failing, max_retries=3, base_delay_sec=2,
                                                 max_delay_sec=3, jitter=False))
        self.assertEqual(len(calls), 4)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [2, 3, 3])

    def test_nonretryable_and_circuit_errors_are_not_retried(self):
        for error in (ValueError("bad input"), CircuitOpenError("open")):
            calls = []
            async def failing():
                calls.append(1)
                raise error
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self.async_run(retry_with_backoff(failing))
                self.assertEqual(len(calls), 1)
