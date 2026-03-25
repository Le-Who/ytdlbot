import unittest
from unittest.mock import AsyncMock, patch
import msgspec
import zlib
from typing import Any

from app.core.storage.redis_storage import (
    RedisStorage,
    _RAW_PREFIX,
    _COMPRESSED_PREFIX,
    _COMPRESS_THRESHOLD,
)


class TestRedisStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_redis = AsyncMock()
        self.default_ttl = 3600
        self.storage = RedisStorage(
            redis_client=self.mock_redis, default_ttl=self.default_ttl
        )

    # ── GET tests ─────────────────────────────────────────────────────

    async def test_get_missing_data(self):
        self.mock_redis.get.return_value = None
        result = await self.storage.get("missing_key")
        self.assertIsNone(result)
        self.mock_redis.get.assert_awaited_once_with("missing_key")

    async def test_get_success_no_type_hint(self):
        test_data = {"hello": "world"}
        encoded = _RAW_PREFIX + msgspec.msgpack.encode(test_data)
        self.mock_redis.get.return_value = encoded

        result = await self.storage.get("test_key")
        self.assertEqual(result, test_data)
        self.mock_redis.get.assert_awaited_once_with("test_key")

    async def test_get_success_type_hint_any(self):
        test_data = {"hello": "world"}
        encoded = _RAW_PREFIX + msgspec.msgpack.encode(test_data)
        self.mock_redis.get.return_value = encoded

        result = await self.storage.get("test_key", type_hint=Any)
        self.assertEqual(result, test_data)

    async def test_get_success_with_type_hint(self):
        class MyStruct(msgspec.Struct):
            name: str
            age: int

        test_data = MyStruct(name="Alice", age=30)
        encoded = _RAW_PREFIX + msgspec.msgpack.encode(test_data)
        self.mock_redis.get.return_value = encoded

        result = await self.storage.get("test_key", type_hint=MyStruct)
        self.assertIsInstance(result, MyStruct)
        self.assertEqual(result.name, "Alice")
        self.assertEqual(result.age, 30)

    @patch("app.core.storage.redis_storage.logger")
    async def test_get_decode_error(self, mock_logger):
        # Starts with raw prefix but contains invalid msgpack
        self.mock_redis.get.return_value = _RAW_PREFIX + b"invalid"

        result = await self.storage.get("test_key")
        self.assertIsNone(result)
        mock_logger.error.assert_called_once()
        self.assertEqual(
            "Redis decode error for key %s: %s", mock_logger.error.call_args[0][0]
        )

    # ── SET tests ─────────────────────────────────────────────────────

    async def test_set_success_default_ttl(self):
        test_data = {"key": "value"}
        await self.storage.set("test_key", test_data)

        call_args = self.mock_redis.set.call_args
        self.assertEqual(call_args.args[0], "test_key")
        # Verify it's a properly prefixed msgpack blob
        data = call_args.args[1]
        self.assertEqual(data[0:1], _RAW_PREFIX)
        decoded = msgspec.msgpack.decode(data[1:])
        self.assertEqual(decoded, test_data)
        self.assertEqual(call_args.kwargs.get("ex"), self.default_ttl)

    async def test_set_success_custom_ttl(self):
        test_data = [1, 2, 3]
        custom_ttl = 60
        await self.storage.set("test_key", test_data, ttl=custom_ttl)

        call_args = self.mock_redis.set.call_args
        self.assertEqual(call_args.kwargs.get("ex"), custom_ttl)

    @patch("app.core.storage.redis_storage.logger")
    async def test_set_encode_error(self, mock_logger):
        class Unserializable:
            pass

        await self.storage.set("test_key", Unserializable())

        self.mock_redis.set.assert_not_awaited()
        mock_logger.error.assert_called_once()
        self.assertEqual(
            "Redis encode error for key %s: %s", mock_logger.error.call_args[0][0]
        )

    # ── DELETE / CLEAR ────────────────────────────────────────────────

    async def test_delete(self):
        await self.storage.delete("test_key")
        self.mock_redis.delete.assert_awaited_once_with("test_key")

    async def test_clear(self):
        await self.storage.clear()
        self.mock_redis.flushdb.assert_awaited_once()


class TestRedisStorageCompression(unittest.IsolatedAsyncioTestCase):
    """Tests specifically for the msgpack + zlib compression layer."""

    def setUp(self):
        self.mock_redis = AsyncMock()
        self.storage = RedisStorage(
            redis_client=self.mock_redis, default_ttl=600
        )

    async def test_small_payload_not_compressed(self):
        """Payloads smaller than threshold should NOT be zlib-compressed."""
        small_data = {"x": 1}
        await self.storage.set("k", small_data)

        stored = self.mock_redis.set.call_args.args[1]
        self.assertEqual(stored[0:1], _RAW_PREFIX)
        self.assertEqual(msgspec.msgpack.decode(stored[1:]), small_data)

    async def test_large_payload_compressed(self):
        """Payloads larger than threshold SHOULD be zlib-compressed."""
        large_data = {"data": "x" * (_COMPRESS_THRESHOLD + 100)}
        await self.storage.set("k", large_data)

        stored = self.mock_redis.set.call_args.args[1]
        self.assertEqual(stored[0:1], _COMPRESSED_PREFIX)

        # Manually decompress and verify
        decompressed = zlib.decompress(stored[1:])
        self.assertEqual(msgspec.msgpack.decode(decompressed), large_data)

    async def test_roundtrip_compressed(self):
        """Write large data, read it back — the decode path handles compression."""
        large_data = {"items": list(range(500))}
        encoded = RedisStorage._encode(large_data)
        self.assertEqual(encoded[0:1], _COMPRESSED_PREFIX)

        self.mock_redis.get.return_value = encoded
        result = await self.storage.get("k")
        self.assertEqual(result, large_data)

    async def test_backward_compat_json_fallback(self):
        """Legacy JSON-encoded values (no magic prefix) should still decode."""
        legacy_data = {"legacy": True}
        legacy_bytes = msgspec.json.encode(legacy_data)
        # Simulates old data: no \x00/\x01 prefix, just raw JSON bytes
        self.mock_redis.get.return_value = legacy_bytes

        result = await self.storage.get("old_key")
        self.assertEqual(result, legacy_data)


class TestRedisStorageNamespacing(unittest.IsolatedAsyncioTestCase):
    """Tests for key prefix/namespacing."""

    def setUp(self):
        self.mock_redis = AsyncMock()

    async def test_prefix_applied_to_get(self):
        storage = RedisStorage(
            redis_client=self.mock_redis, default_ttl=600, prefix="lnk"
        )
        self.mock_redis.get.return_value = None
        await storage.get("some_token")
        self.mock_redis.get.assert_awaited_once_with("lnk:some_token")

    async def test_prefix_applied_to_set(self):
        storage = RedisStorage(
            redis_client=self.mock_redis, default_ttl=600, prefix="inf"
        )
        await storage.set("url", {"a": 1})
        key_used = self.mock_redis.set.call_args.args[0]
        self.assertEqual(key_used, "inf:url")

    async def test_prefix_applied_to_delete(self):
        storage = RedisStorage(
            redis_client=self.mock_redis, default_ttl=600, prefix="can"
        )
        await storage.delete("tok")
        self.mock_redis.delete.assert_awaited_once_with("can:tok")

    async def test_no_prefix_when_empty(self):
        storage = RedisStorage(
            redis_client=self.mock_redis, default_ttl=600, prefix=""
        )
        self.mock_redis.get.return_value = None
        await storage.get("raw_key")
        self.mock_redis.get.assert_awaited_once_with("raw_key")


if __name__ == "__main__":
    unittest.main()
