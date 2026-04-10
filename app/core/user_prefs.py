"""Per-user download preferences stored in Redis (or in-memory fallback).

Schema (msgspec / dict):
    {
        "default_format":  "video" | "audio" | None,
        "default_quality": 1080 | 720 | 480 | 360 | None  (None == "best")
    }

Key pattern : prefs:{user_id}
TTL         : USER_PREFS_TTL_DAYS * 86400  (default 90 days)

Usage:
    prefs = await get_prefs(user_id)
    await set_prefs(user_id, default_format="audio")
    await clear_prefs(user_id)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.core.user_prefs")

# Valid option sets (used for validation in commands.py)
VALID_FORMATS = {"video", "audio"}
VALID_QUALITIES: dict[str, int | None] = {
    "best": None,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "360": 360,
}

_TTL_SECONDS = 90 * 86400  # 90 days


def _key(user_id: int) -> str:
    return f"prefs:{user_id}"


async def get_prefs(user_id: int) -> dict[str, Any]:
    """Return the preference dict for *user_id*.  Always returns a dict (never None)."""
    from app.core import state  # late import to avoid circular at module load

    raw = await state.prefs_cache.get(_key(user_id))
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    # Unexpected type — return empty to be safe
    logger.warning("Unexpected prefs type for user %s: %s", user_id, type(raw))
    return {}


async def set_prefs(user_id: int, **kwargs: Any) -> None:
    """Merge *kwargs* into the stored preference dict and persist."""
    from app.core import state

    existing = await get_prefs(user_id)
    existing.update(kwargs)
    await state.prefs_cache.set(_key(user_id), existing, ttl=_TTL_SECONDS)


async def clear_prefs(user_id: int) -> None:
    """Delete all stored preferences for *user_id*."""
    from app.core import state

    await state.prefs_cache.delete(_key(user_id))
