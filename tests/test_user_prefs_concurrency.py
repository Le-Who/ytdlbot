"""Concurrent Telegram settings commands preserve each field and reset order."""

import asyncio
from copy import deepcopy

from app.core import state, user_prefs
from app.core.storage.memory import MemoryStorage


class SnapshotStorage(MemoryStorage):
    async def get(self, key, type_hint=None):
        value = deepcopy(await super().get(key))
        await asyncio.sleep(0)
        return value


async def test_concurrent_format_and_quality_commands_preserve_both(monkeypatch):
    cache = SnapshotStorage(maxsize=10, ttl=60)
    monkeypatch.setattr(state, "prefs_cache", cache)
    await asyncio.gather(
        user_prefs.set_prefs(7, default_format="audio"),
        user_prefs.set_prefs(7, default_quality=720),
    )
    assert await user_prefs.get_prefs(7) == {
        "default_format": "audio",
        "default_quality": 720,
    }


async def test_later_reset_cannot_be_undone_by_an_earlier_update(monkeypatch):
    cache = SnapshotStorage(maxsize=10, ttl=60)
    monkeypatch.setattr(state, "prefs_cache", cache)
    await cache.set("prefs:7", {"default_format": "audio"})
    await asyncio.gather(
        user_prefs.set_prefs(7, default_quality=720),
        user_prefs.clear_prefs(7),
    )
    assert await user_prefs.get_prefs(7) == {}


async def test_reading_preferences_does_not_grant_write_access(monkeypatch):
    cache = MemoryStorage(maxsize=10, ttl=60)
    monkeypatch.setattr(state, "prefs_cache", cache)
    await user_prefs.set_prefs(7, default_format="audio")
    read = await user_prefs.get_prefs(7)
    read["default_format"] = "video"
    assert await user_prefs.get_prefs(7) == {"default_format": "audio"}
