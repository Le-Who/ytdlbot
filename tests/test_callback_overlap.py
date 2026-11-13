"""A callback request owns its operation before its first asynchronous step."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from app.bot import callbacks, group_logic
from app.core import state
from app.core.models import DownloadContext
from app.core.storage.memory import MemoryStorage
from app.services.media.models import DeliveryReceipt, DeliveryStatus


@pytest.mark.parametrize("handler_name", ["send", "private", "group"])
@pytest.mark.parametrize("first_outcome", ["success", "failure", "cancel"])
async def test_overlapping_callbacks_share_one_active_operation(
    monkeypatch, handler_name, first_outcome
):
    cache = MemoryStorage(maxsize=20, ttl=60)
    monkeypatch.setattr(state, "link_cache", cache)
    await cache.set(
        "token",
        DownloadContext(
            page_url="https://www.tiktok.com/@test/photo/123",
            chat_id=9,
            api_source="pipeline",
            api_json={"media_id": "123"},
        ),
    )
    monkeypatch.setattr(
        state,
        "limiter",
        SimpleNamespace(
            allow_user=AsyncMock(return_value=True),
            allow_chat=AsyncMock(return_value=True),
        ),
    )
    monkeypatch.setattr(
        state,
        "download_queue",
        SimpleNamespace(
            enqueue=AsyncMock(return_value=True),
            release=MagicMock(),
        ),
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        entered.set()
        await finish.wait()
        if first_outcome == "failure":
            raise OSError("operation failed")
        if handler_name == "send":
            return True
        return DeliveryReceipt(args[1], (), DeliveryStatus.SUCCESS)

    deliver = AsyncMock(side_effect=run)
    monkeypatch.setattr(callbacks.DownloadOrchestrator, "process_download", deliver)
    monkeypatch.setattr(state, "media_pipeline", SimpleNamespace(deliver=deliver))
    message = MagicMock(spec=Message)
    message.chat_id = 9
    message.message_id = 100
    message.video = message.animation = message.document = None
    message.photo = ()
    data = {
        "send": "m2|send|token",
        "private": "m2|apislide|token|photo",
        "group": "m2|apigrpslide|token|photo",
    }[handler_name]

    def update():
        return SimpleNamespace(
            callback_query=SimpleNamespace(
                data=data,
                message=message,
                from_user=SimpleNamespace(id=7),
                answer=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
            )
        )

    handler = {
        "send": callbacks.on_send,
        "private": callbacks.on_slideshow,
        "group": group_logic.on_group_slideshow,
    }[handler_name]
    context = SimpleNamespace(user_data={}, bot=AsyncMock())
    first = asyncio.create_task(handler(update(), context))
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(handler(update(), context))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert deliver.await_count == 1, (
            "a second update must not repeat active media work"
        )
    finally:
        if first_outcome == "cancel":
            first.cancel()
        finish.set()
        await asyncio.gather(first, second, return_exceptions=True)

    # Releasing an active claim never permanently consumes an existing button.
    if first_outcome == "failure":
        with pytest.raises(OSError):
            await handler(update(), context)
    else:
        await handler(update(), context)
    assert deliver.await_count == 2
