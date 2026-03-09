from typing import Any, Optional, TypeVar
from cachetools import TTLCache

from .base import StateStorage

T = TypeVar("T")

class MemoryStorage(StateStorage):
    def __init__(self, maxsize: int, ttl: int):
        self._cache = TTLCache(maxsize=maxsize, ttl=ttl)

    async def get(self, key: str, type_hint: Any = None) -> Any:
        return self._cache.get(key)

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._cache[key] = value

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    async def clear(self) -> None:
        self._cache.clear()
