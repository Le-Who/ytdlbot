from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from app.core.media_cache import (
    CachedDelivery,
    CachedSignedURL,
    MediaCache,
    RedisLeaseManager,
    StableMediaMetadata,
)
from app.services.media.models import (
    ClipInterval,
    MediaKind,
    MediaRequest,
    QualityPolicy,
)

DAY = 24 * 60 * 60


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRedis:
    """Small Redis boundary fake with expiry and atomic script semantics."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.values: dict[str, tuple[bytes, float | None]] = {}
        self.eval_calls = 0

    def _live(self, key: str) -> tuple[bytes, float | None] | None:
        value = self.values.get(key)
        if value is not None and value[1] is not None and value[1] <= self.clock():
            self.values.pop(key, None)
            return None
        return value

    async def get(self, key: str) -> bytes | None:
        value = self._live(key)
        return value[0] if value is not None else None

    async def set(
        self,
        key: str,
        value: bytes | str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        if nx and self._live(key) is not None:
            return None
        encoded = value.encode() if isinstance(value, str) else value
        ttl = ex if ex is not None else (px / 1000 if px is not None else None)
        self.values[key] = (
            encoded,
            None if ttl is None else self.clock() + ttl,
        )
        return True

    async def expire(self, key: str, ttl: int) -> bool:
        value = self._live(key)
        if value is None:
            return False
        self.values[key] = (value[0], self.clock() + ttl)
        return True

    async def delete(self, key: str) -> int:
        if ":lease:" in key:
            raise AssertionError("lease deletion must use compare-and-delete Lua")
        return int(self.values.pop(key, None) is not None)

    async def eval(
        self, script: str, key_count: int, key: str, owner: str, *args: str
    ) -> int:
        del key_count
        self.eval_calls += 1
        value = self._live(key)
        if value is None or value[0].decode() != owner:
            return 0
        if "del" in script:
            self.values.pop(key, None)
            return 1
        ttl_ms = int(args[0])
        self.values[key] = (value[0], self.clock() + ttl_ms / 1000)
        return 1


def request(**changes: Any) -> MediaRequest:
    base = MediaRequest.from_url(
        "https://youtu.be/abc123",
        kind=MediaKind.VIDEO,
        quality=QualityPolicy(max_edge=1080),
        audio_format="mp3",
        audio_language="uk",
        clip=ClipInterval(10, 20),
        album_selection=(3, 1),
        watermark_allowed=False,
        caller_scope="chat:1",
        auth_scope="public",
        exact=True,
    )
    return replace(base, **changes)


def delivery(file_id: str = "cached-file-id") -> CachedDelivery:
    return CachedDelivery(
        file_id=file_id,
        telegram_type=MediaKind.VIDEO,
        item_index=0,
    )


@pytest.mark.asyncio
async def test_public_file_id_reuse_is_bot_scoped_and_sliding_for_thirty_days():
    """Catches per-chat fragmentation, cross-bot reuse, and fixed (non-sliding) TTL."""
    clock = Clock()
    cache = MediaCache(clock=clock)
    first = request(caller_scope="chat:1")
    equivalent = request(caller_scope="chat:999")

    await cache.put_delivery(first, bot_id="bot-a", delivery=delivery())
    clock.advance(29 * DAY)
    assert await cache.get_delivery(equivalent, bot_id="bot-a") == delivery()

    clock.advance(2 * DAY)
    assert await cache.get_delivery(equivalent, bot_id="bot-a") == delivery()
    assert await cache.get_delivery(equivalent, bot_id="bot-b") is None

    clock.advance(30 * DAY + 1)
    assert await cache.get_delivery(equivalent, bot_id="bot-a") is None


@pytest.mark.asyncio
async def test_file_id_key_isolates_every_output_equivalence_field_and_version():
    """Catches reuse across a changed transform, request policy, or auth boundary."""
    clock = Clock()
    redis = FakeRedis(clock)
    cache = MediaCache(redis=redis, clock=clock, transform_version="pipeline-v7")
    base = request()
    await cache.put_delivery(base, bot_id="bot-a", delivery=delivery())

    variants = [
        request(kind=MediaKind.AUTO),
        request(quality=QualityPolicy(max_edge=720)),
        request(audio_format="m4a"),
        request(audio_language="en"),
        request(clip=ClipInterval(11, 20)),
        request(album_selection=(1, 3)),
        request(watermark_allowed=True),
        request(exact=False),
        request(auth_scope="instagram:user-2", caller_scope="chat:2"),
    ]

    for changed in variants:
        assert await cache.get_delivery(changed, bot_id="bot-a") is None

    upgraded = MediaCache(
        redis=redis,
        clock=clock,
        transform_version="pipeline-v8",
    )
    assert await upgraded.get_delivery(base, bot_id="bot-a") is None


@pytest.mark.asyncio
async def test_invalid_file_id_is_evicted_without_touching_other_record_types():
    """Catches retry loops that keep a rejected Telegram identifier cached."""
    cache = MediaCache()
    media_request = request()
    metadata = StableMediaMetadata(
        platform="youtube",
        media_id="abc123",
        kind=MediaKind.VIDEO,
        title="Example",
        duration_seconds=42.0,
        width=1080,
        height=1920,
        item_count=1,
        album_order=(),
        provider="ytdlp",
    )
    await cache.put_metadata(media_request, metadata)
    await cache.put_delivery(media_request, bot_id="bot-a", delivery=delivery())

    await cache.evict_delivery(media_request, bot_id="bot-a", item_index=0)

    assert await cache.get_delivery(media_request, bot_id="bot-a") is None
    assert await cache.get_metadata(media_request) == metadata


@pytest.mark.asyncio
async def test_stable_metadata_and_signed_urls_use_separate_records_and_ttls():
    """Catches signed URLs or temporary info paths leaking into durable metadata."""
    clock = Clock()
    redis = FakeRedis(clock)
    cache = MediaCache(
        redis=redis,
        clock=clock,
        metadata_ttl=DAY,
        signed_url_ttl=60,
    )
    media_request = request()
    metadata = StableMediaMetadata(
        platform="youtube",
        media_id="abc123",
        kind=MediaKind.VIDEO,
        title="Example",
        duration_seconds=42.0,
        width=1080,
        height=1920,
        item_count=1,
        album_order=(),
        provider="ytdlp",
    )
    signed = CachedSignedURL(
        url="https://cdn.example/video.mp4?signature=secret",
        provider="ytdlp",
        expires_at=clock() + 30,
    )

    await cache.put_metadata(media_request, metadata)
    await cache.put_signed_url(media_request, signed)

    metadata_key = cache.record_key("metadata", media_request)
    persisted = json.loads((await redis.get(metadata_key) or b"{}").decode())
    assert "signature=secret" not in json.dumps(persisted)
    assert "info_json" not in json.dumps(persisted)
    assert await cache.get_signed_url(media_request) == signed

    clock.advance(31)
    assert await cache.get_signed_url(media_request) is None
    assert await cache.get_metadata(media_request) == metadata


@pytest.mark.asyncio
async def test_redis_lease_release_and_renew_require_the_owner_token():
    """Catches stale workers deleting or extending a newer worker's lease."""
    clock = Clock()
    redis = FakeRedis(clock)
    leases = RedisLeaseManager(redis, prefix="media", clock=clock)

    owner = await leases.acquire("resolve:key", ttl=10)
    assert owner is not None
    assert await leases.acquire("resolve:key", ttl=10) is None
    assert await leases.release("resolve:key", "wrong-owner") is False
    assert await leases.renew("resolve:key", "wrong-owner", ttl=20) is False
    assert await leases.renew("resolve:key", owner, ttl=20) is True

    clock.advance(11)
    assert await leases.acquire("resolve:key", ttl=10) is None
    assert await leases.release("resolve:key", owner) is True
    assert await leases.acquire("resolve:key", ttl=10) not in {None, owner}
    assert redis.eval_calls == 4


@pytest.mark.asyncio
async def test_memory_lease_fallback_matches_owner_and_expiry_semantics():
    """Catches local fallback releasing another owner or ignoring lease expiry."""
    clock = Clock()
    cache = MediaCache(clock=clock)

    first = await cache.acquire_lease("materialize:key", ttl=5)
    assert first is not None
    assert await cache.release_lease("materialize:key", "wrong") is False
    assert await cache.acquire_lease("materialize:key", ttl=5) is None

    clock.advance(6)
    second = await cache.acquire_lease("materialize:key", ttl=5)
    assert second not in {None, first}
    assert await cache.release_lease("materialize:key", first) is False
    assert await cache.release_lease("materialize:key", second) is True
