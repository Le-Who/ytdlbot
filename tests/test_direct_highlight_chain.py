"""Direct highlight links retain clips through the serialized selection menu."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_ig_callback_chains import _button, _click, _serialized
from test_ig_callback_chains import (
    ig_chain as ig_chain,  # noqa: PLC0414 - shared fixture
)

from app.bot import group_logic, ig_callbacks, messages
from app.core import state
from app.services.media.models import ClipInterval


@pytest.mark.parametrize("caller_scope", ["private", "group"])
@pytest.mark.parametrize("item_id", ["41", "99"])
async def test_shared_highlight_delivers_only_the_requested_item(
    ig_chain, monkeypatch, caller_scope, item_id
):
    env = ig_chain
    url = f"https://www.instagram.com/s/aGlnaGxpZ2h0Ojc=/?xtok=example&story_media_id={item_id}_123"
    status = SimpleNamespace(message_id=100, edit_text=AsyncMock(), delete=AsyncMock())
    message = SimpleNamespace(
        text=f"{url} 00:10-00:20", reply_text=AsyncMock(return_value=status)
    )
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=1, username="tester"),
        effective_chat=SimpleNamespace(id=42),
    )
    monkeypatch.setattr(
        state,
        "limiter",
        SimpleNamespace(
            allow_user=AsyncMock(return_value=True),
            allow_chat=AsyncMock(return_value=True),
        ),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    handler = (
        messages.on_message
        if caller_scope == "private"
        else group_logic.handle_group_message
    )
    await handler(update, context)

    assert message.reply_text.await_count == 1
    if item_id == "99":
        assert not env.bot.sends
        assert "Элемент хайлайта не найден" in status.edit_text.await_args.args[0]
        return
    assert env.bot.sends == [(42, [("video", b"asset:41")])]
    request, candidate = env.transport.calls[0]
    assert request.media_id == "41"
    assert request.clip == ClipInterval(10, 20)
    assert request.caller_scope == caller_scope
    assert request.auth_scope == candidate.auth_scope != "public"
    status.delete.assert_awaited_once()


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
