import unittest
from unittest.mock import AsyncMock, patch
import msgspec
from typing import Any

from app.core.storage.redis_storage import RedisStorage


class TestRedisStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_redis = AsyncMock()
        self.default_ttl = 3600
        self.storage = RedisStorage(redis_client=self.mock_redis, default_ttl=self.default_ttl)

    async def test_get_missing_data(self):
        self.mock_redis.get.return_value = None
        result = await self.storage.get("missing_key")
        self.assertIsNone(result)
        self.mock_redis.get.assert_awaited_once_with("missing_key")

    async def test_get_success_no_type_hint(self):
        test_data = {"hello": "world"}
        self.mock_redis.get.return_value = msgspec.json.encode(test_data)

        result = await self.storage.get("test_key")
        self.assertEqual(result, test_data)
        self.mock_redis.get.assert_awaited_once_with("test_key")

    async def test_get_success_type_hint_any(self):
        test_data = {"hello": "world"}
        self.mock_redis.get.return_value = msgspec.json.encode(test_data)

        result = await self.storage.get("test_key", type_hint=Any)
        self.assertEqual(result, test_data)
        self.mock_redis.get.assert_awaited_once_with("test_key")

    async def test_get_success_with_type_hint(self):
        class MyStruct(msgspec.Struct):
            name: str
            age: int

        test_data = MyStruct(name="Alice", age=30)
        self.mock_redis.get.return_value = msgspec.json.encode(test_data)

        result = await self.storage.get("test_key", type_hint=MyStruct)
        self.assertIsInstance(result, MyStruct)
        self.assertEqual(result.name, "Alice")
        self.assertEqual(result.age, 30)
        self.mock_redis.get.assert_awaited_once_with("test_key")

    @patch("app.core.storage.redis_storage.logger")
    async def test_get_decode_error(self, mock_logger):
        self.mock_redis.get.return_value = b"invalid json"

        result = await self.storage.get("test_key")
        self.assertIsNone(result)
        mock_logger.error.assert_called_once()
        self.assertEqual("Redis decode error for key %s: %s", mock_logger.error.call_args[0][0])
        self.assertEqual("test_key", mock_logger.error.call_args[0][1])

    async def test_set_success_default_ttl(self):
        test_data = {"key": "value"}
        await self.storage.set("test_key", test_data)

        self.mock_redis.set.assert_awaited_once_with(
            "test_key",
            msgspec.json.encode(test_data),
            ex=self.default_ttl
        )

    async def test_set_success_custom_ttl(self):
        test_data = [1, 2, 3]
        custom_ttl = 60
        await self.storage.set("test_key", test_data, ttl=custom_ttl)

        self.mock_redis.set.assert_awaited_once_with(
            "test_key",
            msgspec.json.encode(test_data),
            ex=custom_ttl
        )

    @patch("app.core.storage.redis_storage.logger")
    async def test_set_encode_error(self, mock_logger):
        # Create something that cannot be serialized by msgspec
        class Unserializable:
            pass

        test_data = Unserializable()

        await self.storage.set("test_key", test_data)

        # Redis set should not be called
        self.mock_redis.set.assert_not_awaited()
        # Logger should log the error
        mock_logger.error.assert_called_once()
        self.assertEqual("Redis encode error for key %s: %s", mock_logger.error.call_args[0][0])
        self.assertEqual("test_key", mock_logger.error.call_args[0][1])

    async def test_delete(self):
        await self.storage.delete("test_key")
        self.mock_redis.delete.assert_awaited_once_with("test_key")

    async def test_clear(self):
        await self.storage.clear()
        self.mock_redis.flushdb.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
