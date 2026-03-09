"""Tests for TokenBucketLimiter — deterministic, no time.sleep."""

import unittest
from unittest.mock import patch

from app.core.limiter import TokenBucketLimiter


class TestTokenBucketLimiter(unittest.TestCase):
    """Test token bucket rate limiter with monkeypatched time."""

    def test_basic_consumption(self):
        """Consuming tokens up to capacity, then blocked."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
        self.assertTrue(limiter.allow("k"))
        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))

    def test_refill_after_elapsed_time(self):
        """After enough time passes, tokens are refilled (via monkeypatch)."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)

        # Consume all tokens at t=0
        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            limiter.allow("k")
            limiter.allow("k")
            self.assertFalse(limiter.allow("k"))

        # Advance time by 1.5 seconds → should refill 1.5 tokens
        with patch("app.core.limiter.time.monotonic", return_value=1.5):
            self.assertTrue(limiter.allow("k"))  # cost=1, 1.5-1=0.5 remaining
            self.assertFalse(limiter.allow("k"))  # cost=1, only 0.5 left

    def test_refill_caps_at_capacity(self):
        """Refill does not exceed capacity."""
        limiter = TokenBucketLimiter(capacity=3, refill_rate=100.0)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            limiter.allow("k")  # 3→2
            limiter.allow("k")  # 2→1
            limiter.allow("k")  # 1→0

        # Fast refill: 100 tokens/sec * 1s = 100 tokens, but capped at capacity=3
        with patch("app.core.limiter.time.monotonic", return_value=1.0):
            self.assertTrue(limiter.allow("k"))  # 3→2
            self.assertTrue(limiter.allow("k"))  # 2→1
            self.assertTrue(limiter.allow("k"))  # 1→0
            self.assertFalse(limiter.allow("k"))

    def test_independent_keys(self):
        """Different keys are rate-limited independently."""
        limiter = TokenBucketLimiter(capacity=1, refill_rate=0.1)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            self.assertTrue(limiter.allow("key_a"))
            self.assertTrue(limiter.allow("key_b"))
            self.assertFalse(limiter.allow("key_a"))
            self.assertFalse(limiter.allow("key_b"))

    def test_custom_cost(self):
        """Custom cost consumes multiple tokens at once."""
        limiter = TokenBucketLimiter(capacity=5, refill_rate=1.0)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            self.assertTrue(limiter.allow("k", cost=3))  # 5→2
            self.assertFalse(limiter.allow("k", cost=3))  # only 2 left
            self.assertTrue(limiter.allow("k", cost=2))  # 2→0
            self.assertFalse(limiter.allow("k", cost=1))

    def test_burst_parameter(self):
        """burst overrides capacity for initial bucket size."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0, burst=5)

        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            for _ in range(5):
                self.assertTrue(limiter.allow("k"))
            self.assertFalse(limiter.allow("k"))

    def test_prune_stale_buckets(self):
        """Stale buckets are pruned after prune interval."""
        with patch("app.core.limiter.time.monotonic", return_value=0.0):
            limiter = TokenBucketLimiter(capacity=1, refill_rate=1.0)
            limiter._prune_interval = 0  # Always prune
            limiter.allow("stale_key")

        # Move far enough into the future to exceed _max_idle_sec
        future_time = limiter._max_idle_sec + 100
        with patch("app.core.limiter.time.monotonic", return_value=future_time):
            limiter.allow("trigger_prune")  # triggers prune

        self.assertNotIn("stale_key", limiter._buckets)


if __name__ == "__main__":
    unittest.main()
