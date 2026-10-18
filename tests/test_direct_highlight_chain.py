"""Direct highlight links retain clips through the serialized selection menu."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_ig_callback_chains import _button, _click, _serialized
from test_ig_callback_chains import (
    ig_chain as ig_chain,  # noqa: PLC0414 - shared fixture
)

from app.bot import ig_callbacks, messages
from app.services.media.models import ClipInterval


@pytest.mark.parametrize("all_items", [False, True])
async def test_direct_highlight_clip_reaches_selected_delivery(ig_chain, all_items):
    env = ig_chain
    message = SimpleNamespace(reply_text=AsyncMock(return_value=env.query))
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
    )
    env.query.edit_text = env.query.edit_message_text
    context = SimpleNamespace(bot=MagicMock())
    await messages._handle_instagram(
        update,
        context,
        "https://www.instagram.com/stories/highlights/7/",
        "direct",
        "*00:10-00:20",
        "highlight",
        "7",
        None,
    )
    await env.cache.set("direct_hl_7", _serialized(await env.cache.get("direct_hl_7")))
    handler = (
        ig_callbacks.on_ig_hl_download_all
        if all_items
        else ig_callbacks.on_ig_hl_download
    )
    action = "ig_hl_dl_all" if all_items else "ig_hl_dl"
    await _click(env, handler, _button(env, action))

    assert env.bot.sends == [
        (42, [("video", b"asset:41"), ("photo", b"asset:32")])
        if all_items
        else (42, [("video", b"asset:41")])
    ]
    request, candidate = env.transport.calls[0]
    assert request.clip == ClipInterval(10, 20)
    assert request.auth_scope == candidate.auth_scope == "instagram:direct_hl_7"
    assert all(reservation.released == 1 for reservation in env.transport.reservations)
