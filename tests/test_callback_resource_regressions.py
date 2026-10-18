"""Exact callback source identity and ownership across interrupted exports."""

import asyncio
import hashlib
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cachetools import TTLCache
from telegram import Message

from app.bot import callbacks, group_logic
from app.core import state
from app.core.job_store import DeliveryOutcome, JobStore, delivery_job_context
from app.core.models import DownloadContext
from app.core.storage.memory import MemoryStorage


def gif_callback(token, bot):
    message = MagicMock(spec=Message)
    message.chat_id = 42
    message.message_id = 7
    message.animation = SimpleNamespace(file_id="source-animation")
    message.video = None
    message.reply_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    query = SimpleNamespace(
        data=f"m2|giffile|{token}", message=message, answer=AsyncMock()
    )
    return SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)


@pytest.fixture
def export_state(monkeypatch, tmp_path):
    monkeypatch.setattr(callbacks, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(state, "link_cache", MemoryStorage(100, 3600))
    monkeypatch.setattr(state, "gifdoc_cache", MemoryStorage(100, 3600))
    monkeypatch.setattr(state, "file_cache", {})
    monkeypatch.setattr(state, "processing_gifs", set())
    monkeypatch.setattr(state, "sent_media_sources", TTLCache(100, 60))
    monkeypatch.setattr(state, "gif_file_sem", asyncio.Semaphore(1))


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dictionary", [False, True])
async def test_gif_export_never_reuses_url_only_cache_for_different_clip(
    export_state, tmp_path, as_dictionary
):
    url = "https://youtu.be/abcdefghijk"
    legacy_key = "gifdoc_" + hashlib.md5(url.encode()).hexdigest()
    await state.gifdoc_cache.set(legacy_key, "wrong-unclipped-gif")
    payload = DownloadContext(page_url=url, format_id="137+140", section="*1-2")
    await state.link_cache.set("clipped", asdict(payload) if as_dictionary else payload)
    source = tmp_path / "clipped.gif"
    source.write_bytes(b"GIF89a-clipped-source")
    state.file_cache["clipped"] = str(source)
    delivered = []

    async def send_document(**kwargs):
        document = kwargs["document"]
        delivered.append(
            document if isinstance(document, str) else document.input_file_content
        )
        return SimpleNamespace(
            message_id=8, document=SimpleNamespace(file_id="correct-clipped-gif")
        )

    bot = SimpleNamespace(send_document=send_document)
    update, context = gif_callback("clipped", bot)
    update.callback_query.message.animation = None
    await callbacks.on_save_as_gif_file(update, context)
    await callbacks.on_save_as_gif_file(update, context)

    assert delivered == [b"GIF89a-clipped-source", "correct-clipped-gif"]
    assert source.read_bytes() == b"GIF89a-clipped-source"


@pytest.mark.asyncio
async def test_same_token_gif_messages_use_their_own_telegram_source(
    export_state, tmp_path, monkeypatch
):
    unrelated = tmp_path / "token-current.gif"
    unrelated.write_bytes(b"wrong token-wide source")
    state.file_cache["shared"] = str(unrelated)
    delivered = []

    async def get_file(file_id):
        async def download_to_drive(path):
            Path(path).write_bytes(file_id.encode())

        return SimpleNamespace(download_to_drive=download_to_drive)

    async def convert(source):
        output = Path(source).with_suffix(".gif")
        output.write_bytes(b"GIF89a-" + Path(source).read_bytes())
        return str(output)

    async def send_document(**kwargs):
        document = kwargs["document"]
        delivered.append(
            document if isinstance(document, str) else document.input_file_content
        )
        return SimpleNamespace(
            message_id=8,
            document=SimpleNamespace(file_id=f"cached-{len(delivered)}"),
        )

    monkeypatch.setattr(
        "app.services.converter.MediaConverter.convert_to_native_gif", convert
    )
    bot = SimpleNamespace(get_file=get_file, send_document=send_document)
    first, context = gif_callback("shared", bot)
    second, _ = gif_callback("shared", bot)
    first.callback_query.message.animation = SimpleNamespace(
        file_unique_id="unique-first", file_id="first-source"
    )
    second.callback_query.message.animation = SimpleNamespace(
        file_unique_id="unique-second", file_id="second-source"
    )
    await callbacks.on_save_as_gif_file(first, context)
    await callbacks.on_save_as_gif_file(second, context)
    await callbacks.on_save_as_gif_file(first, context)

    assert delivered == [b"GIF89a-first-source", b"GIF89a-second-source", "cached-1"]
    assert list(tmp_path.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"wrong token-wide source"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", [DeliveryOutcome.SUCCESS, DeliveryOutcome.UNCERTAIN]
)
@pytest.mark.parametrize("cache_expired", [False, True])
async def test_gif_source_cache_migration_preserves_legacy_durable_receipts(
    export_state, tmp_path, outcome, cache_expired
):
    store = JobStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 1})
    job = await store.claim_next("owner")
    assert job is not None
    url = "https://youtu.be/abcdefghijk"
    await state.link_cache.set("source", DownloadContext(page_url=url))
    if cache_expired:
        await state.link_cache.delete("source")
    legacy_key = "gifdoc_" + hashlib.md5(url.encode()).hexdigest()
    await store.record_delivery(
        job.id, outcome, item_key=f"gif-document:42:{legacy_key}"
    )
    sent = []

    async def forbid_delivery(*args, **kwargs):
        sent.append((args, kwargs))
        pytest.fail("A historical successful or uncertain send cannot be replayed")

    bot = SimpleNamespace(send_document=forbid_delivery, get_file=forbid_delivery)
    update, context = gif_callback("source", bot)
    with delivery_job_context(store, job.id, owner_id="owner"):
        await callbacks.on_save_as_gif_file(update, context)

    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_gif_export_allows_failed_receipts_and_ordinary_polling(
    export_state, tmp_path, durable
):
    source = tmp_path / "retryable.gif"
    source.write_bytes(b"GIF89a-retryable")
    state.file_cache["source"] = str(source)
    sent = []

    async def send_document(**kwargs):
        sent.append(kwargs["document"].input_file_content)
        return SimpleNamespace(message_id=8, document=None)

    update, context = gif_callback(
        "source", SimpleNamespace(send_document=send_document)
    )
    update.callback_query.message.animation = None
    if durable:
        store = JobStore(tmp_path / "jobs.sqlite3")
        await store.accept_update({"update_id": 1})
        job = await store.claim_next("owner")
        assert job is not None
        await store.record_delivery(
            job.id, DeliveryOutcome.FAILED, item_key="gif-document:42:old-url-key"
        )
        with delivery_job_context(store, job.id, owner_id="owner"):
            await callbacks.on_save_as_gif_file(update, context)
    else:
        await callbacks.on_save_as_gif_file(update, context)

    assert sent == [b"GIF89a-retryable"]
    assert source.read_bytes() == b"GIF89a-retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel", "oversize"])
async def test_telegram_gif_source_is_removed_after_early_exit(
    export_state, tmp_path, failure
):
    async def download_to_drive(path):
        with Path(path).open("wb") as output:  # noqa: ASYNC230 - deterministic local fake
            output.write(b"partial source")
            if failure == "oversize":
                output.truncate(51 * 1024 * 1024)
        if failure == "error":
            raise OSError("interrupted source download")
        if failure == "cancel":
            raise asyncio.CancelledError

    bot = SimpleNamespace(
        get_file=AsyncMock(
            return_value=SimpleNamespace(download_to_drive=download_to_drive)
        )
    )
    update, context = gif_callback("early-exit", bot)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await callbacks.on_save_as_gif_file(update, context)
    else:
        await callbacks.on_save_as_gif_file(update, context)

    assert list(tmp_path.iterdir()) == []
    assert state.processing_gifs == set()


@pytest.mark.asyncio
async def test_gif_debounce_owns_source_before_download_and_preserves_other_files(
    export_state, tmp_path
):
    existing = tmp_path / "gif_src_shared.mp4"
    existing.write_bytes(b"another request owns this file")
    started = asyncio.Event()
    release = asyncio.Event()
    source_paths = []

    async def download_to_drive(path):
        source_paths.append(Path(path))
        Path(path).write_bytes(b"partial owned download")
        started.set()
        await release.wait()
        raise OSError("source unavailable")

    bot = SimpleNamespace(
        get_file=AsyncMock(
            return_value=SimpleNamespace(download_to_drive=download_to_drive)
        )
    )
    update, context = gif_callback("shared", bot)
    first = asyncio.create_task(callbacks.on_save_as_gif_file(update, context))
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(callbacks.on_save_as_gif_file(update, context))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    assert len(source_paths) == 1
    assert source_paths[0] != existing
    assert existing.read_bytes() == b"another request owns this file"
    assert list(tmp_path.iterdir()) == [existing]
    assert state.processing_gifs == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_animation_export_cleans_output_when_delivery_escapes(
    export_state, tmp_path, monkeypatch, cancelled
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"owned by source cache")
    generated = tmp_path / "converted.mp4"
    state.file_cache["source"] = str(source)

    async def convert(path):
        generated.write_bytes(b"mute:" + Path(path).read_bytes())
        return str(generated)

    async def interrupted_send(bot, chat_id, path, **kwargs):
        assert Path(path).read_bytes() == b"mute:owned by source cache"
        if cancelled:
            raise asyncio.CancelledError
        raise OSError("delivery receipt adapter failed")

    monkeypatch.setattr(callbacks.MediaSender, "convert_to_gif_ffmpeg", convert)
    monkeypatch.setattr(callbacks.MediaSender, "send_file", interrupted_send)
    update, context = gif_callback("source", SimpleNamespace())
    query = update.callback_query
    query.data = "m2|gif|source"
    query.message.animation = None
    query.edit_message_reply_markup = AsyncMock()
    with pytest.raises(asyncio.CancelledError if cancelled else OSError):
        await callbacks.on_convert_to_gif(update, context)

    assert not generated.exists()
    assert source.read_bytes() == b"owned by source cache"
    assert state.processing_gifs == set()


@pytest.mark.asyncio
async def test_animation_exports_use_each_messages_actual_source(
    export_state, tmp_path, monkeypatch
):
    stale = tmp_path / "latest-token-output.mp4"
    stale.write_bytes(b"stale token-wide source")
    state.file_cache["shared"] = str(stale)
    delivered = []

    async def get_file(file_id):
        async def download_to_drive(path):
            Path(path).write_bytes(file_id.encode())

        return SimpleNamespace(download_to_drive=download_to_drive)

    async def convert(path):
        generated = Path(path).with_suffix(".gif.mp4")
        generated.write_bytes(b"mute:" + Path(path).read_bytes())
        return str(generated)

    async def send(bot, chat_id, path, **kwargs):
        delivered.append(Path(path).read_bytes())
        return True

    monkeypatch.setattr(callbacks.MediaSender, "convert_to_gif_ffmpeg", convert)
    monkeypatch.setattr(callbacks.MediaSender, "send_file", send)
    bot = SimpleNamespace(get_file=get_file)
    for identity in ("first-source", "second-source"):
        update, context = gif_callback("shared", bot)
        query = update.callback_query
        query.data = "m2|gif|shared"
        query.edit_message_reply_markup = AsyncMock()
        query.message.animation = None
        query.message.video = SimpleNamespace(file_id=identity, file_unique_id=identity)
        await callbacks.on_convert_to_gif(update, context)

    assert delivered == [b"mute:first-source", b"mute:second-source"]
    assert list(tmp_path.iterdir()) == [stale]
    assert stale.read_bytes() == b"stale token-wide source"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "data"),
    [
        (callbacks.on_slideshow, "m2|apislide|token|unexpected"),
        (callbacks.on_slideshow, "slideshow|unexpected"),
        (group_logic.on_group_slideshow, "m2|apigrpslide|token|unexpected"),
        (group_logic.on_group_slideshow, "grpslide|token|unexpected"),
    ],
)
async def test_invalid_slideshow_mode_never_starts_media_work(
    monkeypatch, handler, data
):
    work = []

    class Queue:
        async def enqueue(self, update_ui):
            work.append("queued")
            return False

    cache = MemoryStorage(10, 3600)
    await cache.set(
        "token",
        DownloadContext(
            page_url="https://www.tiktok.com/@user/video/123",
            api_source="pipeline",
            api_json={"media_id": "123"},
        ),
    )
    monkeypatch.setattr(state, "link_cache", cache)
    monkeypatch.setattr(state, "download_queue", Queue())
    monkeypatch.setattr(
        state,
        "limiter",
        SimpleNamespace(
            allow_user=AsyncMock(return_value=True),
            allow_chat=AsyncMock(return_value=True),
        ),
    )
    message = MagicMock(spec=Message)
    message.chat_id = 42
    query = SimpleNamespace(
        data=data,
        message=message,
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        user_data={"page_url": "https://www.tiktok.com/@user/video/123"}
    )
    await handler(update, context)

    assert work == []
