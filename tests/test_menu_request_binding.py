"""Old Telegram menus keep the request and metadata they originally displayed."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from app.bot import callbacks, messages
from app.core import state
from app.core.storage.memory import MemoryStorage
from app.core.texts import Texts
from app.services.ytdlp.models import ExtractionResult, FormatItem


@pytest.fixture
def menu_env(monkeypatch):
    cache = MemoryStorage(maxsize=100, ttl=600)
    monkeypatch.setattr(state, "link_cache", cache)
    monkeypatch.setattr(state, "info_cache", MemoryStorage(maxsize=20, ttl=600))
    monkeypatch.setattr(state, "cancel_cache", MemoryStorage(maxsize=20, ttl=600))
    context = SimpleNamespace(user_data={}, bot=AsyncMock())
    return SimpleNamespace(context=context, cache=cache)


def extraction(title, format_id, height, *, slideshow=False):
    return ExtractionResult(
        title,
        [FormatItem(format_id, "mp4", height, height * 1000)],
        None,
        "00:10",
        slideshow,
        f"{title}.json",
        None,
    )


async def publish(env, url, result, token):
    await state.info_cache.set(url, result)
    status = MagicMock(spec=Message)
    status.chat_id = 9
    status.edit_text = AsyncMock()
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
    await messages._complete_message_parse(
        env.context,
        message,
        SimpleNamespace(id=7),
        SimpleNamespace(id=9),
        url,
        "*1-3",
        token,
    )
    return status.edit_text.await_args.kwargs["reply_markup"]


def query(data, *, user_id=7, chat_id=9):
    message = MagicMock(spec=Message)
    message.chat_id = chat_id
    message.photo = ()
    message.video = message.animation = message.document = None
    return SimpleNamespace(
        data=data,
        message=message,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


async def test_old_picker_uses_its_own_request_after_a_new_link(menu_env):
    env = menu_env
    first = await publish(
        env, "https://example.com/first", extraction("First", "137", 1080), "first"
    )
    await publish(
        env, "https://example.com/second", extraction("Second", "18", 360), "second"
    )
    q = query(first.inline_keyboard[0][0].callback_data)

    await callbacks.on_pick(SimpleNamespace(callback_query=q), env.context)

    tokens = [
        button.callback_data.split("|")[-1]
        for row in q.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and "|send|" in button.callback_data
    ]
    payload = await env.cache.get(tokens[0])
    assert (payload.page_url, payload.format_id, payload.height, payload.title) == (
        "https://example.com/first",
        "137",
        1080,
        "First",
    )
    assert payload.info_json_path == "First.json"
    assert payload.section == "*1-3"


async def test_refreshed_back_menu_rebuilds_all_format_metadata(menu_env, monkeypatch):
    env = menu_env
    env.context.user_data = {
        "page_url": "https://example.com/first",
        "title": "Old",
        "format_map": {"137": 1080},
        "size_map": {"137": 1},
        "info_json_path": "Old.json",
    }
    await state.info_cache.set(
        "https://example.com/first", extraction("Fresh", "18", 360)
    )
    back = query("m2|back")
    await callbacks.on_back(SimpleNamespace(callback_query=back), env.context)
    markup = back.edit_message_text.await_args.kwargs["reply_markup"]
    pick = query(markup.inline_keyboard[0][0].callback_data)
    await callbacks.on_pick(SimpleNamespace(callback_query=pick), env.context)
    send = next(
        button
        for row in pick.edit_message_text.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for button in row
        if button.callback_data and "|send|" in button.callback_data
    )
    payload = await env.cache.get(send.callback_data.split("|")[-1])
    assert (payload.title, payload.height, payload.info_json_path) == (
        "Fresh",
        360,
        "Fresh.json",
    )


async def test_expired_bound_picker_never_uses_the_latest_request(menu_env):
    env = menu_env
    first = await publish(
        env, "https://example.com/first", extraction("First", "137", 1080), "first"
    )
    data = first.inline_keyboard[0][0].callback_data
    token = data.split("|")[-2]
    await env.cache.delete(f"menu:{token}")
    await publish(
        env, "https://example.com/second", extraction("Second", "18", 360), "second"
    )
    q = query(data)
    await callbacks.on_pick(SimpleNamespace(callback_query=q), env.context)
    assert q.edit_message_text.await_args.args[0] == Texts.DATA_EXPIRED_RESEND


@pytest.mark.parametrize("user_id,chat_id", [(8, 9), (7, 10)])
async def test_bound_picker_cannot_move_between_users_or_chats(
    menu_env, user_id, chat_id
):
    env = menu_env
    first = await publish(
        env, "https://example.com/first", extraction("First", "137", 1080), "first"
    )
    q = query(
        first.inline_keyboard[0][0].callback_data, user_id=user_id, chat_id=chat_id
    )
    await callbacks.on_pick(SimpleNamespace(callback_query=q), env.context)
    assert q.edit_message_text.await_args.args[0] == Texts.DATA_EXPIRED_RESEND


async def test_bound_back_after_a_new_link_restores_the_original_menu(menu_env):
    env = menu_env
    first = await publish(
        env, "https://example.com/first", extraction("First", "137", 1080), "first"
    )
    pick = query(first.inline_keyboard[0][0].callback_data)
    await callbacks.on_pick(SimpleNamespace(callback_query=pick), env.context)
    back = next(
        button
        for row in pick.edit_message_text.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for button in row
        if button.callback_data and "|back|" in button.callback_data
    )
    await publish(
        env, "https://example.com/second", extraction("Second", "18", 360), "second"
    )
    q = query(back.callback_data)
    await callbacks.on_back(SimpleNamespace(callback_query=q), env.context)
    assert "First" in q.edit_message_text.await_args.args[0]
    restored = q.edit_message_text.await_args.kwargs["reply_markup"]
    selected = query(restored.inline_keyboard[0][0].callback_data)
    await callbacks.on_pick(SimpleNamespace(callback_query=selected), env.context)
    assert "1080p" in selected.edit_message_text.await_args.args[0]


async def test_slideshow_audio_uses_its_snapshot_without_video_maps(menu_env):
    env = menu_env
    first = await publish(
        env,
        "https://example.com/first",
        extraction("Slides", "137", 1080, slideshow=True),
        "first",
    )
    await publish(
        env, "https://example.com/second", extraction("Second", "18", 360), "second"
    )
    audio = next(
        button
        for row in first.inline_keyboard
        for button in row
        if "|pick|" in button.callback_data
    )
    q = query(audio.callback_data)
    await callbacks.on_pick(SimpleNamespace(callback_query=q), env.context)
    assert "Audio" in q.edit_message_text.await_args.args[0]
    send = next(
        button
        for row in q.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and "|send|" in button.callback_data
    )
    payload = await env.cache.get(send.callback_data.split("|")[-1])
    assert payload.page_url == "https://example.com/first"


async def test_slideshow_callback_uses_original_menu_after_a_new_link(
    menu_env, monkeypatch
):
    env = menu_env
    first = await publish(
        env,
        "https://example.com/first",
        extraction("Slides", "137", 1080, slideshow=True),
        "first",
    )
    await publish(
        env, "https://example.com/second", extraction("Second", "18", 360), "second"
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
    download = AsyncMock(return_value=(None, "unavailable"))
    monkeypatch.setattr(callbacks.MediaSender, "download_slideshow", download)
    q = query(first.inline_keyboard[0][0].callback_data)
    await callbacks.on_slideshow(SimpleNamespace(callback_query=q), env.context)
    download.assert_awaited_once_with("https://example.com/first")
