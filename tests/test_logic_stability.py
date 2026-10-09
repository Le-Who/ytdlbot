"""Concurrent requests exercise the actual private-message parsing path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.messages import on_message
from app.core import state
from app.core.storage.memory import MemoryStorage
from app.services.ytdlp.models import ExtractionResult


async def test_parsing_deduplication(monkeypatch):
    """Removing handler deduplication must start two independent extractions."""
    started, finish, second_cache_miss = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    calls = []
    result = ExtractionResult(
        title="Shared title",
        formats=[],
        special_format=None,
        duration_str="1:00",
        is_slideshow=False,
        info_json_path=None,
        thumbnail_url=None,
    )

    class Cache(MemoryStorage):
        reads = 0

        async def get(self, key, type_hint=None):
            value = await super().get(key, type_hint)
            self.reads += 1
            if self.reads == 2:
                second_cache_miss.set()
            return value

    async def extract(url):
        calls.append(url)
        started.set()
        await finish.wait()
        return result

    url = "https://youtube.com/watch?v=unique_123"
    monkeypatch.setattr(state, "media_pipeline", None)
    monkeypatch.setattr(state, "info_cache", Cache(maxsize=10, ttl=60))
    monkeypatch.setattr(state, "prefs_cache", MemoryStorage(maxsize=10, ttl=60))
    monkeypatch.setattr(state, "cancel_cache", MemoryStorage(maxsize=10, ttl=60))
    monkeypatch.setattr(state, "inflight_parsing", {})
    monkeypatch.setattr(state, "parsing_sem", asyncio.Semaphore(2))
    monkeypatch.setattr(state, "ytdlp", SimpleNamespace(list_formats=extract))
    monkeypatch.setattr(
        state,
        "limiter",
        SimpleNamespace(
            allow_user=AsyncMock(return_value=True),
            allow_chat=AsyncMock(return_value=True),
        ),
    )
    statuses, contexts, updates = [], [], []
    for user_id in (11, 22):
        status = SimpleNamespace(edit_text=AsyncMock())
        statuses.append(status)
        contexts.append(
            SimpleNamespace(
                user_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock())
            )
        )
        updates.append(
            SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(id=user_id),
                message=SimpleNamespace(
                    text=url,
                    caption=None,
                    reply_to_message=None,
                    reply_text=AsyncMock(return_value=status),
                ),
            )
        )
    tasks = []
    try:
        tasks.append(asyncio.create_task(on_message(updates[0], contexts[0])))
        await asyncio.wait_for(started.wait(), timeout=1)
        tasks.append(asyncio.create_task(on_message(updates[1], contexts[1])))
        await asyncio.wait_for(second_cache_miss.wait(), timeout=1)
        assert calls == [url]
        assert not any(task.done() for task in tasks)
        finish.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    finally:
        finish.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert calls == [url]
    assert state.inflight_parsing == {}
    assert [context.user_data["title"] for context in contexts] == [
        "Shared title",
        "Shared title",
    ]
    for status in statuses:
        assert "Shared title" in status.edit_text.await_args.args[0]
        assert status.edit_text.await_args.kwargs["parse_mode"] == "HTML"
