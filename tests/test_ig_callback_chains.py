"""Serialized Instagram menus retain exact selection and delivery outcomes."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from telegram import Message

from app.bot import ig_callbacks
from app.core import state
from app.core.job_store import DeliveryOutcome, JobStore, delivery_job_context
from app.core.models import DownloadContext
from app.core.storage.memory import MemoryStorage
from app.core.texts import Texts
from app.services.instagram import IGStoryItem, InstagramService
from app.services.media.delivery import TelegramDelivery
from app.services.media.models import ClipInterval, MediaKind
from app.services.media.pipeline import MediaPipeline
from app.services.media.registry import ProviderRegistry
from app.services.media.transport import MaterializedItem


def _serialized(value):
    """Match the untyped dictionaries returned by the production msgpack cache."""
    return msgspec.msgpack.decode(msgspec.msgpack.encode(value))


def _cached_items():
    return [
        {
            "mediaid": "41",
            "is_video": True,
            "url": "https://scontent.cdninstagram.com/41.mp4",
            "thumbnail_url": "https://scontent.cdninstagram.com/41.jpg",
            "timestamp": "2026-10-18T08:00:00+00:00",
            "duration": 30,
            "typename": "GraphStoryVideo",
        },
        {
            "mediaid": "32",
            "is_video": False,
            "url": "https://scontent.cdninstagram.com/32.jpg",
            "thumbnail_url": "https://scontent.cdninstagram.com/32.jpg",
            "timestamp": "2026-10-18T09:00:00+00:00",
            "duration": None,
            "typename": "GraphStoryImage",
        },
    ]


class _Reservation:
    def __init__(self):
        self.released = 0

    def renew(self):
        pass

    async def release(self):
        self.released += 1


class _Transport:
    """Replace external CDN download/conversion; retain the real delivery pipeline."""

    def __init__(self, directory):
        self.directory = directory
        self.calls = []
        self.reservations = []

    async def materialize(self, request, candidates, **options):
        (candidate,) = candidates
        self.calls.append((request, candidate))
        paths = []
        for item in candidate.items:
            path = self.directory / f"{item.media_id}.{item.container}"
            path.write_bytes(f"asset:{item.media_id}".encode())
            paths.append(path)
        reservation = _Reservation()
        self.reservations.append(reservation)
        return MaterializedItem(
            tuple(paths),
            sum(path.stat().st_size for path in paths),
            candidate,
            reservation,
        )


class _Bot:
    """Capture bytes at the Telegram boundary while the owned files still exist."""

    def __init__(self):
        self.sends = []
        self.incomplete_album_receipt = False

    def _send(self, chat_id, media):
        assets = [
            (kind, Path(source.removeprefix("file://"))) for kind, source in media
        ]
        self.sends.append(
            (chat_id, [(kind, path.read_bytes()) for kind, path in assets])
        )
        return [
            SimpleNamespace(
                message_id=100 + index,
                video=SimpleNamespace(file_id=f"video-{index}"),
                photo=[SimpleNamespace(file_id=f"photo-{index}")],
            )
            for index, _ in enumerate(assets)
        ]

    async def send_video(self, *, chat_id, video, **options):
        return self._send(chat_id, [("video", video)])[0]

    async def send_photo(self, *, chat_id, photo, **options):
        return self._send(chat_id, [("photo", photo)])[0]

    async def send_media_group(self, *, chat_id, media, **options):
        messages = self._send(chat_id, [(item.type, item.media) for item in media])
        return messages[:1] if self.incomplete_album_receipt else messages


@pytest.fixture
async def ig_chain(tmp_path, monkeypatch):
    cache = MemoryStorage(maxsize=20, ttl=60)
    await cache.set(
        "sel",
        _serialized(
            DownloadContext(
                page_url="https://www.instagram.com/tester/",
                user_tag="tester",
                section="*00:10-00:20",
                api_source="instagram",
                api_json={
                    "stories": _cached_items(),
                    "highlights": [
                        {
                            "highlight_id": "7",
                            "title": "Travel",
                            "cover_url": "https://scontent.cdninstagram.com/cover.jpg",
                            "item_count": 2,
                        }
                    ],
                },
            )
        ),
    )
    bot = _Bot()
    transport = _Transport(tmp_path)

    async def validate_fixture(request, materialized):
        # Synthetic files stand in for the external media/ffprobe boundary.
        assert all(path.is_file() for path in materialized.paths)

    pipeline = MediaPipeline(
        ProviderRegistry([]),
        transport,
        TelegramDelivery(bot, media_dir=tmp_path, local_mode=True, bot_id="test-bot"),
        artifact_validator=validate_fixture,
    )
    monkeypatch.setattr(state, "link_cache", cache)
    monkeypatch.setattr(state, "media_pipeline", pipeline)

    async def highlight_items(highlight_id):
        assert highlight_id == "7"
        return [
            IGStoryItem(**{**item, "timestamp": datetime(2026, 10, 18, 8, tzinfo=UTC)})
            for item in _cached_items()
        ], None

    async def forbid_legacy_download(*args, **kwargs):
        pytest.fail("Authorized pipeline callback reached the legacy downloader")

    monkeypatch.setattr(InstagramService, "get_highlight_items", highlight_items)
    monkeypatch.setattr(InstagramService, "download_story_item", forbid_legacy_download)
    message = MagicMock(spec=Message)
    message.chat_id = 42
    message.reply_text = AsyncMock()
    query = SimpleNamespace(
        data="m2|ig_menu|sel",
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        cache=cache,
        bot=bot,
        transport=transport,
        query=query,
        update=SimpleNamespace(callback_query=query),
        context=SimpleNamespace(bot=bot),
    )


def _button(env, action, position=0):
    keyboard = env.query.edit_message_text.await_args.kwargs["reply_markup"]
    return [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.split("|")[1] == action
    ][position]


async def _click(env, handler, data):
    env.query.data = data
    await handler(env.update, env.context)


async def _open_selection(env, source):
    await ig_callbacks.on_ig_menu(env.update, env.context)
    if source == "story":
        await _click(env, ig_callbacks.on_ig_stories, _button(env, "ig_stories"))
    else:
        await _click(env, ig_callbacks.on_ig_highlights, _button(env, "ig_highlights"))
        await _click(
            env, ig_callbacks.on_ig_highlight_items, _button(env, "ig_hl_items")
        )
        # Highlight selection is another cache boundary, independently of profile cache.
        await env.cache.set("sel_hl_7", _serialized(await env.cache.get("sel_hl_7")))


@pytest.mark.parametrize("source", ["story", "highlight"])
@pytest.mark.parametrize("all_items", [False, True], ids=["single", "all"])
async def test_serialized_menu_selection_delivers_exact_items(
    ig_chain, source, all_items
):
    """Catch lost clip/auth, reordered items, or selecting the wrong numbered item."""
    env = ig_chain
    await _open_selection(env, source)
    if source == "story":
        action = "ig_dl_all" if all_items else "ig_dl"
        handler = (
            ig_callbacks.on_ig_download_all
            if all_items
            else ig_callbacks.on_ig_download
        )
    else:
        action = "ig_hl_dl_all" if all_items else "ig_hl_dl"
        handler = (
            ig_callbacks.on_ig_hl_download_all
            if all_items
            else ig_callbacks.on_ig_hl_download
        )
    await _click(env, handler, _button(env, action, 0 if all_items else 1))

    assert env.bot.sends == [
        (42, [("video", b"asset:41"), ("photo", b"asset:32")])
        if all_items
        else (42, [("photo", b"asset:32")])
    ]
    assert len(env.transport.calls) == 1
    request, candidate = env.transport.calls[0]
    assert request.clip == ClipInterval(10, 20)
    assert request.kind is (MediaKind.ALBUM if all_items else MediaKind.PHOTO)
    assert request.exact
    assert request.caller_scope == "ig_callback"
    assert request.auth_scope == (
        "instagram:sel" if source == "story" else "instagram:sel_hl_7"
    )
    assert candidate.auth_scope == request.auth_scope
    assert candidate.provider == "authorized-instagram"
    assert [item.media_id for item in candidate.items] == (
        ["41", "32"] if all_items else ["32"]
    )
    assert [item.url for item in candidate.items] == (
        [
            "https://scontent.cdninstagram.com/41.mp4",
            "https://scontent.cdninstagram.com/32.jpg",
        ]
        if all_items
        else ["https://scontent.cdninstagram.com/32.jpg"]
    )
    assert all(reservation.released == 1 for reservation in env.transport.reservations)
    env.query.message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("source", ["story", "highlight"])
@pytest.mark.parametrize("all_items", [False, True], ids=["single", "all"])
async def test_malformed_cached_timestamp_expires_selection_before_delivery(
    ig_chain, source, all_items
):
    """A valid first item cannot be sent before a corrupt later item is rejected."""
    env = ig_chain
    await _open_selection(env, source)
    if source == "story":
        payload = await env.cache.get("sel")
        payload["api_json"]["stories"][1]["timestamp"] = "invalid-timestamp"
        await env.cache.set("sel", _serialized(payload))
        action = "ig_dl_all" if all_items else "ig_dl"
        handler = (
            ig_callbacks.on_ig_download_all
            if all_items
            else ig_callbacks.on_ig_download
        )
    else:
        payload = await env.cache.get("sel_hl_7")
        items = payload["items"] if isinstance(payload, dict) else payload
        items[1]["timestamp"] = "invalid-timestamp"
        await env.cache.set("sel_hl_7", _serialized(payload))
        action = "ig_hl_dl_all" if all_items else "ig_hl_dl"
        handler = (
            ig_callbacks.on_ig_hl_download_all
            if all_items
            else ig_callbacks.on_ig_hl_download
        )

    await _click(env, handler, _button(env, action, 0 if all_items else 1))

    assert env.query.edit_message_text.await_args.args == (Texts.CACHE_EXPIRED_RESEND,)
    assert env.transport.calls == []
    assert env.bot.sends == []


@pytest.mark.parametrize("all_items", [False, True], ids=["single", "all"])
async def test_legacy_highlight_list_still_delivers(ig_chain, all_items):
    env = ig_chain
    await env.cache.set("legacy_hl_7", _serialized(_cached_items()))
    handler = (
        ig_callbacks.on_ig_hl_download_all
        if all_items
        else ig_callbacks.on_ig_hl_download
    )
    data = "m2|ig_hl_dl_all|legacy_hl_7" if all_items else "m2|ig_hl_dl|legacy_hl_7|32"

    await _click(env, handler, data)

    assert env.bot.sends == [
        (42, [("video", b"asset:41"), ("photo", b"asset:32")])
        if all_items
        else (42, [("photo", b"asset:32")])
    ]
    request, _ = env.transport.calls[0]
    assert request.clip == ClipInterval()
    assert request.auth_scope == "instagram:legacy_hl_7"


async def test_incomplete_album_receipt_does_not_replay_success_or_uncertainty(
    ig_chain, tmp_path
):
    env = ig_chain
    await _open_selection(env, "story")
    data = _button(env, "ig_dl_all")
    env.bot.incomplete_album_receipt = True
    store = JobStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 123})
    job = await store.claim_next("ig-test")
    assert job is not None

    with delivery_job_context(store, job.id, owner_id="ig-test"):
        await _click(env, ig_callbacks.on_ig_download_all, data)
        await _click(env, ig_callbacks.on_ig_download_all, data)

    assert env.bot.sends == [(42, [("video", b"asset:41"), ("photo", b"asset:32")])]
    request, _ = env.transport.calls[0]
    assert (
        await store.delivery_outcome(
            job.id, item_key=f"42:request:{request.cache_key}:0"
        )
        is DeliveryOutcome.SUCCESS
    )
    assert (
        await store.delivery_outcome(
            job.id, item_key=f"42:request:{request.cache_key}:1"
        )
        is DeliveryOutcome.UNCERTAIN
    )
    assert await store.has_unsafe_deliveries(job.id)
    assert not await store.has_failed_deliveries(job.id)
    assert env.query.edit_message_text.await_args.args == (Texts.IG_DOWNLOAD_ERROR,)
    assert "reply_markup" not in env.query.edit_message_text.await_args.kwargs
