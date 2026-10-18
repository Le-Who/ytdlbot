"""Successful sends retain a non-owning identity binding for cloud GIF exports."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cachetools import TTLCache
from telegram import Video
from telegram.error import BadRequest, NetworkError
from test_callback_resource_regressions import gif_callback

from app.bot import callbacks
from app.core import state
from app.core.media_cache import CachedDelivery, MediaCache
from app.core.resource_budget import DiskBudget
from app.core.storage.memory import MemoryStorage
from app.services.media.delivery import DeliveryAsset, TelegramDelivery
from app.services.media.models import (
    DeliveryTarget,
    MediaCandidate,
    MediaItem,
    MediaKind,
)
from app.services.media.pipeline import build_media_request
from app.services.media.transport import MaterializedItem


class SentBot:
    def __init__(self):
        self.sequence = 0
        self.get_attempts = []
        self.outputs = []
        self.error = None
        self.download_bytes = None
        self.id = 42

    async def send_video(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.sequence += 1
        media = Video(
            file_id=f"sent-{self.sequence}",
            file_unique_id=f"unique-{self.sequence}",
            width=1,
            height=1,
            duration=1,
        )
        return SimpleNamespace(message_id=self.sequence, video=media)

    async def send_animation(self, **kwargs):
        self.outputs.append("animation")
        return SimpleNamespace(message_id=99, animation=None)

    async def send_media_group(self, **kwargs):
        return [await self.send_video() for _ in kwargs["media"]]

    async def send_document(self, **kwargs):
        self.outputs.append("document")
        return SimpleNamespace(message_id=99, document=None)

    async def get_file(self, file_id):
        self.get_attempts.append(file_id)
        if self.download_bytes is None:
            raise BadRequest("File is too big")

        async def download_to_drive(path):
            Path(path).write_bytes(self.download_bytes)

        return SimpleNamespace(download_to_drive=download_to_drive)


@pytest.fixture
def binding_env(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "sent_media_sources", TTLCache(100, 60), raising=False)
    monkeypatch.setattr(state, "file_cache", {})
    monkeypatch.setattr(state, "gifdoc_cache", MemoryStorage(100, 60))
    monkeypatch.setattr(state, "processing_gifs", set())
    monkeypatch.setattr(callbacks, "TEMP_DIR", str(tmp_path))
    conversions = []

    async def convert(path):
        path = Path(path)
        conversions.append((path, path.stat().st_size, path.read_bytes()[:4]))
        output = tmp_path / f"converted-{len(conversions)}.gif"
        output.write_bytes(b"GIF89a")
        return str(output)

    monkeypatch.setattr(callbacks.MediaSender, "convert_to_gif_ffmpeg", convert)
    monkeypatch.setattr(
        "app.services.converter.MediaConverter.convert_to_native_gif", convert
    )
    return SimpleNamespace(bot=SentBot(), conversions=conversions)


async def send_source(
    env, path, *, local_mode=False, request=None, cache=None, materialized=False
):
    delivery = TelegramDelivery(
        env.bot,
        media_dir=path.parent,
        local_mode=local_mode,
        media_cache=cache,
        bot_id="42",
    )
    item = MediaItem("source", MediaKind.VIDEO, "")
    media = DeliveryAsset(item, path)
    if materialized:
        reservation = await DiskBudget(path.parent).reserve(path.stat().st_size)
        reservation.bind(path)
        media = MaterializedItem(
            (path,),
            path.stat().st_size,
            MediaCandidate("source", "https://cdn.example.com/video", items=(item,)),
            reservation,
        )
    receipt = await delivery.deliver(
        media,
        DeliveryTarget("42"),
        request=request,
    )
    if materialized:
        await media.release()
    return receipt


async def export(env, mode, *, identity=1, token="shared"):
    update, context = gif_callback(token, env.bot)
    query = update.callback_query
    query.message.animation = None
    query.message.video = Video(
        file_id=f"sent-{identity}",
        file_unique_id=f"unique-{identity}",
        width=1,
        height=1,
        duration=1,
    )
    query.edit_message_reply_markup = AsyncMock()
    if mode == "animation":
        query.data = f"m2|gif|{token}"
        await callbacks.on_convert_to_gif(update, context)
    else:
        await callbacks.on_save_as_gif_file(update, context)


def sparse_source(path, header=b"clip", size=25_000_000):
    with path.open("wb") as output:
        output.write(header)
        output.truncate(size)


@pytest.mark.parametrize("mode", ["animation", "native"])
@pytest.mark.parametrize("local_mode", [False, True], ids=["cloud", "local-api"])
@pytest.mark.parametrize("materialized", [False, True], ids=["legacy", "pipeline"])
async def test_confirmed_local_source_exports_above_cloud_getfile_limit(
    binding_env, tmp_path, mode, local_mode, materialized
):
    env = binding_env
    source = tmp_path / "retained-source.mp4"
    sparse_source(source)
    state.file_cache["shared"] = str(source)
    assert (
        await send_source(env, source, local_mode=local_mode, materialized=materialized)
    ).success

    await export(env, mode)

    assert env.conversions == [(source, 25_000_000, b"clip")]
    assert env.bot.get_attempts == []
    assert env.bot.outputs == ["animation" if mode == "animation" else "document"]
    assert source.stat().st_size == 25_000_000


@pytest.mark.parametrize("mode", ["animation", "native"])
async def test_binding_selects_exact_successful_source_despite_shared_token(
    binding_env, tmp_path, mode
):
    env = binding_env
    first, second = tmp_path / "first.mp4", tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    assert (await send_source(env, first)).success
    assert (await send_source(env, second)).success
    state.file_cache["shared"] = str(second)

    await export(env, mode, identity=1)
    await export(env, mode, identity=2)

    assert env.conversions == [(first, 5, b"firs"), (second, 6, b"seco")]
    assert env.bot.get_attempts == []
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


async def test_successful_album_binds_each_returned_source_identity(
    binding_env, tmp_path
):
    env = binding_env
    first, second = tmp_path / "album-first.mp4", tmp_path / "album-second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    delivery = TelegramDelivery(env.bot, local_mode=False)
    receipt = await delivery.deliver(
        [
            DeliveryAsset(MediaItem("first", MediaKind.VIDEO, ""), first),
            DeliveryAsset(MediaItem("second", MediaKind.VIDEO, ""), second),
        ],
        DeliveryTarget("42"),
    )
    assert receipt.success

    await export(env, "native", identity=2)
    await export(env, "animation", identity=1)

    assert env.conversions == [(second, 6, b"seco"), (first, 5, b"firs")]
    assert env.bot.get_attempts == []


@pytest.mark.parametrize("mode", ["animation", "native"])
@pytest.mark.parametrize("changed", ["removed", "overwritten"])
async def test_stale_binding_recovers_from_telegram_without_unlinking_owner_file(
    binding_env, tmp_path, mode, changed
):
    env = binding_env
    source = tmp_path / "old.mp4"
    source.write_bytes(b"old-source")
    assert (await send_source(env, source)).success
    if changed == "removed":
        source.unlink()
    else:
        source.write_bytes(b"another owner's replacement")
    env.bot.download_bytes = b"telegram-source"

    await export(env, mode)

    assert env.bot.get_attempts == ["sent-1"]
    assert [(size, header) for _, size, header in env.conversions] == [(15, b"tele")]
    if changed == "overwritten":
        assert source.read_bytes() == b"another owner's replacement"
    else:
        assert not source.exists()
    assert all(not path.exists() for path, _, _ in env.conversions)


@pytest.mark.parametrize("mode", ["animation", "native"])
async def test_expired_metadata_leaves_owner_file_and_uses_normal_expiry_path(
    binding_env, tmp_path, monkeypatch, mode
):
    env = binding_env
    clock = [0.0]
    monkeypatch.setattr(
        state, "sent_media_sources", TTLCache(10, 1, timer=lambda: clock[0])
    )
    source = tmp_path / "owner-retained.mp4"
    source.write_bytes(b"still owned")
    assert (await send_source(env, source)).success
    clock[0] = 2.0

    await export(env, mode)

    assert env.bot.get_attempts == ["sent-1"]
    assert env.conversions == []
    assert env.bot.outputs == []
    assert source.read_bytes() == b"still owned"


@pytest.mark.parametrize("failure", [BadRequest("rejected"), NetworkError("unknown")])
async def test_failed_or_uncertain_send_never_binds_unconfirmed_media(
    binding_env, tmp_path, failure
):
    env = binding_env
    source = tmp_path / "unconfirmed.mp4"
    source.write_bytes(b"not-confirmed")
    env.bot.error = failure
    receipt = await send_source(env, source)
    assert not receipt.success
    state.file_cache["shared"] = str(source)

    await export(env, "animation")

    assert env.conversions == []
    assert env.bot.get_attempts == ["sent-1"]
    assert source.read_bytes() == b"not-confirmed"


@pytest.mark.parametrize("cached", [False, True], ids=["url", "file-id"])
async def test_remote_send_does_not_bind_unuploaded_local_asset(
    binding_env, tmp_path, cached
):
    env = binding_env
    source = tmp_path / "unuploaded.mp4"
    source.write_bytes(b"unuploaded")
    if cached:
        request = build_media_request("https://youtu.be/abcdefghijk", kind="video")
        cache = MediaCache()
        await cache.put_delivery(
            request,
            bot_id="42",
            delivery=CachedDelivery("cached-file-id", MediaKind.VIDEO),
        )
        receipt = await send_source(env, source, request=request, cache=cache)
    else:
        receipt = await TelegramDelivery(env.bot, local_mode=False).deliver(
            DeliveryAsset(
                MediaItem("source", MediaKind.VIDEO, ""),
                "https://cdn.example.com/source.mp4",
            ),
            DeliveryTarget("42"),
        )
    assert receipt.success
    state.file_cache["shared"] = str(source)

    await export(env, "native")

    assert env.conversions == []
    assert env.bot.get_attempts == ["sent-1"]
    assert source.read_bytes() == b"unuploaded"
