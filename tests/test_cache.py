"""Tests for app.core.cache — FileTTLCache eviction callbacks."""

import unittest

from app.core.cache import FileTTLCache


class TestFileTTLCache(unittest.TestCase):
    """Test eviction callback behavior in FileTTLCache."""

    def test_popitem_triggers_eviction(self):
        """popitem calls on_eviction with the evicted value."""
        evicted = []
        cache = FileTTLCache(maxsize=2, ttl=60, on_eviction=evicted.append)
        cache["a"] = "val_a"
        cache["b"] = "val_b"
        cache["c"] = "val_c"  # forces eviction of oldest

        self.assertEqual(evicted, ["val_a"])

    def test_pop_triggers_eviction(self):
        """pop calls on_eviction when key exists."""
        evicted = []
        cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=evicted.append)
        cache["key"] = "value"

        result = cache.pop("key")
        self.assertEqual(result, "value")
        self.assertEqual(evicted, ["value"])

    def test_pop_missing_key_no_eviction(self):
        """pop with missing key does NOT call on_eviction."""
        evicted = []
        cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=evicted.append)

        result = cache.pop("missing", "default")
        self.assertEqual(result, "default")
        self.assertEqual(evicted, [])

    def test_clear_triggers_eviction_for_all(self):
        """clear calls on_eviction once for each removed item."""
        evicted = []
        cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=evicted.append)
        cache["a"] = "val_a"
        cache["b"] = "val_b"
        cache["c"] = "val_c"

        cache.clear()
        self.assertEqual(evicted, ["val_a", "val_b", "val_c"])

    def test_expiry_calls_eviction_once_with_fake_clock(self):
        now = [0.0]
        evicted = []
        cache = FileTTLCache(
            maxsize=2, ttl=10, timer=lambda: now[0], on_eviction=evicted.append
        )
        cache["a"] = "A"
        now[0] = 5
        cache["b"] = "B"
        now[0] = 10
        self.assertEqual(cache.expire(), [("a", "A")])
        self.assertEqual(evicted, ["A"])
        self.assertEqual(cache.expire(), [])
        self.assertEqual(cache["b"], "B")
        now[0] = 15
        cache.clear()
        self.assertEqual(evicted, ["A", "B"])
        self.assertEqual(len(cache), 0)

    def test_no_eviction_callback(self):
        """When on_eviction is None, operations still work."""
        cache = FileTTLCache(maxsize=2, ttl=60)
        cache["a"] = "val_a"
        cache["b"] = "val_b"
        cache["c"] = "val_c"  # evicts oldest, no crash
        cache.pop("b")
        cache.clear()


if __name__ == "__main__":
    unittest.main()
