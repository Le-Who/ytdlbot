import msgspec
from typing import Any, Optional, Type, TypeVar
import logging

from .base import StateStorage

T = TypeVar("T")
logger = logging.getLogger("app.storage.redis")

class RedisStorage(StateStorage):
    def __init__(self, redis_client: Any, default_ttl: int):
        self.redis = redis_client
        self.default_ttl = default_ttl

    async def get(self, key: str, type_hint: Any = None) -> Any:
        data = await self.redis.get(key)
        if not data:
            return None
        try:
            return msgspec.json.decode(data, type=type_hint)
        except Exception as e:
            logger.error("Redis decode error for key %s: %s", key, str(e))
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        try:
            data = msgspec.json.encode(value)
            await self.redis.set(key, data, ex=ttl or self.default_ttl)
        except Exception as e:
            logger.error("Redis encode error for key %s: %s", key, str(e))

    async def delete(self, key: str) -> None:
        await self.redis.delete(key)

    async def clear(self) -> None:
        await self.redis.flushdb()
