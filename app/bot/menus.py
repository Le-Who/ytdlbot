"""Request snapshots for menus that can outlive the user's next message."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

from app.core import state


async def save_menu(data: dict[str, Any], *, user_id: int, chat_id: int) -> str:
    token = uuid4().hex[:16]
    await state.link_cache.set(
        f"menu:{token}",
        {"data": deepcopy(data), "user_id": user_id, "chat_id": chat_id},
    )
    return token


async def load_menu(token: str, query: Any) -> dict[str, Any] | None:
    saved = await state.link_cache.get(f"menu:{token}")
    if not isinstance(saved, dict) or (saved.get("user_id"), saved.get("chat_id")) != (
        getattr(query.from_user, "id", None),
        query.message.chat_id,
    ):
        return None
    data = saved.get("data")
    return deepcopy(data) if isinstance(data, dict) else None
