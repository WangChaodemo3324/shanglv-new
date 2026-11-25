"""按请求 ID 占用幂等键，并用执行者令牌保护结果、释放和续期。"""

import asyncio
from dataclasses import dataclass
import hashlib
import logging
from time import monotonic
from typing import Literal
from uuid import uuid4

logger = logging.getLogger(__name__)

_INFLIGHT_PREFIX = "0:"
_DONE_PREFIX = "1\n"
_CHECK_OWNER = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
"""
_COMPLETE = _CHECK_OWNER + "redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1"
_RELEASE = _CHECK_OWNER + "return redis.call('DEL', KEYS[1])"
_RENEW = _CHECK_OWNER + "return redis.call('EXPIRE', KEYS[1], ARGV[2])"


class IdempotencyUnavailable(Exception):
    """Redis 操作结果无法确认。"""


@dataclass
class _MemoryEntry:
    value: str
    expires_at: float
    changed: asyncio.Event


def make_key(user_id: str, session_id: str, message: str,
             request_id: str | None = None) -> str:
    if request_id:
        raw = f"{user_id}:rid:{request_id}"
    else:
        h = hashlib.md5(f"{user_id}|{session_id}|{message}".encode("utf-8")).hexdigest()
        raw = f"{user_id}:hash:{h}"
    return f"shanglv:idem:{raw}"


class IdempotencyGuard:
    def __init__(self, redis_config: dict | Literal[False] | None = None, ttl_sec: int = 300,
                 renew_interval_sec: float = 60):
        if ttl_sec <= 0 or renew_interval_sec <= 0:
            raise ValueError("幂等有效期和续期间隔必须大于零")
        self._config = redis_config
        self._ttl = ttl_sec
        self._renew_interval = min(renew_interval_sec, ttl_sec / 3)
        self._redis = None
        self._mem: dict[str, _MemoryEntry] = {}

    async def start(self) -> None:
        """启动时选择存储。启用 Redis 后，单次调用失败保持原存储。"""
        if self._config is False or self._redis is not None:
            return
        config = self._config
        if config is None:
            from config import REDIS_CONFIG
            config = REDIS_CONFIG
        client = None
        try:
            from redis.asyncio import Redis
            from redis.asyncio.retry import Retry
            from redis.backoff import NoBackoff

            client = Redis(
                host=config.get("host", "127.0.0.1"),
                port=config.get("port", 6380),
                db=config.get("db", 0),
                password=config.get("password"),
                socket_connect_timeout=2, socket_timeout=2, decode_responses=True,
                retry=Retry(NoBackoff(), 0),
            )
            await client.ping()
        except Exception as exc:
            if client is not None:
                await client.aclose()
            logger.warning("Idempotency guard using process memory: %s", exc)
        else:
            self._redis = client
            logger.info("Idempotency guard using Redis")

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def _redis_call(self, command, *args, **kwargs):
        try:
            return await command(*args, **kwargs)
        except Exception as exc:
            raise IdempotencyUnavailable("Redis 幂等操作结果无法确认") from exc

    @staticmethod
    def _status(value: str | None) -> tuple[str, str | None]:
        if value is None:
            return "inflight", None
        if value.startswith(_DONE_PREFIX):
            return "done", value[len(_DONE_PREFIX):]
        return "inflight", None

    async def begin(self, key: str) -> tuple[str, str | None]:
        """返回 (fresh, token)、(inflight, None) 或 (done, response_json)。"""
        token = uuid4().hex
        value = _INFLIGHT_PREFIX + token
        if self._redis is not None:
            for _ in range(2):
                if await self._redis_call(self._redis.set, key, value, nx=True, ex=self._ttl):
                    return "fresh", token
                current = await self._redis_call(self._redis.get, key)
                if current is not None:
                    return self._status(current)
            return "inflight", None

        now = monotonic()
        for expired_key, entry in list(self._mem.items()):
            if entry.expires_at <= now:
                del self._mem[expired_key]
                entry.changed.set()
        entry = self._mem.get(key)
        if entry is not None:
            return self._status(entry.value)
        self._mem[key] = _MemoryEntry(value, now + self._ttl, asyncio.Event())
        return "fresh", token

    async def wait_done(self, key: str, timeout_sec: float = 300) -> str | None:
        deadline = monotonic() + timeout_sec
        while True:
            if self._redis is not None:
                value = await self._redis_call(self._redis.get, key)
                event = None
            else:
                entry = self._live_entry(key)
                value = entry.value if entry is not None else None
                event = entry.changed if entry is not None else None
            if value is None:
                return None
            status, cached = self._status(value)
            if status == "done":
                return cached
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            if event is None:
                await asyncio.sleep(min(0.4, remaining))
            else:
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except TimeoutError:
                    return None

    def _live_entry(self, key: str) -> _MemoryEntry | None:
        entry = self._mem.get(key)
        if entry is not None and entry.expires_at <= monotonic():
            del self._mem[key]
            entry.changed.set()
            return None
        return entry

    def _owned_entry(self, key: str, token: str) -> _MemoryEntry | None:
        entry = self._live_entry(key)
        return entry if entry is not None and entry.value == _INFLIGHT_PREFIX + token else None

    async def complete(self, key: str, token: str, response_json: str) -> bool:
        value = _DONE_PREFIX + response_json
        if self._redis is not None:
            return bool(await self._redis_call(
                self._redis.eval, _COMPLETE, 1, key, _INFLIGHT_PREFIX + token, value, self._ttl,
            ))
        entry = self._owned_entry(key, token)
        if entry is None:
            return False
        entry.value = value
        entry.expires_at = monotonic() + self._ttl
        entry.changed.set()
        return True

    async def release(self, key: str, token: str) -> bool:
        if self._redis is not None:
            return bool(await self._redis_call(self._redis.eval, _RELEASE, 1, key, _INFLIGHT_PREFIX + token))
        entry = self._owned_entry(key, token)
        if entry is None:
            return False
        del self._mem[key]
        entry.changed.set()
        return True

    async def renew(self, key: str, token: str) -> bool:
        if self._redis is not None:
            return bool(await self._redis_call(
                self._redis.eval, _RENEW, 1, key, _INFLIGHT_PREFIX + token, self._ttl,
            ))
        entry = self._owned_entry(key, token)
        if entry is None:
            return False
        entry.expires_at = monotonic() + self._ttl
        return True

    async def keep_alive(self, key: str, token: str) -> None:
        delay = self._renew_interval
        while True:
            await asyncio.sleep(delay)
            try:
                if not await self.renew(key, token):
                    logger.warning("Idempotency lease no longer owned")
                    return
            except IdempotencyUnavailable:
                logger.warning("Idempotency lease renewal unavailable")
                delay = min(self._renew_interval, 2)
            else:
                delay = self._renew_interval
