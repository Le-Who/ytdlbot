import logging
import zlib
from typing import Any, TypeVar

import msgspec

from .base import StateStorage

T = TypeVar("T")
logger = logging.getLogger("app.storage.redis")

_RAW_PREFIX = b"\x00"
_COMPRESSED_PREFIX = b"\x01"
_COMPRESS_THRESHOLD = 1024


class RedisStorage(StateStorage):
    def __init__(
        self,
        redis_client: Any,
        default_ttl: int,
        prefix: str = "",
    ):
        self.redis = redis_client
        self.default_ttl = default_ttl
        self.prefix = f"{prefix}:" if prefix else ""

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    @staticmethod
    def _encode(value: Any) -> bytes:
        """msgpack-encode *value* and optionally zlib-compress."""
        raw = msgspec.msgpack.encode(value)
        if len(raw) > _COMPRESS_THRESHOLD:
            return _COMPRESSED_PREFIX + zlib.compress(raw, level=1)
        return _RAW_PREFIX + raw

    @staticmethod
    def _decode(data: bytes, type_hint: Any = None) -> Any:
        """Decode data written by ``_encode``.

        Supports a transparent fallback to ``msgspec.json`` for
        backward-compatibility with values written before this change
        (avoids the need for a manual cache flush on deploy).
        """
        if data[0:1] == _COMPRESSED_PREFIX:
            payload = zlib.decompress(data[1:])
        elif data[0:1] == _RAW_PREFIX:
            payload = data[1:]
        else:
            if type_hint is None or type_hint is Any:
                return msgspec.json.decode(data)
            return msgspec.json.decode(data, type=type_hint)

        if type_hint is None or type_hint is Any:
            return msgspec.msgpack.decode(payload)
        return msgspec.msgpack.decode(payload, type=type_hint)

    async def get(self, key: str, type_hint: Any = None) -> Any:
        data = await self.redis.get(self._key(key))
        if not data:
            return None
        try:
            return self._decode(data, type_hint)
        except Exception as e:  # noqa: BLE001
            logger.error("Redis decode error for key %s: %s", key, str(e))
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            data = self._encode(value)
            await self.redis.set(self._key(key), data, ex=ttl or self.default_ttl)
        except Exception as e:  # noqa: BLE001
            logger.error("Redis encode error for key %s: %s", key, str(e))

    async def delete(self, key: str) -> None:
        await self.redis.delete(self._key(key))

    async def clear(self) -> None:
        if not self.prefix:
            await self.redis.flushdb()
            return
        pattern = (
            "".join(
                f"\\{character}" if character in "\\*?[]" else character
                for character in self.prefix
            )
            + "*"
        )
        keys = []
        async for key in self.redis.scan_iter(match=pattern, count=100):
            prefix = self.prefix.encode() if isinstance(key, bytes) else self.prefix
            if key.startswith(prefix):
                keys.append(key)
                if len(keys) == 100:
                    await self.redis.delete(*keys)
                    keys.clear()
        if keys:
            await self.redis.delete(*keys)
