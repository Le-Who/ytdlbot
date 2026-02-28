import unittest
from unittest.mock import MagicMock
from app.core.cache import FileTTLCache


class TestFileTTLCache(unittest.TestCase):
    def setUp(self):
        self.mock_eviction = MagicMock()
        # Default cache for most tests
        self.cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=self.mock_eviction)

    def test_popitem_eviction(self):
        """Test that popitem triggers on_eviction."""
        self.cache["key1"] = "value1"
        key, value = self.cache.popitem()

        self.assertEqual(key, "key1")
        self.assertEqual(value, "value1")
        self.mock_eviction.assert_called_once_with("value1")

    def test_pop_eviction(self):
        """Test that pop triggers on_eviction when key exists."""
        self.cache["key1"] = "value1"
        value = self.cache.pop("key1")

        self.assertEqual(value, "value1")
        self.mock_eviction.assert_called_once_with("value1")

    def test_pop_default_no_eviction(self):
        """Test that pop with default does not trigger on_eviction when key is missing."""
        value = self.cache.pop("missing_key", default="default_val")

        self.assertEqual(value, "default_val")
        self.mock_eviction.assert_not_called()

    def test_clear_eviction(self):
        """Test that clear triggers on_eviction for all items."""
        self.cache["key1"] = "value1"
        self.cache["key2"] = "value2"

        self.cache.clear()

        self.assertEqual(self.mock_eviction.call_count, 2)
        # Verify calls were made for both values. Order is not guaranteed in dicts/caches generally,
        # but check any order.
        self.mock_eviction.assert_any_call("value1")
        self.mock_eviction.assert_any_call("value2")
        self.assertEqual(len(self.cache), 0)

    def test_eviction_on_overflow(self):
        """Test that adding items beyond maxsize triggers on_eviction via popitem."""
        # Create a small cache
        small_cache = FileTTLCache(maxsize=1, ttl=60, on_eviction=self.mock_eviction)

        small_cache["key1"] = "value1"
        # Adding second item should evict the first one (LRU policy by default in cachetools)
        small_cache["key2"] = "value2"

        self.mock_eviction.assert_called_once_with("value1")
        self.assertIn("key2", small_cache)
        self.assertNotIn("key1", small_cache)

    def test_no_callback(self):
        """Test that cache works fine without on_eviction callback."""
        cache_no_cb = FileTTLCache(maxsize=10, ttl=60)

        cache_no_cb["key1"] = "value1"

        # Test popitem
        key, value = cache_no_cb.popitem()
        self.assertEqual(key, "key1")
        self.assertEqual(value, "value1")

        # Test pop
        cache_no_cb["key2"] = "value2"
        value = cache_no_cb.pop("key2")
        self.assertEqual(value, "value2")

        # Test clear
        cache_no_cb["key3"] = "value3"
        cache_no_cb.clear()
        self.assertEqual(len(cache_no_cb), 0)

if __name__ == "__main__":
    unittest.main()
