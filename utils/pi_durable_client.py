"""pi-durable 客户端（仿 wenshu backend/app/integrations/pi_runtime.py）。

降级语义（关键约定）：
  - 提交结果未知（连不上 / 5xx / 超时）→ 用同一个请求重试一次，仍无法确认则抛异常；
  - 提交在入队前被明确拒绝 → 抛 DurableSubmitRejected；
  - 提交成功后的故障 → wait_turn 抛异常（绝不降级本地重跑——那会双执行），
    上层转 503 提示「用相同 request_id 重发接续结果」；
  - available() 带 TTL 缓存（成功 15s / 失败 5s），避免每轮请求都打健康检查。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class DurableTurnTimeout(Exception):
    """等待总预算耗尽（任务仍在 pi-durable 上跑）。上层应 503 并提示重发接续。"""


class PiDurableUnavailable(Exception):
    """提交结果未知，或提交后不可用。上层应返回 503。"""


class DurableSubmitRejected(Exception):
    """Node 在创建任务前明确拒绝了提交。"""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"任务服务拒绝提交（HTTP {status_code}）")


class PiDurableClient:
    def __init__(self, cfg: dict):
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.token = cfg.get("token") or ""
        self.submit_timeout_sec = float(cfg.get("submit_timeout_sec", 10.0))
        self.wait_timeout_sec = float(cfg.get("wait_timeout_sec", 480.0))
        self.poll_interval_sec = float(cfg.get("poll_interval_sec", 30.0))
        self.health_ttl_sec = float(cfg.get("health_ttl_sec", 15.0))
        self._client: Optional[httpx.AsyncClient] = None
        # 健康缓存：(ok, checked_at)
        self._health: tuple[bool, float] = (False, 0.0)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.submit_timeout_sec)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def available(self) -> bool:
        """健康检查 + TTL 缓存（成功缓存 health_ttl，失败缓存 5s）"""
        ok, checked = self._health
        ttl = self.health_ttl_sec if ok else 5.0
        if time.monotonic() - checked < ttl:
            return ok
        try:
            resp = await self._http().get(f"{self.base_url}/health", timeout=3.0)
            ok = resp.status_code == 200
        except Exception:
            ok = False
        self._health = (ok, time.monotonic())
        return ok

    def _headers(self) -> dict:
        return {"x-internal-token": self.token}

    async def submit_turn(self, payload: dict) -> dict:
        """提交或确认同一轮任务。两次尝试复用完全相同的请求。"""
        if not payload.get("request_id"):
            raise ValueError("提交 durable 任务需要 request_id")

        for attempt in (1, 2):
            try:
                resp = await self._http().post(
                    f"{self.base_url}/v1/pipeline/submit",
                    headers=self._headers(), json=payload,
                    timeout=self.submit_timeout_sec)
                if resp.status_code in (400, 401, 403, 413, 422):
                    if attempt == 1:
                        raise DurableSubmitRejected(resp.status_code)
                    # 此次被拒绝不能证明上一次提交未被接受。
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") if isinstance(body, dict) and body.get("code") == 0 else None
                if isinstance(data, dict) and isinstance(data.get("task_id"), str) and data["task_id"]:
                    return data
                raise ValueError("提交响应缺少有效任务 ID")
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("pi-durable submit 未确认（%d/2）: %s", attempt, exc)
                if attempt == 2:
                    raise PiDurableUnavailable("提交结果暂时无法确认") from exc
                await _sleep_min(0.25)

    async def wait_turn(self, task_id: str) -> dict:
        """长轮询等待任务完成，返回 {"results": [...]}。
        - completed → 返回 result（含 results 列表）
        - faulted  → 抛 PiDurableUnavailable
        - 总预算耗尽（仍 running）→ 抛 DurableTurnTimeout
        - 传输错误重试 3 次后 → 抛 PiDurableUnavailable（不降级——任务可能还在跑）
        """
        deadline = time.monotonic() + self.wait_timeout_sec
        transport_errors = 0
        while True:
            try:
                resp = await self._http().post(
                    f"{self.base_url}/v1/pipeline/wait",
                    headers=self._headers(),
                    json={"task_id": task_id, "timeout_ms": int(self.poll_interval_sec * 1000)},
                    timeout=self.poll_interval_sec + 10.0)
                resp.raise_for_status()
                body = resp.json()
                transport_errors = 0
            except (httpx.HTTPError, ValueError) as e:
                transport_errors += 1
                if transport_errors > 3:
                    raise PiDurableUnavailable(f"wait 传输错误（重试耗尽）: {e}") from e
                await _sleep_min(2.0)
                continue

            if body.get("code") != 0:
                raise PiDurableUnavailable(f"wait 被拒绝: {body.get('message')}")

            data = body.get("data") or {}
            status = data.get("status")
            if status == "completed":
                result = data.get("result") or {}
                if "results" not in result:
                    raise PiDurableUnavailable("completed 但缺 results 字段")
                return result
            if status == "faulted":
                raise PiDurableUnavailable(f"任务 faulted: {data.get('error', 'unknown')}")

            # running：预算内继续轮询
            if time.monotonic() >= deadline:
                raise DurableTurnTimeout(
                    f"等待 {self.wait_timeout_sec}s 仍未完成，任务在 pi-durable 上继续，"
                    f"用相同 request_id 重发可接续结果")
            await _sleep_min(2.0)


    async def submit_intent(self, prompt: str, request_id: str, user_id: str) -> Optional[str]:
        """M7：提交意图识别 prompt 到 pi-durable 的 GenerationTask。
        返回 submission_id；提交期故障返回 None（降级信号——意图识别幂等，降级无风险）。"""
        try:
            resp = await self._http().post(
                f"{self.base_url}/v1/intent/submit",
                headers=self._headers(),
                json={"request_id": request_id, "user_id": user_id, "prompt": prompt},
                timeout=self.submit_timeout_sec)
            if resp.status_code >= 500:
                logger.warning("pi-durable intent submit 5xx: %s", resp.status_code)
                return None
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != 0:
                logger.warning("pi-durable intent submit rejected: %s", body.get("message"))
                return None
            return (body.get("data") or {}).get("submission_id")
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("pi-durable intent submit failed → 降级本地意图识别: %s", e)
            return None

    async def wait_intent(self, submission_id: str) -> Optional[str]:
        """M7：等待意图识别完成，返回 assistant 文本。
        与编排 wait 不同：超时/不可达返回 None（降级本地意图识别）——意图识别幂等，
        同 prompt 本地重跑无副作用，不像编排有双执行风险。"""
        deadline = time.monotonic() + min(self.wait_timeout_sec, 90.0)
        transport_errors = 0
        while True:
            try:
                resp = await self._http().post(
                    f"{self.base_url}/v1/intent/wait",
                    headers=self._headers(),
                    json={"submission_id": submission_id, "timeout_ms": int(self.poll_interval_sec * 1000)},
                    timeout=self.poll_interval_sec + 10.0)
                resp.raise_for_status()
                body = resp.json()
                transport_errors = 0
            except (httpx.HTTPError, ValueError) as e:
                transport_errors += 1
                if transport_errors > 3:
                    logger.warning("pi-durable intent wait 传输错误耗尽 → 降级本地: %s", e)
                    return None
                await _sleep_min(2.0)
                continue

            if body.get("code") != 0:
                logger.warning("pi-durable intent wait rejected: %s", body.get("message"))
                return None

            data = body.get("data") or {}
            status = data.get("status")
            if status == "completed":
                return data.get("text")
            if status == "running":
                if time.monotonic() >= deadline:
                    logger.warning("pi-durable intent wait 超预算 → 降级本地意图识别")
                    return None
                await _sleep_min(2.0)
                continue
            # 未知状态按不可用处理
            return None


async def _sleep_min(sec: float) -> None:
    import asyncio
    await asyncio.sleep(sec)
