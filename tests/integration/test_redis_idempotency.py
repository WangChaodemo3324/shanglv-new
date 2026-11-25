"""Opt-in Lua integration test against an isolated Redis instance.

Set SHANGLV_TEST_REDIS_PORT and run:
python -m unittest discover -s tests/integration -p test_redis_idempotency.py
"""

import asyncio
import os
import unittest
from uuid import uuid4

from utils.idempotency import IdempotencyGuard


@unittest.skipUnless(os.environ.get("SHANGLV_TEST_REDIS_PORT"), "isolated Redis port not configured")
class RedisIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key = f"shanglv:test:idem:{uuid4().hex}"
        self.guard = IdempotencyGuard(
            redis_config={"host": "127.0.0.1", "port": int(os.environ["SHANGLV_TEST_REDIS_PORT"])},
            ttl_sec=5,
        )
        self.addAsyncCleanup(self.guard.close)
        await self.guard.start()
        self.redis = self.guard._redis
        self.assertIsNotNone(self.redis, "test requires Redis, memory fallback is not allowed")
        self.addAsyncCleanup(self._delete_test_key)

    async def _delete_test_key(self):
        await self.redis.delete(self.key)

    async def test_renew_and_reject_stale_owner_after_expiry(self):
        status, token_a = await self.guard.begin(self.key)
        self.assertEqual(status, "fresh")

        await self.redis.pexpire(self.key, 1000)
        self.assertTrue(await self.guard.renew(self.key, token_a))
        self.assertGreater(await self.redis.pttl(self.key), 1000)
        self.assertEqual(await self.guard.begin(self.key), ("inflight", None))

        await self.redis.pexpire(self.key, 1)
        for _ in range(50):
            if not await self.redis.exists(self.key):
                break
            await asyncio.sleep(0.02)
        status, token_b = await self.guard.begin(self.key)
        self.assertEqual(status, "fresh")
        self.assertNotEqual(token_a, token_b)

        self.assertFalse(await self.guard.complete(self.key, token_a, "stale"))
        self.assertFalse(await self.guard.release(self.key, token_a))
        self.assertFalse(await self.guard.renew(self.key, token_a))
        self.assertEqual(await self.guard.begin(self.key), ("inflight", None))

        self.assertTrue(await self.guard.complete(self.key, token_b, "current"))
        self.assertEqual(await self.guard.wait_done(self.key), "current")
        self.assertFalse(await self.guard.release(self.key, token_b))
        self.assertEqual(await self.guard.begin(self.key), ("done", "current"))

        await self._delete_test_key()
        status, token_c = await self.guard.begin(self.key)
        self.assertEqual(status, "fresh")
        self.assertTrue(await self.guard.release(self.key, token_c))
        self.assertIsNone(await self.redis.get(self.key))
