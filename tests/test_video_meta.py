"""Tests for _extract_video_meta helper in callbacks.py."""
import json
import os
import sys
import asyncio
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")

from app.bot.callbacks import _extract_video_meta


class TestExtractVideoMeta(unittest.IsolatedAsyncioTestCase):
    """Tests for _extract_video_meta helper."""

    async def test_from_info_json_full(self):
        """All fields present in info JSON → returns without ffprobe."""
        info = {"duration": 125.5, "width": 1920, "height": 1080}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(info, f)
            path = f.name

        try:
            meta = await _extract_video_meta("/fake/video.mp4", info_json_path=path)
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
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
                proc_mock = AsyncMock()
                proc_mock.communicate.return_value = (b"", b"")
                mock_proc.return_value = proc_mock

                meta = await _extract_video_meta("/fake/video.mp4", info_json_path=path)
                # width/height from JSON should still be set
                self.assertEqual(meta["width"], 1280)
                self.assertEqual(meta["height"], 720)
                self.assertIsNone(meta["duration"])
        finally:
            os.unlink(path)

    async def test_no_info_json_uses_ffprobe(self):
        """No info JSON → falls back to ffprobe."""
        ffprobe_output = json.dumps({
            "format": {"duration": "90.0"},
            "streams": [
                {"codec_type": "video", "width": 854, "height": 480},
                {"codec_type": "audio"},
            ]
        })

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
            proc_mock = AsyncMock()
            proc_mock.communicate.return_value = (ffprobe_output.encode(), b"")
            mock_proc.return_value = proc_mock

            meta = await _extract_video_meta("/fake/video.mp4")
            self.assertEqual(meta["duration"], 90)
            self.assertEqual(meta["width"], 854)
            self.assertEqual(meta["height"], 480)

    async def test_no_info_json_no_ffprobe(self):
        """Neither info JSON nor ffprobe available → returns all None."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            meta = await _extract_video_meta("/fake/video.mp4")
            self.assertIsNone(meta["duration"])
            self.assertIsNone(meta["width"])
            self.assertIsNone(meta["height"])

    async def test_info_json_path_nonexistent(self):
        """info_json_path points to a missing file → proceeds to ffprobe."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            meta = await _extract_video_meta(
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
            with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
                meta = await _extract_video_meta("/fake/video.mp4", info_json_path=path)
                self.assertIsNone(meta["duration"])
        finally:
            os.unlink(path)

    async def test_ffprobe_timeout(self):
        """ffprobe exceeding timeout → returns gracefully."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
            proc_mock = AsyncMock()
            proc_mock.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
            mock_proc.return_value = proc_mock

            meta = await _extract_video_meta("/fake/video.mp4")
            self.assertIsNone(meta["duration"])

    async def test_duration_float_truncation(self):
        """Float duration like 123.99 should be truncated to 123."""
        info = {"duration": 123.99, "width": 640, "height": 360}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(info, f)
            path = f.name

        try:
            meta = await _extract_video_meta("/fake/video.mp4", info_json_path=path)
            self.assertEqual(meta["duration"], 123)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
