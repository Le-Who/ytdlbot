import unittest
import asyncio
from app.core.storage.memory import MemoryStorage


class TestMemoryStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage(maxsize=3, ttl=1)

    async def test_set_and_get(self):
        """Store a value and retrieve it."""
        await self.storage.set("key1", "value1")
        result = await self.storage.get("key1")
        self.assertEqual(result, "value1")

    async def test_get_missing_key(self):
        """Retrieve a value for a key that does not exist."""
        result = await self.storage.get("missing_key")
        self.assertIsNone(result)

    async def test_set_ttl_ignored(self):
        """The ttl parameter passed to set is ignored, but set works."""
        await self.storage.set("key2", "value2", ttl=100)
        result = await self.storage.get("key2")
        self.assertEqual(result, "value2")

    async def test_delete_key(self):
        """Store a value, delete it, and ensure it's removed."""
        await self.storage.set("key3", "value3")
        await self.storage.delete("key3")
        result = await self.storage.get("key3")
        self.assertIsNone(result)

    async def test_delete_missing_key(self):
        """Deleting a non-existent key shouldn't raise an error."""
        # Should not raise any exception
        await self.storage.delete("missing_key")
        result = await self.storage.get("missing_key")
        self.assertIsNone(result)

    async def test_clear(self):
        """Store multiple values, call clear(), and ensure all are removed."""
        await self.storage.set("key1", "val1")
        await self.storage.set("key2", "val2")
        await self.storage.clear()

        self.assertIsNone(await self.storage.get("key1"))
        self.assertIsNone(await self.storage.get("key2"))

    async def test_maxsize_eviction(self):
        """Store more values than maxsize, and ensure the oldest is evicted."""
        await self.storage.set("key1", "val1")
        await self.storage.set("key2", "val2")
        await self.storage.set("key3", "val3")
        await self.storage.set("key4", "val4")  # This should evict key1

        self.assertIsNone(await self.storage.get("key1"))
        self.assertEqual(await self.storage.get("key2"), "val2")
        self.assertEqual(await self.storage.get("key3"), "val3")
        self.assertEqual(await self.storage.get("key4"), "val4")

    async def test_ttl_expiration(self):
        """Store a value, wait for ttl to expire, and ensure it's removed."""
        # The TTL is set to 1 second in setUp
        await self.storage.set("key1", "val1")

        # It should be present immediately
        self.assertEqual(await self.storage.get("key1"), "val1")

        # Wait slightly more than 1 second
        await asyncio.sleep(1.1)

        # Now it should be expired
        self.assertIsNone(await self.storage.get("key1"))


if __name__ == "__main__":
    unittest.main()
