from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, patch

import pytest

from app.services import tikwm
from app.services.tikwm import TikWMResult, TikWMService, _stream_response


class StreamingResponse:
    def __init__(
        self,
        session: StreamingSession,
        chunks: tuple[bytes, ...],
        *,
        status_code: int = 200,
        declared: int | None = None,
        delay: float = 0,
    ) -> None:
        self.session = session
        self.chunks = chunks
        self.status_code = status_code
        self.delay = delay
        self.headers = {"content-type": "video/mp4"}
        if declared is not None:
            self.headers["content-length"] = str(declared)

    async def aiter_content(self, chunk_size: int):
        del chunk_size
        assert self.session.active, "response consumed after AsyncSession closed"
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk


class StreamingSession:
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        status_code: int = 200,
        declared: int | None = None,
        delay: float = 0,
    ) -> None:
        self.active = False
        self.response = StreamingResponse(
            self, chunks, status_code=status_code, declared=declared, delay=delay
        )

    async def __aenter__(self) -> Self:
        self.active = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.active = False

    async def get(self, *args: object, **kwargs: object) -> StreamingResponse:
        return self.response


class RaisingSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, *args: object, **kwargs: object) -> StreamingResponse:
        del args, kwargs
        raise OSError("request failed for https://cdn.example/a.mp3?token=secret")


@pytest.mark.asyncio
async def test_video_stream_is_consumed_inside_session_and_matches_content_length(
    tmp_path: Path,
) -> None:
    body = b"\x00\x00\x00\x18ftyp" + b"x" * 11_000
    session = StreamingSession(
        (body[:6000], body[6000:]), declared=len(body), delay=0.01
    )
    with (
        patch("app.services.tikwm.TEMP_DIR", str(tmp_path)),
        patch("app.services.tikwm.AsyncSession", return_value=session),
    ):
        path, error = await TikWMService.download_video(
            "https://page.example/video/1",
            direct_video_url="https://cdn.example/video.mp4?token=secret",
            _cdn_retry=False,
        )

    assert error is None
    assert path is not None and Path(path).read_bytes() == body


@pytest.mark.asyncio
async def test_truncated_video_content_length_is_rejected_and_removed(
    tmp_path: Path,
) -> None:
    body = b"\x00\x00\x00\x18ftyp" + b"x" * 11_000
    session = StreamingSession((body,), declared=len(body) + 1)
    with (
        patch("app.services.tikwm.TEMP_DIR", str(tmp_path)),
        patch("app.services.tikwm.AsyncSession", return_value=session),
    ):
        path, error = await TikWMService.download_video(
            "https://page.example/video/1",
            direct_video_url="https://cdn.example/video.mp4?token=secret",
            _cdn_retry=False,
        )

    assert path is None
    assert error is not None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["http", "short", "truncated"])
@pytest.mark.parametrize("short_cached", [True, False])
@pytest.mark.parametrize("final_status", [200, 429])
async def test_direct_video_refreshes_rejected_metadata_aliases(
    tmp_path, monkeypatch, failure, short_cached, final_status
):
    short = "https://vm.tiktok.com/alias/"
    canonical = "https://www.tiktok.com/@user/video/123"
    rejected = "https://cdn.example/old.mp4"
    fresh_url = "https://cdn.example/new.mp4"
    unrelated = "https://www.tiktok.com/@user/video/456"
    old = TikWMResult(status="video", url=rejected, canonical_url=canonical)
    unrelated_result = TikWMResult(
        status="video", url=rejected, canonical_url=unrelated
    )
    expiry = time.time() + 120
    cache = {canonical: (old, expiry), unrelated: (unrelated_result, expiry)}
    if short_cached:
        cache[short] = cache[canonical]
    monkeypatch.setattr(tikwm, "_tikwm_cache", cache)
    monkeypatch.setattr(tikwm, "_last_request_time", 0)
    monkeypatch.setattr(tikwm, "TEMP_DIR", str(tmp_path))

    rejected_session = StreamingSession(
        (b"small",),
        status_code=403 if failure == "http" else 200,
        declared=11_000 if failure == "truncated" else None,
    )
    body = b"new-video" * 2_000
    fresh_session = StreamingSession(
        (body,), declared=len(body), status_code=final_status
    )
    metadata_session = StreamingSession(())
    metadata_session.head = AsyncMock(return_value=SimpleNamespace(url=canonical))
    requests = []

    async def resolve(url, **_kwargs):
        requests.append(url)
        assert not rejected_session.active
        assert not list(tmp_path.iterdir())
        assert short not in cache and canonical not in cache
        return SimpleNamespace(json=lambda: {"code": 0, "data": {"hdplay": fresh_url}})

    metadata_session.get = AsyncMock(side_effect=resolve)
    rejected_session.get = AsyncMock(return_value=rejected_session.response)
    fresh_session.get = AsyncMock(return_value=fresh_session.response)
    sessions = iter(
        [rejected_session, metadata_session, metadata_session, fresh_session]
    )
    monkeypatch.setattr(tikwm, "AsyncSession", lambda: next(sessions))

    path, error = await TikWMService.download_video(short, direct_video_url=rejected)

    assert len(requests) == 1 and requests[0].startswith(tikwm.API_BASE)
    assert rejected_session.get.await_args.args == (rejected,)
    assert fresh_session.get.await_args.args == (fresh_url,)
    assert cache[unrelated][0] is unrelated_result
    if final_status == 200:
        assert error is None
        assert path is not None and Path(path).read_bytes() == body
        assert cache[short][0] is cache[canonical][0]
        assert cache[canonical][0].url == fresh_url
        assert list(tmp_path.iterdir()) == [Path(path)]
    else:
        assert path is None and error == "TikWM CDN error: HTTP 429"
        assert short not in cache and canonical not in cache
        assert not list(tmp_path.iterdir())


async def test_rejected_direct_url_preserves_metadata_refreshed_during_transfer(
    tmp_path, monkeypatch
):
    short = "https://vm.tiktok.com/alias/"
    canonical = "https://www.tiktok.com/@user/video/123"
    rejected = "https://cdn.example/old.mp4"
    current = "https://cdn.example/new.mp4"
    old = TikWMResult(status="video", url=rejected, canonical_url=canonical)
    fresh = TikWMResult(status="video", url=current, canonical_url=canonical)
    expiry = time.time() + 120
    cache = {short: (old, expiry), canonical: (old, expiry)}
    monkeypatch.setattr(tikwm, "_tikwm_cache", cache)
    monkeypatch.setattr(tikwm, "TEMP_DIR", str(tmp_path))

    rejected_session = StreamingSession((), status_code=404)
    metadata_session = StreamingSession(())
    metadata_session.head = AsyncMock(return_value=SimpleNamespace(url=canonical))
    good_body = b"current-video" * 1_000
    fresh_session = StreamingSession((good_body,))

    async def replace_cache(*_args, **_kwargs):
        cache[short] = cache[canonical] = (fresh, expiry)
        return rejected_session.response

    rejected_session.get = AsyncMock(side_effect=replace_cache)
    sessions = iter([rejected_session, metadata_session, fresh_session])
    monkeypatch.setattr(tikwm, "AsyncSession", lambda: next(sessions))
    metadata_session.get = AsyncMock(
        side_effect=AssertionError("replacement metadata is already cached")
    )
    fresh_session.get = AsyncMock(return_value=fresh_session.response)

    path, error = await TikWMService.download_video(short, direct_video_url=rejected)

    assert error is None
    assert path is not None and Path(path).read_bytes() == good_body
    metadata_session.get.assert_not_awaited()
    assert cache == {short: (fresh, expiry), canonical: (fresh, expiry)}


@pytest.mark.asyncio
async def test_stream_helper_rejects_truncated_declared_length(tmp_path: Path) -> None:
    session = StreamingSession((b"short",), declared=10)
    async with session:
        with pytest.raises(ValueError, match="truncated"):
            await _stream_response(session.response, str(tmp_path / "partial.mp4"))


@pytest.mark.asyncio
async def test_slideshow_stream_is_consumed_inside_session_and_truncation_skips_item(
    tmp_path: Path,
) -> None:
    body = b"\xff\xd8\xff" + b"x" * 200
    session = StreamingSession((body,), declared=len(body) + 1)
    result = TikWMResult(status="picker", images=["https://cdn.example/a.jpg"])
    with (
        patch("app.services.tikwm.TEMP_DIR", str(tmp_path)),
        patch("app.services.tikwm.AsyncSession", return_value=session),
    ):
        images, audio = await TikWMService.download_slideshow(result)

    assert images == []
    assert audio is None
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_signed_cdn_query_is_redacted_from_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = StreamingSession((), status_code=500)
    with (
        patch("app.services.tikwm.TEMP_DIR", str(tmp_path)),
        patch("app.services.tikwm.AsyncSession", return_value=session),
        caplog.at_level(logging.ERROR, logger="app.services.tikwm"),
    ):
        await TikWMService.download_video(
            "https://page.example/video/1",
            direct_video_url="https://cdn.example/video.mp4?token=secret",
            _cdn_retry=False,
        )

    assert "token=" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_signed_audio_url_is_not_leaked_through_exception_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("app.services.tikwm.TEMP_DIR", str(tmp_path)),
        patch("app.services.tikwm.AsyncSession", return_value=RaisingSession()),
        caplog.at_level(logging.ERROR, logger="app.services.tikwm"),
    ):
        path = await TikWMService.download_audio(
            "https://cdn.example/a.mp3?token=secret"
        )

    assert path is None
    assert "token=" not in caplog.text
    assert "secret" not in caplog.text
