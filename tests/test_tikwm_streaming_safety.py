from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest

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
