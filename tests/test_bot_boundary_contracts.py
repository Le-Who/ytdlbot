"""Observable command/preferences and authorized-story callback contracts."""

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from app.bot import commands, ig_callbacks
from app.core import state
from app.core.job_store import DeliveryOutcome, JobStore, delivery_job_context
from app.core.models import DownloadContext
from app.core.storage.memory import MemoryStorage
from app.services.instagram import IGStoryItem
from app.services.media.delivery import TelegramDelivery
from app.services.media.models import ClipInterval, DeliveryReceipt, DeliveryStatus
from app.services.media.pipeline import MediaPipeline
from app.services.media.registry import ProviderRegistry
from app.services.media.transport import MaterializedItem


@pytest.fixture
def command_env(monkeypatch):
    cache = MemoryStorage(maxsize=10, ttl=60)
    monkeypatch.setattr(state, "prefs_cache", cache)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=42))
    return update, SimpleNamespace(args=[]), cache


@pytest.mark.parametrize("args", [[], ["document"]])
async def test_setformat_invalid_input_preserves_saved_preference(command_env, args):
    update, context, cache = command_env
    await cache.set("prefs:42", {"default_format": "audio"})
    context.args = args

    await commands.cmd_setformat(update, context)

    assert await cache.get("prefs:42") == {"default_format": "audio"}
    update.message.reply_text.assert_awaited_once()


@pytest.mark.parametrize("value", ["video", "AUDIO"])
async def test_setformat_persists_normalized_choice(command_env, value):
    update, context, cache = command_env
    context.args = [value]

    await commands.cmd_setformat(update, context)

    assert await cache.get("prefs:42") == {"default_format": value.lower()}
    assert update.message.reply_text.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.parametrize("value, quality", [("720", 720), ("best", None)])
async def test_setquality_preserves_format_and_accepts_best(
    command_env, value, quality
):
    update, context, cache = command_env
    await cache.set("prefs:42", {"default_format": "video", "default_quality": 1080})
    context.args = [value]

    await commands.cmd_setquality(update, context)

    assert await cache.get("prefs:42") == {
        "default_format": "video",
        "default_quality": quality,
    }


@pytest.mark.parametrize("args", [[], ["4k"]])
async def test_setquality_invalid_input_preserves_preference(command_env, args):
    update, context, cache = command_env
    await cache.set("prefs:42", {"default_quality": 720})
    context.args = args

    await commands.cmd_setquality(update, context)

    assert await cache.get("prefs:42") == {"default_quality": 720}


async def test_settings_reset_clears_only_current_user(command_env):
    update, context, cache = command_env
    await cache.set("prefs:42", {"default_quality": 720})
    await cache.set("prefs:43", {"default_format": "audio"})
    context.args = ["RESET"]

    await commands.cmd_settings(update, context)

    assert await cache.get("prefs:42") is None
    assert await cache.get("prefs:43") == {"default_format": "audio"}


@pytest.mark.parametrize(
    "prefs, visible",
    [({}, "—"), ({"default_format": "video", "default_quality": 720}, "720p")],
)
async def test_settings_displays_saved_quality(command_env, prefs, visible):
    update, context, cache = command_env
    await cache.set("prefs:42", prefs)

    await commands.cmd_settings(update, context)

    assert visible in update.message.reply_text.await_args.args[0]


@pytest.mark.parametrize(
    "handler, data",
    [
        (ig_callbacks.on_ig_download, "m2|ig_dl|story-selection|31"),
        (ig_callbacks.on_ig_download_all, "m2|ig_dl_all|story-selection"),
    ],
)
async def test_story_callback_preserves_cached_clip(monkeypatch, handler, data):
    """A cached explicit clip must survive the story-selection callback."""
    cache = MemoryStorage(maxsize=10, ttl=60)
    await cache.set(
        "story-selection",
        DownloadContext(
            page_url="https://www.instagram.com/stories/tester/",
            section="*00:10-00:20",
            api_json={
                "stories": [
                    {
                        "mediaid": "31",
                        "is_video": True,
                        "url": "https://cdn.example/story.mp4",
                        "thumbnail_url": None,
                        "timestamp": "2026-10-09T10:00:00",
                        "duration": 30,
                        "typename": "GraphStoryVideo",
                    }
                ]
            },
        ),
    )
    captured = []

    class Pipeline:
        async def deliver_candidate(self, request, target, candidate, **options):
            captured.append((request, target, candidate))
            return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)

    monkeypatch.setattr(state, "link_cache", cache)
    monkeypatch.setattr(state, "media_pipeline", Pipeline())
    message = MagicMock(spec=Message)
    message.chat_id = 42
    message.reply_text = AsyncMock()
    query = SimpleNamespace(
        data=data, message=message, answer=AsyncMock(), edit_message_text=AsyncMock()
    )

    await handler(SimpleNamespace(callback_query=query), SimpleNamespace(bot=object()))

    assert len(captured) == 1
    request, target, candidate = captured[0]
    assert request.clip == ClipInterval(10, 20)
    assert request.auth_scope == "instagram:story-selection"
    assert target.auth_scope == request.auth_scope
    assert [item.media_id for item in candidate.items] == ["31"]


@pytest.mark.parametrize(
    "handler, data",
    [
        (ig_callbacks.on_ig_stories, "m2|ig_stories|gone"),
        (ig_callbacks.on_ig_highlights, "m2|ig_highlights|gone"),
        (ig_callbacks.on_ig_menu, "m2|ig_menu|gone"),
    ],
)
async def test_expired_instagram_menu_does_not_deliver(monkeypatch, handler, data):
    monkeypatch.setattr(state, "link_cache", MemoryStorage(maxsize=10, ttl=60))
    deliver = AsyncMock()
    monkeypatch.setattr(
        state, "media_pipeline", SimpleNamespace(deliver_candidate=deliver)
    )
    message = MagicMock(spec=Message)
    query = SimpleNamespace(
        data=data, message=message, answer=AsyncMock(), edit_message_text=AsyncMock()
    )

    await handler(SimpleNamespace(callback_query=query), SimpleNamespace(bot=object()))

    query.edit_message_text.assert_awaited_once()
    deliver.assert_not_awaited()


async def test_authorized_story_receipt_failure_records_job_error_with_false_result(
    tmp_path, monkeypatch
):
    failure = sqlite3.OperationalError("synthetic authorized receipt write failure")

    class FailingFinalizationStore(JobStore):
        def _finalize_delivery_attempt_sync(self, *args, **kwargs):
            raise failure

    class Reservation:
        released = 0

        def renew(self):
            return None

        async def release(self):
            self.released += 1

    source = tmp_path / "source.mp4"
    original = b"\x00\x00\x00\x18ftypisom"
    source.write_bytes(original)
    materialized_path = tmp_path / "materialized.mp4"
    materialized_path.write_bytes(original)
    reservation = Reservation()
    requests = []

    class Transport:
        async def materialize(self, request, candidates, **kwargs):
            requests.append(request)
            (candidate,) = candidates
            return MaterializedItem(
                (materialized_path,), len(original), candidate, reservation
            )

    bot = MagicMock()
    bot.send_video = AsyncMock(
        return_value=SimpleNamespace(
            message_id=124, video=SimpleNamespace(file_id="authorized-fid")
        )
    )
    validator = AsyncMock()
    pipeline = MediaPipeline(
        ProviderRegistry([]),
        Transport(),
        TelegramDelivery(
            bot, media_dir=tmp_path, local_mode=False, bot_id="boundary-test"
        ),
        artifact_validator=validator,
    )
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    story = IGStoryItem(
        mediaid="31",
        is_video=True,
        url="https://scontent.cdninstagram.com/story.mp4",
        thumbnail_url="",
        timestamp=datetime(2026, 10, 9, tzinfo=UTC),
    )
    store = FailingFinalizationStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 992})
    claimed = await store.claim_next("worker-a")
    assert claimed is not None

    with delivery_job_context(store, claimed.id, owner_id="worker-a") as execution:
        success = await ig_callbacks._deliver_authorized_stories(
            SimpleNamespace(bot=bot), 42, [story], auth_scope="boundary-test"
        )

    assert success is False
    bot.send_video.assert_awaited_once()
    validator.assert_awaited_once()
    assert len(requests) == 1
    key = f"42:request:{requests[0].cache_key}:0"
    assert (
        await store.delivery_outcome(claimed.id, item_key=key)
        is DeliveryOutcome.UNCERTAIN
    )
    assert await store.has_unsafe_deliveries(claimed.id)
    assert not await store.has_failed_deliveries(claimed.id)
    assert reservation.released == 1
    assert not materialized_path.exists()
    assert source.read_bytes() == original
    # False remains the UI contract; the worker must still receive the storage failure.
    assert execution.error is failure
