from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from telegram import Message

from app.constants import AUDIO_FORMAT_ID
from app.core.models import DownloadContext
from app.services.media.models import (
    ClipInterval,
    DeliveryReceipt,
    DeliveryStatus,
    MediaCandidate,
    MediaItem,
    MediaKind,
    ResolvedMedia,
)
from app.services.media.pipeline import (
    CallbackDataError,
    MediaPipelineError,
    build_media_request,
    decode_callback_data,
    encode_callback_data,
    request_from_download_context,
)

YOUTUBE_URL = "https://m.youtube.com/watch?v=abc123&utm_source=test"


class _CapturingPipeline:
    def __init__(self) -> None:
        self.requests = []

    async def deliver(self, request, target, **kwargs):
        self.requests.append(request)
        return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)

    async def deliver_candidate(self, request, target, candidate, **kwargs):
        self.requests.append(request)
        return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)


@pytest.mark.parametrize(
    "entrypoint",
    ["private", "group", "mp3", "mp4", "callback", "ig_callback", "api"],
)
def test_every_entrypoint_builds_the_same_canonical_media_request(entrypoint: str):
    request = build_media_request(
        YOUTUBE_URL,
        kind=MediaKind.VIDEO,
        quality=1080,
        clip="10-20",
        caller_scope=entrypoint,
    )

    assert request.canonical_url == "https://www.youtube.com/watch?v=abc123"
    assert request.quality.max_edge == 1080
    assert request.clip == ClipInterval(10, 20)
    assert request.kind is MediaKind.VIDEO
    assert request.auth_scope == "public"


def test_legacy_download_context_maps_mp3_clip_and_quality_without_fallback():
    request = request_from_download_context(
        DownloadContext(
            page_url=YOUTUBE_URL,
            format_id=AUDIO_FORMAT_ID,
            height=1080,
            section="*00:10-00:20",
        ),
        caller_scope="callback",
    )

    assert request.kind is MediaKind.AUDIO
    assert request.audio_format == "mp3"
    assert request.quality.max_edge == 1080
    assert request.clip == ClipInterval(10, 20)
    assert request.exact is True


def test_fast_mode_is_explicit_and_does_not_rewrite_requested_fields():
    request = build_media_request(
        YOUTUBE_URL,
        kind=MediaKind.AUDIO,
        audio_format="mp3",
        audio_language="uk",
        quality=720,
        clip=ClipInterval(3, 9),
        exact=False,
    )

    assert request.exact is False
    assert request.kind is MediaKind.AUDIO
    assert request.audio_format == "mp3"
    assert request.audio_language == "uk"
    assert request.quality.max_edge == 720
    assert request.clip == ClipInterval(3, 9)


def test_other_valid_http_hosts_are_canonicalized_for_local_extractor_route():
    request = build_media_request("http://media.example.net/watch/42?utm_source=x")

    assert request.platform == "other"
    assert request.canonical_url == "https://media.example.net/watch/42"
    assert request.media_id == "42"


def test_callback_codec_emits_v2_and_accepts_previous_shape():
    encoded = encode_callback_data("send", "token123")

    assert encoded == "m2|send|token123"
    assert decode_callback_data(encoded, expected_action="send") == ("token123",)
    assert decode_callback_data("send|token123", expected_action="send") == (
        "token123",
    )


@pytest.mark.parametrize(
    "payload",
    ["", "m2", "m2|send", "m2|pick|", "m2|other|x", "send|a|b|c|d|e"],
)
def test_callback_codec_rejects_malformed_or_wrong_action(payload: str):
    with pytest.raises(CallbackDataError):
        decode_callback_data(payload, expected_action="send")


@pytest.mark.asyncio
async def test_http_download_uses_pipeline_request_and_releases_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.api import routes

    path = tmp_path / "video.mp4"
    path.write_bytes(b"pipeline-bytes")

    class Cache:
        async def get(self, token: str):
            assert token == "token"
            return DownloadContext(
                page_url=YOUTUBE_URL,
                format_id="22",
                height=1080,
                section="*00:10-00:20",
                title="clip",
            )

    class Limiter:
        async def allow_ip(self, ip: str) -> bool:
            return True

        async def allow_token(self, token: str) -> bool:
            return True

    class Pipeline:
        request = None
        released = False
        renewed = 0

        @asynccontextmanager
        async def open_materialized(self, request):
            self.request = request
            try:
                yield SimpleNamespace(
                    paths=(path,),
                    size_bytes=path.stat().st_size,
                    renew_lease=self._renew,
                )
            finally:
                self.released = True

        def _renew(self):
            self.renewed += 1

    pipeline = Pipeline()
    monkeypatch.setattr(routes.state, "link_cache", Cache())
    monkeypatch.setattr(routes.state, "limiter", Limiter())
    monkeypatch.setattr(routes.state, "media_pipeline", pipeline)

    response = await routes.download(
        "token", SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"pipeline-bytes"
    assert pipeline.request.canonical_url == "https://www.youtube.com/watch?v=abc123"
    assert pipeline.request.quality.max_edge == 1080
    assert pipeline.request.clip == ClipInterval(10, 20)
    assert pipeline.request.caller_scope == "api"
    assert pipeline.renewed >= 1
    assert pipeline.released


@pytest.mark.asyncio
async def test_slow_http_stream_periodically_renews_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.api import routes

    path = tmp_path / "video.mp4"
    path.write_bytes(b"ab")

    class Cache:
        async def get(self, token: str):
            return DownloadContext(page_url=YOUTUBE_URL, format_id="22")

    class Limiter:
        async def allow_ip(self, ip: str) -> bool:
            return True

        async def allow_token(self, token: str) -> bool:
            return True

    class Pipeline:
        renewed = 0
        released = False

        @asynccontextmanager
        async def open_materialized(self, request):
            try:
                yield SimpleNamespace(
                    paths=(path,),
                    size_bytes=2,
                    renew_lease=self._renew,
                )
            finally:
                self.released = True

        def _renew(self):
            self.renewed += 1

    pipeline = Pipeline()
    monkeypatch.setattr(routes.state, "link_cache", Cache())
    monkeypatch.setattr(routes.state, "limiter", Limiter())
    monkeypatch.setattr(routes.state, "media_pipeline", pipeline)
    monkeypatch.setattr(routes, "CHUNK_SIZE", 1)
    monkeypatch.setattr(routes, "_HTTP_LEASE_RENEW_SECONDS", 0.01)

    response = await routes.download(
        "token", SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    )
    iterator = response.body_iterator
    assert await anext(iterator) == b"a"
    await asyncio.sleep(0.035)
    assert await anext(iterator) == b"b"
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)

    assert pipeline.renewed >= 3
    assert pipeline.released


@pytest.mark.asyncio
async def test_http_stream_surfaces_lease_renewal_failure_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.api import routes

    path = tmp_path / "video.mp4"
    path.write_bytes(b"ab")

    class Cache:
        async def get(self, token: str):
            return DownloadContext(page_url=YOUTUBE_URL, format_id="22")

    class Limiter:
        async def allow_ip(self, ip: str) -> bool:
            return True

        async def allow_token(self, token: str) -> bool:
            return True

    class Pipeline:
        released = False

        @asynccontextmanager
        async def open_materialized(self, request):
            try:
                yield SimpleNamespace(
                    paths=(path,),
                    size_bytes=2,
                    renew_lease=self._renew,
                )
            finally:
                self.released = True

        def _renew(self):
            raise RuntimeError("lease owner changed")

    pipeline = Pipeline()
    monkeypatch.setattr(routes.state, "link_cache", Cache())
    monkeypatch.setattr(routes.state, "limiter", Limiter())
    monkeypatch.setattr(routes.state, "media_pipeline", pipeline)
    monkeypatch.setattr(routes, "CHUNK_SIZE", 1)
    monkeypatch.setattr(routes, "_HTTP_LEASE_RENEW_SECONDS", 0.01)

    response = await routes.download(
        "token", SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    )
    iterator = response.body_iterator
    with pytest.raises(RuntimeError, match="lease owner changed"):
        await anext(iterator)

    assert pipeline.released


@pytest.mark.asyncio
async def test_http_download_rejects_oversize_materialization_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.api import routes

    path = tmp_path / "video.mp4"
    path.write_bytes(b"small-test-file")

    class Cache:
        async def get(self, token: str):
            return DownloadContext(page_url=YOUTUBE_URL, format_id="22")

    class Limiter:
        async def allow_ip(self, ip: str) -> bool:
            return True

        async def allow_token(self, token: str) -> bool:
            return True

    class Pipeline:
        released = False

        @asynccontextmanager
        async def open_materialized(self, request):
            try:
                yield SimpleNamespace(
                    paths=(path,),
                    size_bytes=routes.max_media_file_bytes(limit_mb=routes.MAX_DL_MB)
                    + 1,
                    renew_lease=lambda: None,
                )
            finally:
                self.released = True

    pipeline = Pipeline()
    monkeypatch.setattr(routes.state, "link_cache", Cache())
    monkeypatch.setattr(routes.state, "limiter", Limiter())
    monkeypatch.setattr(routes.state, "media_pipeline", pipeline)

    with pytest.raises(HTTPException) as raised:
        await routes.download(
            "token", SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        )

    assert raised.value.status_code == 413
    assert pipeline.released


@pytest.mark.asyncio
async def test_http_download_accepts_exact_decimal_boundary_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.api import routes

    path = tmp_path / "video.mp4"
    path.write_bytes(b"ok")

    class Cache:
        async def get(self, token: str):
            return DownloadContext(page_url=YOUTUBE_URL, format_id="22")

    class Limiter:
        async def allow_ip(self, ip: str) -> bool:
            return True

        async def allow_token(self, token: str) -> bool:
            return True

    class Pipeline:
        released = False

        @asynccontextmanager
        async def open_materialized(self, request):
            try:
                yield SimpleNamespace(
                    paths=(path,),
                    size_bytes=routes.max_media_file_bytes(limit_mb=routes.MAX_DL_MB),
                    renew_lease=lambda: None,
                )
            finally:
                self.released = True

    pipeline = Pipeline()
    monkeypatch.setattr(routes.state, "link_cache", Cache())
    monkeypatch.setattr(routes.state, "limiter", Limiter())
    monkeypatch.setattr(routes.state, "media_pipeline", pipeline)

    response = await routes.download(
        "token", SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    )
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"ok"
    assert pipeline.released


@pytest.mark.parametrize("section", ("nan-20", "0-inf", "0--inf"))
def test_download_context_rejects_non_finite_clip_values(section: str):
    from app.services.media.pipeline import request_from_download_context

    with pytest.raises(ValueError, match="finite"):
        request_from_download_context(
            DownloadContext(
                page_url=YOUTUBE_URL,
                format_id="22",
                section=section,
            ),
            caller_scope="api",
        )


@pytest.mark.asyncio
async def test_private_and_group_messages_invoke_the_common_pipeline(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import group_logic, messages

    pipeline = _CapturingPipeline()
    monkeypatch.setattr(messages.state, "media_pipeline", pipeline)
    monkeypatch.setattr(messages, "get_prefs", AsyncMock(return_value={}))
    monkeypatch.setattr(
        messages, "extract_url_from_update", lambda message: (YOUTUBE_URL, "10-20")
    )
    monkeypatch.setattr(
        group_logic, "extract_url_from_update", lambda message: (YOUTUBE_URL, "10-20")
    )
    monkeypatch.setattr(
        "app.services.instagram.parse_instagram_url",
        lambda url: ("unknown", None, None),
    )
    limiter = SimpleNamespace(
        allow_user=AsyncMock(return_value=True),
        allow_chat=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(messages.state, "limiter", limiter)

    status = SimpleNamespace(delete=AsyncMock(), edit_text=AsyncMock())
    message = MagicMock()
    message.text = YOUTUBE_URL
    message.reply_text = AsyncMock(return_value=status)
    message.delete = AsyncMock()
    user = SimpleNamespace(id=7, username="tester", mention_html=lambda: "tester")
    chat = SimpleNamespace(id=9)
    update = SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        message=message,
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    await messages.on_message(update, context)
    assert message.reply_text.await_count == 1
    await group_logic.handle_group_message(update, context)
    assert message.reply_text.await_count == 2

    private, group = pipeline.requests
    assert private.canonical_url == group.canonical_url
    assert private.clip == group.clip == ClipInterval(10, 20)
    assert private.kind is group.kind is MediaKind.AUTO
    assert not private.exact and not group.exact
    assert (private.caller_scope, group.caller_scope) == ("private", "group")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller_scope", "format_id", "expected_kind"),
    [
        ("command", AUDIO_FORMAT_ID, MediaKind.AUDIO),
        ("command", "22", MediaKind.VIDEO),
        ("callback", AUDIO_FORMAT_ID, MediaKind.AUDIO),
        ("callback", "22", MediaKind.VIDEO),
    ],
)
async def test_command_and_callback_orchestrator_adapters_invoke_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    caller_scope: str,
    format_id: str,
    expected_kind: MediaKind,
):
    from app.core import state
    from app.services.orchestrator import DownloadOrchestrator

    class Queue:
        async def enqueue(self, update_ui, *, kb_error):
            return True

        def release(self) -> None:
            return None

    pipeline = _CapturingPipeline()
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    monkeypatch.setattr(state, "download_queue", Queue())
    monkeypatch.setattr(state, "api_queue", Queue())

    success = await DownloadOrchestrator.process_download(
        token="token",
        chat_id=9,
        bot=MagicMock(),
        payload=DownloadContext(
            page_url=YOUTUBE_URL,
            format_id=format_id,
            height=1080,
            section="*10-20",
        ),
        fmt_size=None,
        update_ui=AsyncMock(),
        kb_error=None,
        caller_scope=caller_scope,
    )

    assert success
    request = pipeline.requests[0]
    assert request.kind is expected_kind
    assert request.quality.max_edge == 1080
    assert request.clip == ClipInterval(10, 20)
    assert request.caller_scope == caller_scope


@pytest.mark.asyncio
async def test_initialized_pipeline_failure_never_falls_through_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.core import state
    from app.services import orchestrator

    class Queue:
        async def enqueue(self, update_ui, *, kb_error):
            return True

        def release(self) -> None:
            return None

    class FailingPipeline:
        async def deliver(self, request, target, **kwargs):
            raise MediaPipelineError("pipeline failed")

    def legacy_guard(*args, **kwargs):
        raise AssertionError("legacy path must not run after pipeline initialization")

    monkeypatch.setattr(state, "media_pipeline", FailingPipeline())
    monkeypatch.setattr(state, "download_queue", Queue())
    monkeypatch.setattr(state, "api_queue", Queue())
    monkeypatch.setattr(orchestrator, "size_allowed", legacy_guard)
    update_ui = AsyncMock()

    success = await orchestrator.DownloadOrchestrator.process_download(
        token="token",
        chat_id=9,
        bot=MagicMock(),
        payload=DownloadContext(page_url=YOUTUBE_URL, format_id="22"),
        fmt_size=None,
        update_ui=update_ui,
        kb_error="error-keyboard",
        caller_scope="callback",
    )

    assert not success
    update_ui.assert_awaited_with("pipeline failed", "error-keyboard")


@pytest.mark.asyncio
async def test_instagram_callback_keeps_authorized_scope_out_of_public_work(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import ig_callbacks

    pipeline = _CapturingPipeline()
    monkeypatch.setattr(ig_callbacks.state, "media_pipeline", pipeline)
    story = SimpleNamespace(
        is_video=True,
        url="https://scontent.example/story.mp4",
        mediaid="story-1",
        label="Story",
    )

    success = await ig_callbacks._deliver_authorized_stories(
        SimpleNamespace(), 9, [story], auth_scope="session-7"
    )

    assert success
    request = pipeline.requests[0]
    assert request.caller_scope == "ig_callback"
    assert request.auth_scope == "instagram:session-7"


@pytest.mark.asyncio
async def test_direct_instagram_story_private_and_group_use_authorized_pipeline(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import group_logic, messages

    story_url = "https://www.instagram.com/stories/tester/story-1/"
    story = SimpleNamespace(
        is_video=True,
        url="https://scontent.example/story.mp4",
        mediaid="story-1",
        label="Story",
    )
    profile = SimpleNamespace(error=None, stories=[story])
    pipeline = _CapturingPipeline()
    limiter = SimpleNamespace(
        allow_user=AsyncMock(return_value=True),
        allow_chat=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(messages.state, "media_pipeline", pipeline)
    monkeypatch.setattr(messages.state, "limiter", limiter)
    monkeypatch.setattr(
        messages, "extract_url_from_update", lambda message: (story_url, "10-20")
    )
    monkeypatch.setattr(
        group_logic, "extract_url_from_update", lambda message: (story_url, "10-20")
    )
    parse_story = lambda url: ("stories", "tester", "story-1")
    monkeypatch.setattr(messages, "parse_instagram_url", parse_story)
    monkeypatch.setattr("app.services.instagram.parse_instagram_url", parse_story)
    monkeypatch.setattr(
        messages.InstagramService,
        "get_profile_media",
        AsyncMock(return_value=profile),
    )
    monkeypatch.setattr(
        messages.InstagramService,
        "download_story_item",
        AsyncMock(side_effect=AssertionError("legacy story download must not run")),
    )

    def update_for(chat_id: int):
        status = SimpleNamespace(
            message_id=100,
            delete=AsyncMock(),
            edit_text=AsyncMock(),
        )
        message = MagicMock()
        message.text = story_url
        message.reply_text = AsyncMock(return_value=status)
        message.delete = AsyncMock()
        user = SimpleNamespace(
            id=7,
            username="tester",
            mention_html=lambda: "tester",
        )
        return (
            SimpleNamespace(
                effective_user=user,
                effective_chat=SimpleNamespace(id=chat_id),
                message=message,
            ),
            message,
        )

    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    private_update, private_message = update_for(9)
    await messages.on_message(private_update, context)
    group_update, group_message = update_for(10)
    await group_logic.handle_group_message(group_update, context)

    assert private_message.reply_text.await_count == 1
    assert group_message.reply_text.await_count == 1
    private, group = pipeline.requests
    assert private.clip == group.clip == ClipInterval(10, 20)
    assert private.caller_scope == "private"
    assert group.caller_scope == "group"
    assert private.auth_scope.startswith("instagram:")
    assert group.auth_scope.startswith("instagram:")


@pytest.mark.asyncio
async def test_new_tiktok_album_offers_pipeline_slideshow_choice_without_delivery(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import group_logic, messages

    url = "https://www.tiktok.com/@tester/photo/123"
    items = (
        MediaItem("123:0", MediaKind.PHOTO, "https://cdn.example/0.jpg"),
        MediaItem("123:1", MediaKind.PHOTO, "https://cdn.example/1.jpg"),
    )
    candidate = MediaCandidate(
        candidate_id="album",
        url=items[0].url,
        has_video=False,
        has_audio=False,
        provider="tikwm",
        media_id="123",
        kind=MediaKind.ALBUM,
        items=items,
    )

    class Pipeline:
        def __init__(self) -> None:
            self.requests = []
            self.deliveries = 0

        async def cached_item_count(self, request):
            return None

        async def resolve(self, request):
            self.requests.append(request)
            return ResolvedMedia(
                request=request,
                items=items,
                candidates=(candidate,),
                provider="tikwm",
            )

        async def deliver(self, request, target, **kwargs):
            self.deliveries += 1
            raise AssertionError("album must wait for the user's slideshow choice")

    class LinkCache:
        def __init__(self) -> None:
            self.values = {}

        async def set(self, key, value):
            self.values[key] = value

    pipeline = Pipeline()
    link_cache = LinkCache()
    limiter = SimpleNamespace(
        allow_user=AsyncMock(return_value=True),
        allow_chat=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(messages.state, "media_pipeline", pipeline)
    monkeypatch.setattr(messages.state, "link_cache", link_cache)
    monkeypatch.setattr(messages.state, "limiter", limiter)
    monkeypatch.setattr(messages, "get_prefs", AsyncMock(return_value={}))
    monkeypatch.setattr(
        messages, "extract_url_from_update", lambda message: (url, "10-20")
    )
    monkeypatch.setattr(
        group_logic, "extract_url_from_update", lambda message: (url, "10-20")
    )
    monkeypatch.setattr(
        "app.services.instagram.parse_instagram_url",
        lambda value: ("unknown", None, None),
    )
    monkeypatch.setattr(
        messages, "parse_instagram_url", lambda value: ("unknown", None, None)
    )

    def update_for(chat_id: int):
        status = SimpleNamespace(
            message_id=100,
            delete=AsyncMock(),
            edit_text=AsyncMock(),
        )
        message = MagicMock()
        message.text = url
        message.reply_text = AsyncMock(return_value=status)
        message.delete = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=7,
                username="tester",
                mention_html=lambda: "tester",
            ),
            effective_chat=SimpleNamespace(id=chat_id),
            message=message,
        )
        return update, message, status

    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    private_update, private_message, private_status = update_for(9)
    await messages.on_message(private_update, context)
    group_update, group_message, group_status = update_for(10)
    await group_logic.handle_group_message(group_update, context)

    assert pipeline.deliveries == 0
    assert len(pipeline.requests) == 2
    assert all(request.clip == ClipInterval(10, 20) for request in pipeline.requests)
    assert private_message.reply_text.await_count == 1
    assert group_message.reply_text.await_count == 1
    private_markup = private_status.edit_text.await_args.kwargs["reply_markup"]
    group_markup = group_status.edit_text.await_args.kwargs["reply_markup"]
    assert private_markup.inline_keyboard[0][0].callback_data.startswith("m2|apislide|")
    assert group_markup.inline_keyboard[0][0].callback_data.startswith(
        "m2|apigrpslide|"
    )
    assert all(value.api_source == "pipeline" for value in link_cache.values.values())


@pytest.mark.asyncio
async def test_warm_tiktok_cache_is_checked_before_album_resolution(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import group_logic, messages

    url = "https://www.tiktok.com/@tester/video/123"

    class Pipeline:
        def __init__(self) -> None:
            self.cache_checks = 0
            self.deliveries = 0

        async def cached_item_count(self, request):
            self.cache_checks += 1
            return 1

        async def resolve(self, request):
            raise AssertionError("warm TikTok cache must be checked before providers")

        async def deliver(self, request, target, **kwargs):
            self.deliveries += 1
            return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)

    pipeline = Pipeline()
    limiter = SimpleNamespace(
        allow_user=AsyncMock(return_value=True),
        allow_chat=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(messages.state, "media_pipeline", pipeline)
    monkeypatch.setattr(messages.state, "limiter", limiter)
    monkeypatch.setattr(messages, "get_prefs", AsyncMock(return_value={}))
    monkeypatch.setattr(
        messages, "extract_url_from_update", lambda message: (url, None)
    )
    monkeypatch.setattr(
        group_logic, "extract_url_from_update", lambda message: (url, None)
    )
    monkeypatch.setattr(
        "app.services.instagram.parse_instagram_url",
        lambda value: ("unknown", None, None),
    )
    monkeypatch.setattr(
        messages, "parse_instagram_url", lambda value: ("unknown", None, None)
    )

    def update_for(chat_id: int):
        status = SimpleNamespace(
            message_id=100,
            delete=AsyncMock(),
            edit_text=AsyncMock(),
        )
        message = MagicMock()
        message.text = url
        message.reply_text = AsyncMock(return_value=status)
        message.delete = AsyncMock()
        return SimpleNamespace(
            effective_user=SimpleNamespace(
                id=7,
                username="tester",
                mention_html=lambda: "tester",
            ),
            effective_chat=SimpleNamespace(id=chat_id),
            message=message,
        )

    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
    await messages.on_message(update_for(9), context)
    await group_logic.handle_group_message(update_for(10), context)

    assert pipeline.cache_checks == 2
    assert pipeline.deliveries == 2


@pytest.mark.asyncio
async def test_private_pipeline_slideshow_photo_callback_delivers_via_pipeline(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import callbacks

    url = "https://www.tiktok.com/@tester/photo/123"
    token = "pipeline-slide"

    class Pipeline:
        def __init__(self) -> None:
            self.calls = []

        async def deliver(self, request, target, **kwargs):
            self.calls.append((request, target, kwargs))
            return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)

    class Queue:
        released = False

        async def enqueue(self, update_ui, **kwargs):
            return True

        def release(self):
            self.released = True

    payload = DownloadContext(
        page_url=url,
        api_source="pipeline",
        api_json={"media_id": "123", "item_count": 2},
        section="10-20",
    )
    pipeline = Pipeline()
    queue = Queue()
    monkeypatch.setattr(callbacks.state, "media_pipeline", pipeline)
    monkeypatch.setattr(
        callbacks.state,
        "link_cache",
        SimpleNamespace(get=AsyncMock(return_value=payload)),
    )
    monkeypatch.setattr(
        callbacks.state,
        "limiter",
        SimpleNamespace(
            allow_user=AsyncMock(return_value=True),
            allow_chat=AsyncMock(return_value=True),
        ),
    )
    monkeypatch.setattr(callbacks.state, "download_queue", queue)
    legacy_download = AsyncMock(
        side_effect=AssertionError("pipeline slideshow must not use legacy download")
    )
    monkeypatch.setattr(callbacks.MediaSender, "download_slideshow", legacy_download)

    query = MagicMock()
    query.data = encode_callback_data("apislide", token, "photo")
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.delete_message = AsyncMock()
    query.from_user = SimpleNamespace(id=7)
    query.message = MagicMock(spec=Message)
    query.message.chat_id = 9
    update = SimpleNamespace(callback_query=query)

    await callbacks.on_slideshow(update, SimpleNamespace(bot=MagicMock()))

    assert len(pipeline.calls) == 1
    request, target, _ = pipeline.calls[0]
    assert request.kind is MediaKind.AUTO
    assert request.clip == ClipInterval()
    assert request.caller_scope == target.caller_scope == "callback"
    query.delete_message.assert_awaited_once()
    legacy_download.assert_not_awaited()
    assert queue.released


@pytest.mark.asyncio
async def test_group_pipeline_slideshow_video_is_delivered_by_pipeline(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.bot import group_logic

    url = "https://www.tiktok.com/@tester/photo/123"
    token = "pipeline-group-slide"

    class Pipeline:
        def __init__(self) -> None:
            self.calls = []

        async def deliver_slideshow_video(self, request, target, **kwargs):
            self.calls.append((request, target, kwargs))
            return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)

    class Queue:
        released = False

        async def enqueue(self, update_ui, **kwargs):
            return True

        def release(self):
            self.released = True

    payload = DownloadContext(
        page_url=url,
        user_tag="@tester",
        chat_id=9,
        original_msg_id=10,
        api_source="pipeline",
        api_json={"media_id": "123", "item_count": 2},
        section="10-20",
    )
    pipeline = Pipeline()
    queue = Queue()
    monkeypatch.setattr(group_logic.state, "media_pipeline", pipeline)
    monkeypatch.setattr(
        group_logic.state,
        "link_cache",
        SimpleNamespace(get=AsyncMock(return_value=payload)),
    )
    monkeypatch.setattr(group_logic.state, "download_queue", queue)
    legacy_download = AsyncMock(
        side_effect=AssertionError("pipeline slideshow must not use legacy download")
    )
    monkeypatch.setattr(group_logic.MediaSender, "download_slideshow", legacy_download)

    query = MagicMock()
    query.data = encode_callback_data("apigrpslide", token, "video")
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.delete_message = AsyncMock()
    query.message = MagicMock()
    context = SimpleNamespace(
        bot=SimpleNamespace(
            send_chat_action=AsyncMock(),
            delete_message=AsyncMock(),
        )
    )

    await group_logic.on_group_slideshow(SimpleNamespace(callback_query=query), context)

    assert len(pipeline.calls) == 1
    request, target, options = pipeline.calls[0]
    assert request.kind is MediaKind.AUTO
    assert request.caller_scope == "group"
    assert target.destination == "9"
    assert options["caption"] == "👤 @tester"
    legacy_download.assert_not_awaited()
    assert queue.released
