from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InputFile
from telegram.error import BadRequest, NetworkError

from app.core.media_cache import CachedDelivery, MediaCache
from app.services.media.delivery import DeliveryAsset, TelegramDelivery
from app.services.media.models import (
    DeliveryStatus,
    DeliveryTarget,
    MediaCandidate,
    MediaItem,
    MediaKind,
    MediaRequest,
)
from app.services.media.transport import MaterializedItem


class Reservation:
    def __init__(self) -> None:
        self.released = False

    async def release(self) -> None:
        self.released = True

    def renew(self) -> None:
        pass


class CacheFailureDouble:
    """Cache boundary double with explicit, independently failing operations."""

    def __init__(
        self,
        *,
        cached: CachedDelivery | None = None,
        fail_get: bool = False,
        fail_evict: bool = False,
        fail_put_indexes: frozenset[int] = frozenset(),
    ) -> None:
        self.cached = cached
        self.fail_get = fail_get
        self.fail_evict = fail_evict
        self.fail_put_indexes = fail_put_indexes
        self.put_indexes: list[int] = []

    async def get_delivery(self, *args, **kwargs):
        del args, kwargs
        if self.fail_get:
            raise RuntimeError("cache-get-secret-file-id")
        return self.cached

    async def evict_delivery(self, *args, **kwargs):
        del args, kwargs
        if self.fail_evict:
            raise RuntimeError("cache-evict-secret-file-id")

    async def put_delivery(self, *args, **kwargs):
        del args
        index = kwargs["delivery"].item_index
        self.put_indexes.append(index)
        if index in self.fail_put_indexes:
            raise RuntimeError(f"cache-put-secret-file-id-{index}")


def telegram_message(kind: MediaKind, file_id: str, message_id: int = 41):
    media = SimpleNamespace(file_id=file_id, file_unique_id=f"unique-{file_id}")
    if kind is MediaKind.PHOTO:
        return SimpleNamespace(message_id=message_id, photo=[media])
    return SimpleNamespace(message_id=message_id, **{kind.value: media})


def asset(
    source: str | Path | io.BytesIO | None,
    *,
    kind: MediaKind = MediaKind.VIDEO,
    index: int = 0,
    size_bytes: int | None = None,
) -> DeliveryAsset:
    return DeliveryAsset(
        item=MediaItem(
            media_id=f"item-{index}",
            kind=kind,
            url=f"https://media.example/{index}",
        ),
        source=source,
        item_index=index,
        size_bytes=size_bytes,
    )


@pytest.mark.asyncio
async def test_materialized_local_path_returns_confirmed_video_receipt(tmp_path: Path):
    """Catches send success being reduced to a bool or releasing the media lease."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    path = media_dir / "clip.mp4"
    path.write_bytes(b"video")
    reservation = Reservation()
    item = MediaItem("video-1", MediaKind.VIDEO, "https://media.example/v")
    materialized = MaterializedItem(
        paths=(path,),
        size_bytes=5,
        candidate=MediaCandidate(
            candidate_id="candidate-1",
            url="https://media.example/v",
            kind=MediaKind.VIDEO,
            media_id="video-1",
            items=(item,),
        ),
        _reservation=reservation,  # type: ignore[arg-type]
    )
    bot = AsyncMock()
    bot.send_video.return_value = telegram_message(MediaKind.VIDEO, "video-file-id")
    delivery = TelegramDelivery(bot, media_dir=media_dir, local_mode=True)

    receipt = await delivery.deliver(materialized, DeliveryTarget("123"))

    assert receipt.status is DeliveryStatus.SUCCESS
    assert receipt.success
    assert receipt.items[0].telegram_type is MediaKind.VIDEO
    assert receipt.items[0].message_id == 41
    assert receipt.items[0].file_id == "video-file-id"
    assert receipt.items[0].item.media_id == "video-1"
    assert bot.send_video.await_args.kwargs["video"] == f"file://{path.resolve()}"
    assert reservation.released is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "method", "argument"),
    [
        (MediaKind.VIDEO, "send_video", "video"),
        (MediaKind.AUDIO, "send_audio", "audio"),
        (MediaKind.PHOTO, "send_photo", "photo"),
        (MediaKind.ANIMATION, "send_animation", "animation"),
        (MediaKind.DOCUMENT, "send_document", "document"),
    ],
)
async def test_each_telegram_media_type_has_a_typed_receipt(
    tmp_path: Path, kind: MediaKind, method: str, argument: str
):
    """Catches a media kind silently falling through to send_video."""
    path = tmp_path / f"item-{kind.value}"
    path.write_bytes(b"payload")
    bot = AsyncMock()
    getattr(bot, method).return_value = telegram_message(kind, f"{kind.value}-id")
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)

    receipt = await delivery.deliver(asset(path, kind=kind), DeliveryTarget("-100"))

    assert receipt.status is DeliveryStatus.SUCCESS
    assert receipt.items[0].telegram_type is kind
    assert receipt.items[0].file_id == f"{kind.value}-id"
    assert (
        getattr(bot, method).await_args.kwargs[argument] == f"file://{path.resolve()}"
    )


@pytest.mark.asyncio
async def test_network_disconnect_is_uncertain_and_is_never_retried(tmp_path: Path):
    """Catches automatic retries that can duplicate an already accepted upload."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    bot = AsyncMock()
    bot.send_video.side_effect = NetworkError("connection closed after upload")
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)

    receipt = await delivery.deliver(asset(path), DeliveryTarget("123"))

    assert receipt.status is DeliveryStatus.UNCERTAIN
    assert receipt.items[0].error_category == "unknown_outcome"
    assert receipt.retryable_items == ()
    assert bot.send_video.await_count == 1


@pytest.mark.asyncio
async def test_invalid_cached_file_id_is_evicted_then_uploaded_only_after_known_failure(
    tmp_path: Path,
):
    """Catches stale file IDs looping forever or unsafe fallback after unknown sends."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    request = MediaRequest.from_url("https://youtu.be/abc123", kind=MediaKind.VIDEO)
    cache = MediaCache()
    await cache.put_delivery(
        request,
        bot_id="bot-a",
        delivery=CachedDelivery("stale-id", MediaKind.VIDEO),
    )
    bot = AsyncMock()
    bot.send_video.side_effect = [
        BadRequest("Wrong file identifier/HTTP URL specified"),
        telegram_message(MediaKind.VIDEO, "fresh-id"),
    ]
    delivery = TelegramDelivery(
        bot,
        media_dir=tmp_path,
        local_mode=True,
        media_cache=cache,
        bot_id="bot-a",
    )

    receipt = await delivery.deliver(
        asset(path), DeliveryTarget("123"), request=request
    )

    assert receipt.status is DeliveryStatus.SUCCESS
    assert bot.send_video.await_count == 2
    assert bot.send_video.await_args_list[0].kwargs["video"] == "stale-id"
    assert (
        bot.send_video.await_args_list[1].kwargs["video"] == f"file://{path.resolve()}"
    )
    cached = await cache.get_delivery(request, bot_id="bot-a")
    assert cached is not None and cached.file_id == "fresh-id"


@pytest.mark.asyncio
async def test_uncertain_cached_file_id_send_is_not_evicted_or_uploaded(tmp_path: Path):
    """Catches losing a usable cache entry or duplicating an uncertain cached send."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    request = MediaRequest.from_url("https://youtu.be/abc123", kind=MediaKind.VIDEO)
    cache = MediaCache()
    original = CachedDelivery("cached-id", MediaKind.VIDEO)
    await cache.put_delivery(request, bot_id="bot-a", delivery=original)
    bot = AsyncMock()
    bot.send_video.side_effect = NetworkError("connection reset")
    delivery = TelegramDelivery(
        bot,
        media_dir=tmp_path,
        local_mode=True,
        media_cache=cache,
        bot_id="bot-a",
    )

    receipt = await delivery.deliver(
        asset(path), DeliveryTarget("123"), request=request
    )

    assert receipt.status is DeliveryStatus.UNCERTAIN
    assert bot.send_video.await_count == 1
    assert await cache.get_delivery(request, bot_id="bot-a") == original


@pytest.mark.asyncio
async def test_cache_get_failure_is_a_miss_and_uploads_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """Catches optional cache reads turning into delivery availability failures."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    request = MediaRequest.from_url("https://youtu.be/abc123", kind=MediaKind.VIDEO)
    cache = CacheFailureDouble(fail_get=True)
    bot = AsyncMock()
    bot.send_video.return_value = telegram_message(MediaKind.VIDEO, "fresh-id")
    delivery = TelegramDelivery(
        bot,
        media_dir=tmp_path,
        local_mode=True,
        media_cache=cache,  # type: ignore[arg-type]
        bot_id="bot-a",
    )

    receipt = await delivery.deliver(
        asset(path), DeliveryTarget("123"), request=request
    )

    assert receipt.status is DeliveryStatus.SUCCESS
    assert receipt.items[0].file_id == "fresh-id"
    assert bot.send_video.await_count == 1
    assert any(getattr(record, "operation", None) == "get" for record in caplog.records)
    assert "cache-get-secret-file-id" not in caplog.text


@pytest.mark.asyncio
async def test_cache_evict_failure_does_not_block_known_not_sent_upload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """Catches cache cleanup becoming a prerequisite for a safe upload fallback."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    request = MediaRequest.from_url("https://youtu.be/abc123", kind=MediaKind.VIDEO)
    cache = CacheFailureDouble(
        cached=CachedDelivery("stale-id", MediaKind.VIDEO), fail_evict=True
    )
    bot = AsyncMock()
    bot.send_video.side_effect = [
        BadRequest("Wrong file identifier/HTTP URL specified"),
        telegram_message(MediaKind.VIDEO, "fresh-id"),
    ]
    delivery = TelegramDelivery(
        bot,
        media_dir=tmp_path,
        local_mode=True,
        media_cache=cache,  # type: ignore[arg-type]
        bot_id="bot-a",
    )

    receipt = await delivery.deliver(
        asset(path), DeliveryTarget("123"), request=request
    )

    assert receipt.status is DeliveryStatus.SUCCESS
    assert receipt.items[0].file_id == "fresh-id"
    assert bot.send_video.await_count == 2
    assert any(
        getattr(record, "operation", None) == "evict" for record in caplog.records
    )
    assert "cache-evict-secret-file-id" not in caplog.text


@pytest.mark.asyncio
async def test_single_cache_put_failure_preserves_confirmed_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """Catches cache writes hiding a confirmed Telegram message receipt."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    request = MediaRequest.from_url("https://youtu.be/abc123", kind=MediaKind.VIDEO)
    cache = CacheFailureDouble(fail_put_indexes=frozenset({0}))
    bot = AsyncMock()
    bot.send_video.return_value = telegram_message(
        MediaKind.VIDEO, "confirmed-id", message_id=73
    )
    delivery = TelegramDelivery(
        bot,
        media_dir=tmp_path,
        local_mode=True,
        media_cache=cache,  # type: ignore[arg-type]
        bot_id="bot-a",
    )

    receipt = await delivery.deliver(
        asset(path), DeliveryTarget("123"), request=request
    )

    assert receipt.status is DeliveryStatus.SUCCESS
    assert receipt.items[0].message_id == 73
    assert receipt.items[0].file_id == "confirmed-id"
    assert bot.send_video.await_count == 1
    assert any(getattr(record, "operation", None) == "put" for record in caplog.records)
    assert "cache-put-secret-file-id-0" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_indexes", [frozenset({0}), frozenset({0, 1, 2})])
async def test_group_cache_put_failures_preserve_every_confirmed_item_in_order(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failed_indexes: frozenset[int],
):
    """Catches one cache write aborting receipt construction for a sent album."""
    request = MediaRequest.from_url("https://youtu.be/abc123", kind=MediaKind.ALBUM)
    cache = CacheFailureDouble(fail_put_indexes=failed_indexes)
    assets: list[DeliveryAsset] = []
    for index in range(3):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"photo")
        assets.append(asset(path, kind=MediaKind.PHOTO, index=index))
    bot = AsyncMock()
    bot.send_media_group.return_value = [
        telegram_message(MediaKind.PHOTO, f"confirmed-{index}", 100 + index)
        for index in range(3)
    ]
    delivery = TelegramDelivery(
        bot,
        media_dir=tmp_path,
        local_mode=True,
        media_cache=cache,  # type: ignore[arg-type]
        bot_id="bot-a",
    )

    receipt = await delivery.deliver(assets, DeliveryTarget("123"), request=request)

    assert receipt.status is DeliveryStatus.SUCCESS
    assert [item.item_index for item in receipt.items] == [0, 1, 2]
    assert [item.message_id for item in receipt.items] == [100, 101, 102]
    assert [item.file_id for item in receipt.items] == [
        "confirmed-0",
        "confirmed-1",
        "confirmed-2",
    ]
    assert cache.put_indexes == [0, 1, 2]
    assert bot.send_media_group.await_count == 1
    assert sum(
        getattr(record, "operation", None) == "put" for record in caplog.records
    ) == len(failed_indexes)
    assert "cache-put-secret-file-id" not in caplog.text


@pytest.mark.asyncio
async def test_album_chunks_are_two_to_ten_in_original_order(tmp_path: Path):
    """Catches truncation, singleton media groups, or reordered album items."""
    bot = AsyncMock()
    paths: list[Path] = []
    for index in range(11):
        path = tmp_path / f"{index:02}.jpg"
        path.write_bytes(b"photo")
        paths.append(path)
    bot.send_media_group.side_effect = [
        [telegram_message(MediaKind.PHOTO, f"id-{index}", index) for index in range(9)],
        [
            telegram_message(MediaKind.PHOTO, "id-9", 9),
            telegram_message(MediaKind.PHOTO, "id-10", 10),
        ],
    ]
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)

    receipt = await delivery.deliver(
        [
            asset(path, kind=MediaKind.PHOTO, index=index)
            for index, path in enumerate(paths)
        ],
        DeliveryTarget("123"),
    )

    assert receipt.status is DeliveryStatus.SUCCESS
    assert bot.send_media_group.await_count == 2
    calls = bot.send_media_group.await_args_list
    assert [len(call.kwargs["media"]) for call in calls] == [9, 2]
    sent = [entry.media for call in calls for entry in call.kwargs["media"]]
    assert sent == [f"file://{path.resolve()}" for path in paths]
    assert [item.item.media_id for item in receipt.items] == [
        f"item-{index}" for index in range(11)
    ]


@pytest.mark.asyncio
async def test_animation_in_album_is_sent_singly_without_changing_its_type(
    tmp_path: Path,
):
    """Catches passing unsupported InputMediaAnimation to sendMediaGroup."""
    photo = tmp_path / "photo.jpg"
    animation = tmp_path / "animation.mp4"
    photo.write_bytes(b"photo")
    animation.write_bytes(b"animation")
    bot = AsyncMock()
    bot.send_photo.return_value = telegram_message(MediaKind.PHOTO, "photo-id")
    bot.send_animation.return_value = telegram_message(
        MediaKind.ANIMATION, "animation-id"
    )
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)

    receipt = await delivery.deliver(
        [
            asset(photo, kind=MediaKind.PHOTO, index=0),
            asset(animation, kind=MediaKind.ANIMATION, index=1),
        ],
        DeliveryTarget("123"),
    )

    assert receipt.status is DeliveryStatus.SUCCESS
    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_awaited_once()
    bot.send_animation.assert_awaited_once()
    assert [item.telegram_type for item in receipt.items] == [
        MediaKind.PHOTO,
        MediaKind.ANIMATION,
    ]


@pytest.mark.asyncio
async def test_partial_album_retry_selects_only_known_failed_items(tmp_path: Path):
    """Catches retrying successful or uncertain album elements after a partial send."""
    bot = AsyncMock()
    paths: list[Path] = []
    assets: list[DeliveryAsset] = []
    for index in range(12):
        path = tmp_path / f"{index:02}.jpg"
        path.write_bytes(b"photo")
        paths.append(path)
        assets.append(asset(path, kind=MediaKind.PHOTO, index=index))
    bot.send_media_group.side_effect = [
        [
            telegram_message(MediaKind.PHOTO, f"id-{index}", index)
            for index in range(10)
        ],
        BadRequest("media group rejected before send"),
        [
            telegram_message(MediaKind.PHOTO, "retry-10", 110),
            telegram_message(MediaKind.PHOTO, "retry-11", 111),
        ],
    ]
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)

    first = await delivery.deliver(assets, DeliveryTarget("123"))

    assert first.status is DeliveryStatus.PARTIAL
    assert [item.item_index for item in first.retryable_items] == [10, 11]
    retried = await delivery.retry_failed(first, assets, DeliveryTarget("123"))
    assert retried.status is DeliveryStatus.SUCCESS
    assert [item.item_index for item in retried.items] == [10, 11]
    assert bot.send_media_group.await_count == 3


@pytest.mark.asyncio
async def test_local_path_is_confined_and_external_path_uses_streaming_multipart(
    tmp_path: Path,
):
    """Catches file:// exposure outside the shared Local Bot API media directory."""
    media_dir = tmp_path / "shared"
    media_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    bot = AsyncMock()
    bot.send_video.return_value = telegram_message(MediaKind.VIDEO, "video-id")
    delivery = TelegramDelivery(bot, media_dir=media_dir, local_mode=True)

    receipt = await delivery.deliver(asset(outside), DeliveryTarget("123"))

    assert receipt.success
    uploaded = bot.send_video.await_args.kwargs["video"]
    assert isinstance(uploaded, InputFile)
    stream = uploaded.input_file_content
    assert not isinstance(stream, bytes)
    assert Path(stream.name) == outside
    assert stream.closed


@pytest.mark.asyncio
async def test_cloud_limit_uses_decimal_boundary_before_open_or_send():
    """Catches MiB arithmetic or reading an oversized body before rejection."""
    bot = AsyncMock()
    delivery = TelegramDelivery(
        bot,
        media_dir=".",
        local_mode=False,
        max_media_bytes=2_000_000_000,
        cloud_max_bytes=2_000_000_000,
    )
    at_limit = io.BytesIO(b"x")
    bot.send_document.return_value = telegram_message(MediaKind.DOCUMENT, "doc-id")

    accepted = await delivery.deliver(
        asset(
            at_limit,
            kind=MediaKind.DOCUMENT,
            size_bytes=2_000_000_000,
        ),
        DeliveryTarget("123"),
    )
    rejected = await delivery.deliver(
        asset(
            io.BytesIO(b"must-not-be-read"),
            kind=MediaKind.DOCUMENT,
            size_bytes=2_000_000_001,
        ),
        DeliveryTarget("123"),
    )

    assert accepted.status is DeliveryStatus.SUCCESS
    assert rejected.status is DeliveryStatus.FAILED
    assert rejected.items[0].error_category == "file_too_large"
    assert bot.send_document.await_count == 1
