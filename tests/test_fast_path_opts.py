# mypy: ignore-errors
"""Unit tests for fast-path pipeline optimizations (OPT-1 to OPT-4)."""

import asyncio
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestFindThumbnail(unittest.TestCase):
    """Tests for converter.find_thumbnail() – OPT-1."""

    def setUp(self):
        from app.services.converter import find_thumbnail
        self.find_thumbnail = find_thumbnail

    def test_returns_jpg_if_exists(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "test.mp4")
            thumb = os.path.join(d, "test.jpg")
            open(thumb, "wb").write(b"\xff\xd8\xff fake jpeg")
            result = self.find_thumbnail(video)
            self.assertEqual(result, thumb)

    def test_returns_none_if_no_thumbnail(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "test.mp4")
            result = self.find_thumbnail(video)
            self.assertIsNone(result)

    def test_returns_none_for_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "test.mp4")
            thumb = os.path.join(d, "test.jpg")
            open(thumb, "wb").write(b"")  # empty
            result = self.find_thumbnail(video)
            self.assertIsNone(result)

    def test_returns_none_for_none_input(self):
        result = self.find_thumbnail(None)
        self.assertIsNone(result)

    def test_returns_none_for_empty_string(self):
        result = self.find_thumbnail("")
        self.assertIsNone(result)

    def test_fallback_to_jpeg_extension(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "test.mp4")
            thumb = os.path.join(d, "test.jpeg")
            open(thumb, "wb").write(b"\xff\xd8\xff")
            result = self.find_thumbnail(video)
            self.assertEqual(result, thumb)

    def test_fallback_to_webp(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "test.mp4")
            thumb = os.path.join(d, "test.webp")
            open(thumb, "wb").write(b"RIFF fake webp")
            result = self.find_thumbnail(video)
            self.assertEqual(result, thumb)

    def test_jpg_preferred_over_webp(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "test.mp4")
            jpg = os.path.join(d, "test.jpg")
            webp = os.path.join(d, "test.webp")
            open(jpg, "wb").write(b"\xff\xd8\xff")
            open(webp, "wb").write(b"RIFF fake")
            result = self.find_thumbnail(video)
            self.assertEqual(result, jpg)


class TestNativeAudioBypass(unittest.TestCase):
    """Tests for orchestrator Opus bypass helpers – OPT-3."""

    def test_is_native_audio_mp3(self):
        from app.services.orchestrator import _is_native_audio_container
        self.assertTrue(_is_native_audio_container("/tmp/audio.mp3"))

    def test_is_native_audio_m4a(self):
        from app.services.orchestrator import _is_native_audio_container
        self.assertTrue(_is_native_audio_container("/tmp/audio.m4a"))

    def test_is_native_audio_ogg(self):
        from app.services.orchestrator import _is_native_audio_container
        self.assertTrue(_is_native_audio_container("/tmp/audio.ogg"))

    def test_is_native_audio_opus(self):
        from app.services.orchestrator import _is_native_audio_container
        self.assertTrue(_is_native_audio_container("/tmp/audio.opus"))

    def test_is_not_native_audio_webm(self):
        from app.services.orchestrator import _is_native_audio_container
        self.assertFalse(_is_native_audio_container("/tmp/audio.webm"))

    def test_is_not_native_audio_mp4(self):
        from app.services.orchestrator import _is_native_audio_container
        self.assertFalse(_is_native_audio_container("/tmp/video.mp4"))

    def test_detect_opus_from_webm_positive(self):
        from app.services.orchestrator import _detect_opus_from_webm
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            # OpusHead signature within first 512 bytes
            f.write(b"\x1a\x45\xdf\xa3" + b"\x00" * 100 + b"OpusHead" + b"\x00" * 50)
            fname = f.name
        try:
            self.assertTrue(_detect_opus_from_webm(fname))
        finally:
            os.unlink(fname)

    def test_detect_opus_from_webm_negative(self):
        from app.services.orchestrator import _detect_opus_from_webm
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(b"\x1a\x45\xdf\xa3" + b"\x00" * 200)
            fname = f.name
        try:
            self.assertFalse(_detect_opus_from_webm(fname))
        finally:
            os.unlink(fname)

    def test_detect_opus_from_webm_missing_file(self):
        from app.services.orchestrator import _detect_opus_from_webm
        self.assertFalse(_detect_opus_from_webm("/nonexistent/path.webm"))

    def test_maybe_rename_non_webm_unchanged(self):
        from app.services.orchestrator import _maybe_rename_webm_to_ogg
        result = asyncio.run(_maybe_rename_webm_to_ogg("/tmp/audio.ogg"))
        self.assertEqual(result, "/tmp/audio.ogg")

    def test_maybe_rename_webm_with_opus_header(self):
        from app.services.orchestrator import _maybe_rename_webm_to_ogg
        with tempfile.TemporaryDirectory() as d:
            webm_path = os.path.join(d, "audio.webm")
            with open(webm_path, "wb") as f:
                f.write(b"\x1a\x45\xdf\xa3" + b"\x00" * 50 + b"OpusHead" + b"\x00" * 50)

            result = asyncio.run(_maybe_rename_webm_to_ogg(webm_path))
            expected = os.path.join(d, "audio.ogg")
            self.assertEqual(result, expected)
            self.assertTrue(os.path.exists(expected))

    def test_maybe_rename_webm_no_opus_unchanged(self):
        from app.services.orchestrator import _maybe_rename_webm_to_ogg
        with tempfile.TemporaryDirectory() as d:
            webm_path = os.path.join(d, "video.webm")
            with open(webm_path, "wb") as f:
                f.write(b"\x1a\x45\xdf\xa3" + b"\x00" * 200)

            result = asyncio.run(_maybe_rename_webm_to_ogg(webm_path))
            self.assertEqual(result, webm_path)  # unchanged


class TestCobaltUrlHeadSize(unittest.IsolatedAsyncioTestCase):
    """Tests for _cobalt_url_head_size – OPT-4."""

    async def test_returns_content_length_on_success(self):
        from app.services.orchestrator import _cobalt_url_head_size

        mock_response = MagicMock()
        mock_response.headers = {"content-length": "10485760"}  # 10 MB

        mock_session = AsyncMock()
        mock_session.head = AsyncMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("curl_cffi.requests.AsyncSession", return_value=mock_session):
            result = await _cobalt_url_head_size("https://cdn.example.com/video.mp4")

        self.assertEqual(result, 10485760)

    async def test_returns_none_on_missing_header(self):
        from app.services.orchestrator import _cobalt_url_head_size

        mock_response = MagicMock()
        mock_response.headers = {}

        mock_session = AsyncMock()
        mock_session.head = AsyncMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("curl_cffi.requests.AsyncSession", return_value=mock_session):
            result = await _cobalt_url_head_size("https://cdn.example.com/video.mp4")

        self.assertIsNone(result)

    async def test_returns_none_on_exception(self):
        from app.services.orchestrator import _cobalt_url_head_size

        with patch(
            "curl_cffi.requests.AsyncSession",
            side_effect=Exception("Network error"),
        ):
            result = await _cobalt_url_head_size("https://cdn.example.com/video.mp4")

        self.assertIsNone(result)


class TestOpenMediaPassthrough(unittest.TestCase):
    """Tests for sender._open_media URL pass-through – OPT-4."""

    def test_https_url_passes_through(self):
        from app.services.sender import _open_media
        url = "https://cdn.cobalt.tools/video.mp4"
        with _open_media(url) as result:
            self.assertEqual(result, url)

    def test_http_url_passes_through(self):
        from app.services.sender import _open_media
        url = "http://example.com/video.mp4"
        with _open_media(url) as result:
            self.assertEqual(result, url)

    def test_bytesio_passes_through(self):
        from app.services.sender import _open_media
        buf = io.BytesIO(b"video data")
        with _open_media(buf) as result:
            self.assertIs(result, buf)

    def test_file_path_opens_binary(self):
        from app.services.sender import _open_media
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
            f.write(b"fake video")
            fname = f.name
        try:
            with _open_media(fname) as result:
                self.assertEqual(result.read(), b"fake video")
        finally:
            os.unlink(fname)


class TestSplitVideoStreamCopy(unittest.IsolatedAsyncioTestCase):
    """Tests for converter.split_video_stream_copy – OPT-2."""

    async def test_returns_none_for_nonexistent_file(self):
        from app.services.converter import split_video_stream_copy
        result = await split_video_stream_copy("/nonexistent/video.mp4")
        self.assertIsNone(result)

    async def test_returns_none_for_small_file(self):
        from app.services.converter import split_video_stream_copy
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"x" * 100)
            fname = f.name
        try:
            result = await split_video_stream_copy(fname, segment_bytes=200)
            self.assertIsNone(result)
        finally:
            os.unlink(fname)

    async def test_returns_none_for_too_large_file(self):
        from app.services.converter import split_video_stream_copy, _SPLIT_MAX_INPUT_BYTES

        with patch("os.path.getsize", return_value=_SPLIT_MAX_INPUT_BYTES + 1):
            with patch("os.path.exists", return_value=True):
                result = await split_video_stream_copy("/fake/huge.mp4")
        self.assertIsNone(result)

    async def test_returns_none_when_probe_fails(self):
        from app.services.converter import split_video_stream_copy

        with patch("os.path.exists", return_value=True):
            with patch("os.path.getsize", return_value=100 * 1024 * 1024):
                with patch(
                    "app.services.converter._probe_full_meta",
                    new_callable=AsyncMock,
                    return_value={"duration_s": None},
                ):
                    result = await split_video_stream_copy("/fake/video.mp4")
        self.assertIsNone(result)

    async def test_returns_none_on_ffmpeg_failure(self):
        from app.services.converter import split_video_stream_copy

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"ffmpeg error"))

        with patch("os.path.exists", return_value=True):
            with patch("os.path.getsize", return_value=100 * 1024 * 1024):
                with patch(
                    "app.services.converter._probe_full_meta",
                    new_callable=AsyncMock,
                    return_value={"duration_s": 300.0},
                ):
                    with patch(
                        "asyncio.create_subprocess_exec",
                        return_value=mock_proc,
                    ):
                        result = await split_video_stream_copy("/fake/video.mp4")
        self.assertIsNone(result)


class TestYtDlpBuilderThumbnail(unittest.TestCase):
    """Verify --write-thumbnail is included for non-pipe, non-audio, non-gif downloads."""

    def test_thumbnail_flags_added_for_video(self):
        from app.services.ytdlp.builders import YtDlpCLIBuilder
        builder = YtDlpCLIBuilder()
        cmd = builder.build_download_cmd(
            url="https://youtube.com/watch?v=test",
            format_id="137+140",
            output_path="/tmp/test.mp4",
            pipe_mode=False,
        )
        cmd_str = " ".join(cmd)
        self.assertIn("--write-thumbnail", cmd_str)
        self.assertIn("--convert-thumbnails", cmd_str)
        self.assertIn("jpg", cmd_str)

    def test_thumbnail_flags_absent_in_pipe_mode(self):
        from app.services.ytdlp.builders import YtDlpCLIBuilder
        builder = YtDlpCLIBuilder()
        cmd = builder.build_download_cmd(
            url="https://youtube.com/watch?v=test",
            format_id="137+140",
            output_path="-",
            pipe_mode=True,
        )
        self.assertNotIn("--write-thumbnail", cmd)

    def test_thumbnail_flags_absent_for_audio(self):
        from app.services.ytdlp.builders import YtDlpCLIBuilder
        builder = YtDlpCLIBuilder()
        cmd = builder.build_download_cmd(
            url="https://youtube.com/watch?v=test",
            format_id="audio",
            output_path="/tmp/audio.m4a",
            pipe_mode=False,
        )
        self.assertNotIn("--write-thumbnail", cmd)

    def test_thumbnail_flags_absent_for_gif(self):
        from app.constants import GIF_FORMAT_ID
        from app.services.ytdlp.builders import YtDlpCLIBuilder
        builder = YtDlpCLIBuilder()
        cmd = builder.build_download_cmd(
            url="https://youtube.com/watch?v=test",
            format_id=GIF_FORMAT_ID,
            output_path="/tmp/anim.gif",
            pipe_mode=False,
        )
        self.assertNotIn("--write-thumbnail", cmd)


class TestExtractVideoMeta(unittest.IsolatedAsyncioTestCase):
    """Tests for orchestrator._extract_video_meta – ffprobe integration."""

    async def test_returns_empty_dict_for_missing_file(self):
        from app.services.orchestrator import _extract_video_meta
        result = await _extract_video_meta("/nonexistent/file.mp4")
        self.assertIsInstance(result, dict)

    async def test_parses_ffprobe_output(self):
        from app.services.orchestrator import _extract_video_meta
        import json

        fake_probe = {
            "format": {"duration": "42.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "codec_tag_string": "avc1",
                }
            ],
        }

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(json.dumps(fake_probe).encode(), b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _extract_video_meta("/fake/video.mp4")

        self.assertEqual(result.get("duration"), 42)
        self.assertEqual(result.get("width"), 1920)
        self.assertEqual(result.get("height"), 1080)
        self.assertEqual(result.get("vcodec"), "h264")
        self.assertEqual(result.get("pix_fmt"), "yuv420p")
        self.assertEqual(result.get("codec_tag"), "avc1")

    async def test_returns_meta_from_info_json_if_present(self):
        from app.services.orchestrator import _extract_video_meta
        import json
        import tempfile

        info = {"duration": 120, "width": 1280, "height": 720, "vcodec": "h264"}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(info, f)
            json_path = f.name

        try:
            result = await _extract_video_meta("/fake/video.mp4", info_json_path=json_path)
            self.assertEqual(result.get("duration"), 120)
            self.assertEqual(result.get("vcodec"), "h264")
        finally:
            os.unlink(json_path)


class TestEnsureTelegramCompatible(unittest.IsolatedAsyncioTestCase):
    """Tests for orchestrator._ensure_telegram_compatible."""

    async def test_h264_passes_through(self):
        from app.services.orchestrator import _ensure_telegram_compatible
        import json

        fake_probe = {
            "format": {"duration": "30.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 1280,
                    "height": 720,
                    "codec_tag_string": "avc1",
                }
            ],
        }
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(json.dumps(fake_probe).encode(), b"")
        )
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _ensure_telegram_compatible("/fake/h264.mp4")
        self.assertEqual(result, "/fake/h264.mp4")

    async def test_hevc_triggers_reencode_and_falls_back_on_failure(self):
        from app.services.orchestrator import _ensure_telegram_compatible
        import json

        fake_probe = {
            "format": {"duration": "15.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "pix_fmt": "yuv420p",
                    "width": 1280,
                    "height": 720,
                }
            ],
        }
        probe_proc = AsyncMock()
        probe_proc.returncode = 0
        probe_proc.communicate = AsyncMock(
            return_value=(json.dumps(fake_probe).encode(), b"")
        )
        ffmpeg_proc = AsyncMock()
        ffmpeg_proc.returncode = 1  # simulate failure
        ffmpeg_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        call_count = 0

        async def fake_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return probe_proc  # ffprobe
            return ffmpeg_proc  # ffmpeg re-encode

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _ensure_telegram_compatible("/fake/hevc.mp4")

        # On re-encode failure should fall back to original path
        self.assertEqual(result, "/fake/hevc.mp4")


class TestBuildGifReplyMarkup(unittest.TestCase):
    """Tests for orchestrator._build_gif_reply_markup."""

    def test_returns_none_for_non_gif(self):
        from app.services.orchestrator import _build_gif_reply_markup
        result = _build_gif_reply_markup("tok123", is_gif=False)
        self.assertIsNone(result)

    def test_returns_markup_for_gif(self):
        from app.services.orchestrator import _build_gif_reply_markup
        with patch("app.bot.keyboards.build_sent_gif_keyboard") as mock_kb:
            mock_kb.return_value = MagicMock()
            result = _build_gif_reply_markup("tok123", is_gif=True)
        mock_kb.assert_called_once_with("tok123")
        self.assertIsNotNone(result)


class TestProbeFullMeta(unittest.IsolatedAsyncioTestCase):
    """Tests for converter._probe_full_meta (used by split_video_stream_copy)."""

    async def test_returns_none_duration_on_bad_output(self):
        from app.services.converter import _probe_full_meta
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _probe_full_meta("/fake/video.mp4")
        self.assertIsNone(result["duration_s"])

    async def test_parses_duration_and_audio_bitrate(self):
        from app.services.converter import _probe_full_meta
        import json

        fake = {
            "format": {"duration": "120.5"},
            "streams": [
                {"codec_type": "audio", "bit_rate": "192000"},
            ],
        }
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(json.dumps(fake).encode(), b""))
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _probe_full_meta("/fake/video.mp4")
        self.assertAlmostEqual(result["duration_s"], 120.5, places=1)
        self.assertEqual(result["audio_kbps"], 192)

    async def test_returns_defaults_on_exception(self):
        from app.services.converter import _probe_full_meta
        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("no ffprobe")
        ):
            result = await _probe_full_meta("/fake/video.mp4")
        self.assertIsNone(result["duration_s"])
        self.assertIsNone(result["audio_kbps"])


class TestSplitVideoCompletePath(unittest.IsolatedAsyncioTestCase):
    """Test happy path for split_video_stream_copy with produced segments."""

    async def test_returns_sorted_parts_on_success(self):
        from app.services.converter import split_video_stream_copy

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        fake_parts = ["/tmp/ytdlbot/split_abc123_000.mp4", "/tmp/ytdlbot/split_abc123_001.mp4"]

        with patch("os.path.exists", return_value=True):
            with patch("os.path.getsize", return_value=100 * 1024 * 1024):
                with patch(
                    "app.services.converter._probe_full_meta",
                    new_callable=AsyncMock,
                    return_value={"duration_s": 300.0, "audio_kbps": 128},
                ):
                    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                        with patch("glob.glob", return_value=fake_parts):
                            result = await split_video_stream_copy("/fake/video.mp4")

        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
