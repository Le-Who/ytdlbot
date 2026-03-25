"""Tests for TokenBucketLimiter — deterministic, no time.sleep."""

import unittest
from unittest.mock import patch

from app.core.limiter import TokenBucketLimiter


class TestTokenBucketLimiter(unittest.IsolatedAsyncioTestCase):
    """Test token bucket rate limiter with monkeypatched time."""

    async def test_basic_consumption(self):
        """Consuming tokens up to capacity, then blocked."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
        self.assertTrue(await limiter.allow("k"))
        self.assertTrue(await limiter.allow("k"))
        self.assertFalse(await limiter.allow("k"))

    async def test_refill_after_elapsed_time(self):
        """After enough time passes, tokens are refilled (via monkeypatch)."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)

        # Consume all tokens at t=0
        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            await limiter.allow("k")
            await limiter.allow("k")
            self.assertFalse(await limiter.allow("k"))

        # Advance time by 1.5 seconds → should refill 1.5 tokens
        with patch("app.core.limiter.time.monotonic", return_value=1.5):
            self.assertTrue(await limiter.allow("k"))  # cost=1, 1.5-1=0.5 remaining
            self.assertFalse(await limiter.allow("k"))  # cost=1, only 0.5 left

    async def test_refill_caps_at_capacity(self):
        """Refill does not exceed capacity."""
        limiter = TokenBucketLimiter(capacity=3, refill_rate=100.0)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            await limiter.allow("k")  # 3→2
            await limiter.allow("k")  # 2→1
            await limiter.allow("k")  # 1→0

        # Fast refill: 100 tokens/sec * 1s = 100 tokens, but capped at capacity=3
        with patch("app.core.limiter.time.monotonic", return_value=1.0):
            self.assertTrue(await limiter.allow("k"))  # 3→2
            self.assertTrue(await limiter.allow("k"))  # 2→1
            self.assertTrue(await limiter.allow("k"))  # 1→0
            self.assertFalse(await limiter.allow("k"))

    async def test_independent_keys(self):
        """Different keys are rate-limited independently."""
        limiter = TokenBucketLimiter(capacity=1, refill_rate=0.1)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            self.assertTrue(await limiter.allow("key_a"))
            self.assertTrue(await limiter.allow("key_b"))
            self.assertFalse(await limiter.allow("key_a"))
            self.assertFalse(await limiter.allow("key_b"))

    async def test_custom_cost(self):
        """Custom cost consumes multiple tokens at once."""
        limiter = TokenBucketLimiter(capacity=5, refill_rate=1.0)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            self.assertTrue(await limiter.allow("k", cost=3))  # 5→2
            self.assertFalse(await limiter.allow("k", cost=3))  # only 2 left
            self.assertTrue(await limiter.allow("k", cost=2))  # 2→0
            self.assertFalse(await limiter.allow("k", cost=1))

    async def test_burst_parameter(self):
        """burst overrides capacity for initial bucket size."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0, burst=5)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            for _ in range(5):
                self.assertTrue(await limiter.allow("k"))
            self.assertFalse(await limiter.allow("k"))

    async def test_prune_stale_buckets(self):
        """Stale buckets are pruned after prune interval."""
        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            limiter = TokenBucketLimiter(capacity=1, refill_rate=1.0)
            limiter._prune_interval = 0  # Always prune
            await limiter.allow("stale_key")

        # Move far enough into the future to exceed _max_idle_sec
        future_time = limiter._max_idle_sec + 100
        with patch("app.core.limiter.time.monotonic", return_value=future_time):
            await limiter.allow("trigger_prune")  # triggers prune

        self.assertNotIn("stale_key", limiter._buckets)


class TestRedisTokenBucketLimiter(unittest.IsolatedAsyncioTestCase):
    """Test RedisTokenBucketLimiter via mocked Redis client."""

    def setUp(self):
        from app.core.limiter import RedisTokenBucketLimiter

        self.mock_redis = unittest.mock.MagicMock()
        self.mock_script = unittest.mock.AsyncMock()
        self.mock_redis.register_script = unittest.mock.MagicMock(
            return_value=self.mock_script
        )
        self.limiter = RedisTokenBucketLimiter(
            self.mock_redis, capacity=10, refill_rate=1.0
        )

    async def test_allow_returns_true_when_script_returns_1(self):
        self.mock_script.return_value = 1
        result = await self.limiter.allow("test_key")
        self.assertTrue(result)
        self.mock_script.assert_awaited_once()
        # Verify the key was namespaced
        call_kwargs = self.mock_script.call_args
        self.assertEqual(call_kwargs.kwargs["keys"], ["ratelimit:test_key"])

    async def test_allow_returns_false_when_script_returns_0(self):
        self.mock_script.return_value = 0
        result = await self.limiter.allow("test_key")
        self.assertFalse(result)

    async def test_allow_returns_true_on_redis_error(self):
        """If Redis is unreachable, limiter should fail-open."""
        self.mock_script.side_effect = ConnectionError("Redis down")
        result = await self.limiter.allow("test_key")
        self.assertTrue(result)

    async def test_register_script_called_once(self):
        """Script should be registered at construction time via EVALSHA."""
        self.mock_redis.register_script.assert_called_once()


if __name__ == "__main__":
    unittest.main()

