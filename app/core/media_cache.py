"""Versioned media cache records and owner-token distributed leases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from app.services.media.models import MediaKind, MediaRequest, request_cache_key

STABLE_METADATA_TTL_SECONDS = 24 * 60 * 60
SIGNED_URL_TTL_SECONDS = 5 * 60
TELEGRAM_FILE_ID_TTL_SECONDS = 30 * 24 * 60 * 60
CACHE_SCHEMA_VERSION = "v1"

COMPARE_AND_DELETE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
""".strip()

COMPARE_AND_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
""".strip()

RecordType = Literal["metadata", "signed-url", "file-id"]
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "x-api-key"}
)


class RedisClient(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    async def set(self, key: str, value: bytes | str, **kwargs: Any) -> Any: ...

    async def expire(self, key: str, seconds: int) -> Any: ...

    async def delete(self, key: str) -> Any: ...

    async def eval(self, script: str, key_count: int, key: str, *args: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class StableMediaMetadata:
    """Metadata safe for durable caching: deliberately no URL or local path."""

    platform: str
    media_id: str
    kind: MediaKind
    title: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    item_count: int = 1
    album_order: tuple[int, ...] = ()
    provider: str | None = None

    def __post_init__(self) -> None:
        if not self.platform or not self.media_id:
            raise ValueError("stable metadata requires platform and media identity")
        if self.duration_seconds is not None and (
            not math.isfinite(self.duration_seconds) or self.duration_seconds < 0
        ):
            raise ValueError("metadata duration must be finite and non-negative")
        if self.width is not None and self.width <= 0:
            raise ValueError("metadata width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("metadata height must be positive")
        if self.item_count < 1:
            raise ValueError("metadata item count must be positive")


@dataclass(frozen=True, slots=True)
class CachedSignedURL:
    """Short-lived transport data kept apart from stable metadata."""

    url: str
    provider: str
    expires_at: float
    variant_id: str = "default"
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("cached media URL must use HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("cached media URL must not contain credentials")
        if not self.provider or not self.variant_id:
            raise ValueError("signed URL requires provider and variant identity")
        if not math.isfinite(self.expires_at):
            raise ValueError("signed URL expiry must be finite")
        for name, value in self.headers:
            if name.lower() in _SENSITIVE_HEADERS:
                raise ValueError("credentials must not be persisted with signed URLs")
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ValueError("cached media headers must not contain newlines")


@dataclass(frozen=True, slots=True)
class CachedDelivery:
    """One Telegram file identifier, scoped to its bot and output item key."""

    file_id: str
    telegram_type: MediaKind | str
    item_index: int = 0
    file_unique_id: str | None = None

    def __post_init__(self) -> None:
        if not self.file_id:
            raise ValueError("cached delivery requires a Telegram file_id")
        if not str(self.telegram_type):
            raise ValueError("cached delivery requires a Telegram media type")
        if self.item_index < 0:
            raise ValueError("cached delivery item index must not be negative")


@dataclass(slots=True)
class _MemoryRecord:
    payload: bytes
    expires_at: float


@dataclass(slots=True)
class _MemoryLease:
    owner: str
    expires_at: float


class RedisLeaseManager:
    """Redis lease whose release and renewal compare an unguessable owner token."""

    def __init__(
        self,
        redis: RedisClient,
        *,
        prefix: str = "media-cache",
        clock: Any = time.time,
    ) -> None:
        self._redis = redis
        self._prefix = prefix.rstrip(":")
        self._clock = clock

    async def acquire(self, key: str, *, ttl: float) -> str | None:
        ttl_ms = _ttl_milliseconds(ttl)
        owner = secrets.token_urlsafe(24)
        acquired = await self._redis.set(self._key(key), owner, nx=True, px=ttl_ms)
        return owner if acquired else None

    async def renew(self, key: str, owner: str, *, ttl: float) -> bool:
        ttl_ms = _ttl_milliseconds(ttl)
        renewed = await self._redis.eval(
            COMPARE_AND_RENEW_LUA,
            1,
            self._key(key),
            owner,
            str(ttl_ms),
        )
        return bool(renewed)

    async def release(self, key: str, owner: str) -> bool:
        released = await self._redis.eval(
            COMPARE_AND_DELETE_LUA,
            1,
            self._key(key),
            owner,
        )
        return bool(released)

    def _key(self, key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"{self._prefix}:lease:{digest}"


class MemoryLeaseManager:
    """Process-local lease fallback with the same owner and expiry rules."""

    def __init__(self, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._leases: dict[str, _MemoryLease] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, *, ttl: float) -> str | None:
        _positive_ttl(ttl)
        async with self._lock:
            current = self._leases.get(key)
            if current is not None and current.expires_at > self._clock():
                return None
            owner = secrets.token_urlsafe(24)
            self._leases[key] = _MemoryLease(owner, self._clock() + ttl)
            return owner

    async def renew(self, key: str, owner: str, *, ttl: float) -> bool:
        _positive_ttl(ttl)
        async with self._lock:
            current = self._leases.get(key)
            if (
                current is None
                or current.owner != owner
                or current.expires_at <= self._clock()
            ):
                if current is not None and current.expires_at <= self._clock():
                    self._leases.pop(key, None)
                return False
            current.expires_at = self._clock() + ttl
            return True

    async def release(self, key: str, owner: str) -> bool:
        async with self._lock:
            current = self._leases.get(key)
            if (
                current is None
                or current.owner != owner
                or current.expires_at <= self._clock()
            ):
                if current is not None and current.expires_at <= self._clock():
                    self._leases.pop(key, None)
                return False
            self._leases.pop(key, None)
            return True


class MediaCache:
    """Typed cache with distinct namespaces and lifetime policies."""

    def __init__(
        self,
        redis: RedisClient | None = None,
        *,
        prefix: str = "media-cache",
        transform_version: str = "pipeline-v1",
        metadata_ttl: int = STABLE_METADATA_TTL_SECONDS,
        signed_url_ttl: int = SIGNED_URL_TTL_SECONDS,
        delivery_ttl: int = TELEGRAM_FILE_ID_TTL_SECONDS,
        max_memory_entries: int = 2048,
        clock: Any = time.time,
    ) -> None:
        for ttl in (metadata_ttl, signed_url_ttl, delivery_ttl):
            _positive_ttl(ttl)
        if not transform_version:
            raise ValueError("transform version must not be empty")
        if max_memory_entries < 1:
            raise ValueError("memory cache size must be positive")
        self._redis = redis
        self._prefix = prefix.rstrip(":")
        self._transform_version = transform_version
        self._metadata_ttl = metadata_ttl
        self._signed_url_ttl = signed_url_ttl
        self._delivery_ttl = delivery_ttl
        self._max_memory_entries = max_memory_entries
        self._clock = clock
        self._memory: dict[str, _MemoryRecord] = {}
        self._memory_lock = asyncio.Lock()
        self._leases: RedisLeaseManager | MemoryLeaseManager
        if redis is None:
            self._leases = MemoryLeaseManager(clock=clock)
        else:
            self._leases = RedisLeaseManager(redis, prefix=self._prefix, clock=clock)

    def record_key(
        self,
        record_type: RecordType,
        request: MediaRequest,
        *,
        bot_id: str | None = None,
        item_index: int = 0,
        variant_id: str = "default",
    ) -> str:
        if record_type == "file-id" and not bot_id:
            raise ValueError("Telegram file_id cache keys require bot_id")
        if item_index < 0:
            raise ValueError("cache item index must not be negative")
        scoped = _cache_scope_request(request)
        identity = {
            "request": request_cache_key(scoped),
            "transform": self._transform_version,
            "bot": bot_id if record_type == "file-id" else None,
            "item": item_index if record_type == "file-id" else None,
            "variant": variant_id if record_type == "signed-url" else None,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"{self._prefix}:{CACHE_SCHEMA_VERSION}:{record_type}:{digest}"

    async def get_metadata(self, request: MediaRequest) -> StableMediaMetadata | None:
        payload = await self._get(self.record_key("metadata", request))
        if payload is None:
            return None
        try:
            data = _json_object(payload)
            return StableMediaMetadata(
                platform=str(data["platform"]),
                media_id=str(data["media_id"]),
                kind=MediaKind(str(data["kind"])),
                title=_optional_string(data.get("title")),
                duration_seconds=_optional_float(data.get("duration_seconds")),
                width=_optional_int(data.get("width")),
                height=_optional_int(data.get("height")),
                item_count=int(data["item_count"]),
                album_order=tuple(int(value) for value in data["album_order"]),
                provider=_optional_string(data.get("provider")),
            )
        except (KeyError, TypeError, ValueError):
            await self._delete(self.record_key("metadata", request))
            return None

    async def put_metadata(
        self, request: MediaRequest, metadata: StableMediaMetadata
    ) -> None:
        if (
            metadata.platform != request.platform
            or metadata.media_id != request.media_id
        ):
            raise ValueError("metadata identity does not match the media request")
        payload = {
            "platform": metadata.platform,
            "media_id": metadata.media_id,
            "kind": metadata.kind.value,
            "title": metadata.title,
            "duration_seconds": metadata.duration_seconds,
            "width": metadata.width,
            "height": metadata.height,
            "item_count": metadata.item_count,
            "album_order": metadata.album_order,
            "provider": metadata.provider,
        }
        await self._set(
            self.record_key("metadata", request),
            _json_bytes(payload),
            self._metadata_ttl,
        )

    async def get_signed_url(
        self, request: MediaRequest, *, variant_id: str = "default"
    ) -> CachedSignedURL | None:
        key = self.record_key("signed-url", request, variant_id=variant_id)
        payload = await self._get(key)
        if payload is None:
            return None
        try:
            data = _json_object(payload)
            record = CachedSignedURL(
                url=str(data["url"]),
                provider=str(data["provider"]),
                expires_at=float(data["expires_at"]),
                variant_id=str(data["variant_id"]),
                headers=tuple(
                    (str(name), str(value)) for name, value in data["headers"]
                ),
            )
        except (KeyError, TypeError, ValueError):
            await self._delete(key)
            return None
        if record.expires_at <= self._clock():
            await self._delete(key)
            return None
        return record

    async def put_signed_url(
        self, request: MediaRequest, signed_url: CachedSignedURL
    ) -> None:
        remaining = signed_url.expires_at - self._clock()
        if remaining <= 0:
            await self._delete(
                self.record_key("signed-url", request, variant_id=signed_url.variant_id)
            )
            return
        ttl = max(1, min(self._signed_url_ttl, math.ceil(remaining)))
        payload = {
            "url": signed_url.url,
            "provider": signed_url.provider,
            "expires_at": signed_url.expires_at,
            "variant_id": signed_url.variant_id,
            "headers": signed_url.headers,
        }
        await self._set(
            self.record_key("signed-url", request, variant_id=signed_url.variant_id),
            _json_bytes(payload),
            ttl,
        )

    async def get_delivery(
        self,
        request: MediaRequest,
        *,
        bot_id: str,
        item_index: int = 0,
    ) -> CachedDelivery | None:
        key = self.record_key("file-id", request, bot_id=bot_id, item_index=item_index)
        payload = await self._get(key, touch_ttl=self._delivery_ttl)
        if payload is None:
            return None
        try:
            data = _json_object(payload)
            raw_type = str(data["telegram_type"])
            try:
                telegram_type: MediaKind | str = MediaKind(raw_type)
            except ValueError:
                telegram_type = raw_type
            return CachedDelivery(
                file_id=str(data["file_id"]),
                telegram_type=telegram_type,
                item_index=int(data["item_index"]),
                file_unique_id=_optional_string(data.get("file_unique_id")),
            )
        except (KeyError, TypeError, ValueError):
            await self._delete(key)
            return None

    async def put_delivery(
        self,
        request: MediaRequest,
        *,
        bot_id: str,
        delivery: CachedDelivery,
    ) -> None:
        payload = {
            "file_id": delivery.file_id,
            "telegram_type": str(delivery.telegram_type),
            "item_index": delivery.item_index,
            "file_unique_id": delivery.file_unique_id,
        }
        await self._set(
            self.record_key(
                "file-id",
                request,
                bot_id=bot_id,
                item_index=delivery.item_index,
            ),
            _json_bytes(payload),
            self._delivery_ttl,
        )

    async def evict_delivery(
        self,
        request: MediaRequest,
        *,
        bot_id: str,
        item_index: int = 0,
    ) -> None:
        await self._delete(
            self.record_key("file-id", request, bot_id=bot_id, item_index=item_index)
        )

    async def acquire_lease(self, key: str, *, ttl: float) -> str | None:
        return await self._leases.acquire(key, ttl=ttl)

    async def renew_lease(self, key: str, owner: str, *, ttl: float) -> bool:
        return await self._leases.renew(key, owner, ttl=ttl)

    async def release_lease(self, key: str, owner: str) -> bool:
        return await self._leases.release(key, owner)

    async def _get(self, key: str, *, touch_ttl: int | None = None) -> bytes | None:
        if self._redis is not None:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            if touch_ttl is not None and not await self._redis.expire(key, touch_ttl):
                return None
            return raw.encode() if isinstance(raw, str) else bytes(raw)
        async with self._memory_lock:
            record = self._memory.get(key)
            if record is None:
                return None
            if record.expires_at <= self._clock():
                self._memory.pop(key, None)
                return None
            if touch_ttl is not None:
                record.expires_at = self._clock() + touch_ttl
            return record.payload

    async def _set(self, key: str, payload: bytes, ttl: int) -> None:
        if self._redis is not None:
            await self._redis.set(key, payload, ex=ttl)
            return
        async with self._memory_lock:
            self._prune_memory()
            if (
                key not in self._memory
                and len(self._memory) >= self._max_memory_entries
            ):
                oldest = min(
                    self._memory,
                    key=lambda entry_key: self._memory[entry_key].expires_at,
                )
                self._memory.pop(oldest, None)
            self._memory[key] = _MemoryRecord(payload, self._clock() + ttl)

    async def _delete(self, key: str) -> None:
        if self._redis is not None:
            await self._redis.delete(key)
            return
        async with self._memory_lock:
            self._memory.pop(key, None)

    def _prune_memory(self) -> None:
        now = self._clock()
        expired = [
            key for key, record in self._memory.items() if record.expires_at <= now
        ]
        for key in expired:
            self._memory.pop(key, None)


def _cache_scope_request(request: MediaRequest) -> MediaRequest:
    if request.auth_scope == "public":
        return replace(request, caller_scope="public")
    return request


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _json_object(payload: bytes) -> dict[str, Any]:
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise TypeError("cache record is not an object")
    return decoded


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _positive_ttl(ttl: float) -> None:
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("cache TTL must be finite and positive")


def _ttl_milliseconds(ttl: float) -> int:
    _positive_ttl(ttl)
    return max(1, math.ceil(ttl * 1000))
