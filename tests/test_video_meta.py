from unittest.mock import AsyncMock

"""Tests for extract_video_meta helper in callbacks.py."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.core.process import ProcessResult
from app.services.orchestrator import extract_video_meta


class TestExtractVideoMeta(unittest.IsolatedAsyncioTestCase):
    """Tests for extract_video_meta helper."""

    async def test_from_info_json_full(self):
        """Cached display metadata remains available for an in-memory download."""
        info = {"duration": 125.5, "width": 1920, "height": 1080}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(info, f)
            path = f.name

        try:
            meta = await extract_video_meta(object(), info_json_path=path)
            self.assertEqual(meta["duration"], 125)
            self.assertEqual(meta["width"], 1920)
            self.assertEqual(meta["height"], 1080)
        finally:
            os.unlink(path)

    async def test_from_info_json_partial_no_duration(self):
        """If duration is missing from info JSON, falls through to ffprobe."""
        info = {"width": 1280, "height": 720}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(info, f)
            path = f.name

        try:
            # Mock ffprobe to also return empty (no ffprobe available in test)
            with patch(
                "app.services.orchestrator.process_supervisor.run",
                new_callable=AsyncMock,
                return_value=ProcessResult(0, b"", b""),
            ):
                meta = await extract_video_meta("/fake/video.mp4", info_json_path=path)
                # width/height from JSON should still be set
                self.assertEqual(meta["width"], 1280)
                self.assertEqual(meta["height"], 720)
                self.assertIsNone(meta["duration"])
        finally:
            os.unlink(path)

    async def test_no_info_json_uses_ffprobe(self):
        """No info JSON → falls back to ffprobe."""
        ffprobe_output = json.dumps(
            {
                "format": {"duration": "90.0"},
                "streams": [
                    {"codec_type": "video", "width": 854, "height": 480},
                    {"codec_type": "audio"},
                ],
            }
        )

        with patch(
            "app.services.orchestrator.process_supervisor.run",
            new_callable=AsyncMock,
            return_value=ProcessResult(0, ffprobe_output.encode(), b""),
        ):
            meta = await extract_video_meta("/fake/video.mp4")
            self.assertEqual(meta["duration"], 90)
            self.assertEqual(meta["width"], 854)
            self.assertEqual(meta["height"], 480)

    async def test_no_info_json_no_ffprobe(self):
        """Neither info JSON nor ffprobe available → returns all None."""
        with patch(
            "app.services.orchestrator.process_supervisor.run",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError,
        ):
            meta = await extract_video_meta("/fake/video.mp4")
            self.assertIsNone(meta["duration"])
            self.assertIsNone(meta["width"])
            self.assertIsNone(meta["height"])

    async def test_info_json_path_nonexistent(self):
        """info_json_path points to a missing file → proceeds to ffprobe."""
        with patch(
            "app.services.orchestrator.process_supervisor.run",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError,
        ):
            meta = await extract_video_meta(
                "/fake/video.mp4",
                info_json_path="/nonexistent/info.json",
            )
            self.assertIsNone(meta["duration"])

    async def test_info_json_corrupt_json(self):
        """Malformed JSON in info file → proceeds to ffprobe."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json{{{")
            path = f.name

        try:
            with patch(
                "app.services.orchestrator.process_supervisor.run",
                new_callable=AsyncMock,
                side_effect=FileNotFoundError,
            ):
                meta = await extract_video_meta("/fake/video.mp4", info_json_path=path)
                self.assertIsNone(meta["duration"])
        finally:
            os.unlink(path)

    async def test_ffprobe_timeout(self):
        """ffprobe exceeding timeout → returns gracefully."""
        with patch(
            "app.services.orchestrator.process_supervisor.run",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            meta = await extract_video_meta("/fake/video.mp4")
            self.assertIsNone(meta["duration"])

    async def test_duration_float_truncation(self):
        """Float duration like 123.99 should be truncated to 123."""
        info = {"duration": 123.99, "width": 640, "height": 360}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(info, f)
            path = f.name

        try:
            meta = await extract_video_meta(object(), info_json_path=path)
            self.assertEqual(meta["duration"], 123)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "cached",
    [
        {"duration": 100},
        {
            "duration": 100,
            "width": 1920,
            "height": 1080,
            "vcodec": "h264",
            "pix_fmt": "yuv420p",
        },
    ],
)
async def test_actual_media_metadata_overrides_extraction_cache(
    tmp_path, monkeypatch, cached
):
    from app.services import orchestrator

    video = tmp_path / "selected.mp4"
    video.write_bytes(b"selected-media")
    info = tmp_path / "info.json"
    info.write_text(json.dumps(cached))
    actual = {
        "format": {"duration": "12.8"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 640,
                "height": 360,
                "pix_fmt": "yuv420p10le",
                "codec_tag_string": "hvc1",
            }
        ],
    }
    probe = AsyncMock(return_value=ProcessResult(0, json.dumps(actual).encode(), b""))
    monkeypatch.setattr(orchestrator.process_supervisor, "run", probe)
    result = await orchestrator.extract_video_meta(str(video), str(info))
    assert result == {
        "duration": 12,
        "width": 640,
        "height": 360,
        "vcodec": "hevc",
        "pix_fmt": "yuv420p10le",
        "codec_tag": "hvc1",
    }
    assert probe.await_args.args[0][-1] == str(video)


@pytest.mark.parametrize(
    "probe_result", [ProcessResult(1, b"", b"failed"), FileNotFoundError("no ffprobe")]
)
async def test_probe_failure_preserves_useful_cache_metadata(
    tmp_path, monkeypatch, probe_result
):
    from app.services import orchestrator

    info = tmp_path / "info.json"
    info.write_text(
        json.dumps(
            {
                "duration": 15.8,
                "width": 640,
                "height": 360,
                "vcodec": "h264",
                "pix_fmt": "yuv420p10le",
            }
        )
    )
    probe = (
        AsyncMock(side_effect=probe_result)
        if isinstance(probe_result, Exception)
        else AsyncMock(return_value=probe_result)
    )
    monkeypatch.setattr(orchestrator.process_supervisor, "run", probe)
    result = await orchestrator.extract_video_meta(
        str(tmp_path / "video.mp4"), str(info)
    )
    assert result["duration"] == 15
    assert result["vcodec"] == "h264"
    assert result["pix_fmt"] == "yuv420p10le"


@pytest.mark.parametrize(
    "format_id", ["22", "tikwm_fallback", "gallerydl_fallback", "pinterest_native"]
)
async def test_legacy_clip_reaches_downloader_and_delivers_clip_bytes(
    tmp_path, monkeypatch, format_id
):
    import sys

    from app.core import config, state
    from app.core.models import DownloadContext
    from app.core.storage.memory import MemoryStorage
    from app.services import downloader, orchestrator

    class Queue:
        async def enqueue(self, *args, **kwargs):
            return True

        def release(self):
            pass

    observed = {}

    def build_command(url, selected_format, height, *, output, section=None, **kwargs):
        observed["section"] = section
        data = b"requested-clip" if section == "*10-20" else b"entire-video"
        return [
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))",
            output,
            data.hex(),
        ]

    async def send_video(**kwargs):
        content = kwargs["video"].input_file_content
        observed["delivered"] = content.read() if hasattr(content, "read") else content
        return SimpleNamespace(
            message_id=12,
            video=SimpleNamespace(file_id="clip", file_unique_id="clip-id"),
        )

    # Native APIs have no section argument. They must never deliver a whole file for a clip.
    async def native_whole_video(*args, **kwargs):
        path = tmp_path / "whole.mp4"
        path.write_bytes(b"entire-video")
        return str(path), None

    monkeypatch.setattr(state, "media_pipeline", None)
    monkeypatch.setattr(state, "download_queue", Queue())
    monkeypatch.setattr(state, "api_queue", Queue())
    monkeypatch.setattr(state, "file_cache", {})
    monkeypatch.setattr(state, "cancel_cache", MemoryStorage(maxsize=100, ttl=60))
    monkeypatch.setattr(
        state,
        "ytdlp",
        SimpleNamespace(
            build_command=build_command, cookies_path=None, tiktok_proxy=None
        ),
    )
    monkeypatch.setattr(downloader, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "YOUTUBE_PIPE_MODE", False)
    monkeypatch.setattr(config, "TELEGRAM_LOCAL_ENDPOINT", "")
    actual_meta = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}]
    }
    monkeypatch.setattr(
        orchestrator.process_supervisor,
        "run",
        AsyncMock(return_value=ProcessResult(0, json.dumps(actual_meta).encode(), b"")),
    )
    monkeypatch.setattr(orchestrator.TikWMService, "download_video", native_whole_video)
    monkeypatch.setattr(
        orchestrator.GalleryDlService, "download_video", native_whole_video
    )
    monkeypatch.setattr(
        orchestrator.PinterestNativeService, "download_video", native_whole_video
    )
    bot = SimpleNamespace(send_chat_action=AsyncMock(), send_video=send_video)
    result = await orchestrator.DownloadOrchestrator.process_download(
        "clip-token",
        123,
        bot,
        DownloadContext(
            page_url="https://youtu.be/example",
            format_id=format_id,
            height=720,
            section="*10-20",
        ),
        None,
        AsyncMock(),
        None,
    )
    assert result is True
    assert observed.get("section") == "*10-20"
    assert observed["delivered"] == b"requested-clip"
