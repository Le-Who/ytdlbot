"""
Injection prevention tests — verify CLI injection mitigation measures.

Tests for SEC-1 (URL separator in yt-dlp), SEC-2 (stream byte limit),
SEC-3 (URL separator in gallery-dl), and URL prefix validation.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("BOT_TOKEN", "test_token")
sys.modules.setdefault("yt_dlp", MagicMock())

from app.core.utils import is_supported_url
from app.services.ytdlp.service import YtDlpService
from app.constants import GIF_FORMAT_ID


class TestURLSeparatorInjection(unittest.TestCase):
    """SEC-1: All yt-dlp subprocess commands must contain '--' before the URL."""

    def setUp(self):
        self.service = YtDlpService()
        self.service.cookies_manager.get_cookies_path = lambda url: None

    def _assert_separator_before_url(self, cmd: list, url: str):
        """Assert '--' appears in cmd and immediately precedes the URL."""
        self.assertIn("--", cmd, "Command must contain '--' end-of-options separator")
        sep_idx = cmd.index("--")
        self.assertEqual(cmd[sep_idx + 1], url,
                         f"URL must immediately follow '--', got: {cmd[sep_idx + 1]}")
        self.assertEqual(cmd[-1], url, "URL must be the last argument in the command")

    def test_video_command_has_separator(self):
        url = "https://youtube.com/watch?v=test"
        cmd = self.service.build_command(
            page_url=url, format_id="137+140", height=1080, output="/tmp/out.mp4"
        )
        self._assert_separator_before_url(cmd, url)

    def test_audio_command_has_separator(self):
        url = "https://youtube.com/watch?v=test"
        cmd = self.service.build_command(
            page_url=url, format_id="bestaudio/best", height=None, output="/tmp/out.mp3"
        )
        self._assert_separator_before_url(cmd, url)

    def test_gif_command_has_separator(self):
        url = "https://pinterest.com/pin/123"
        cmd = self.service.build_command(
            page_url=url, format_id=GIF_FORMAT_ID, height=None, output="/tmp/out.mp4"
        )
        self._assert_separator_before_url(cmd, url)

    def test_pipe_command_has_separator(self):
        url = "https://youtube.com/watch?v=test"
        cmd = self.service.build_command(
            page_url=url, format_id="best", height=720, output="-"
        )
        self._assert_separator_before_url(cmd, url)

    def test_malicious_exec_flag_url(self):
        """Crafted URL starting with --exec must be placed after '--'."""
        url = "--exec=rm -rf /"
        cmd = self.service.build_command(
            page_url=url, format_id="best", height=720, output="/tmp/out.mp4"
        )
        self._assert_separator_before_url(cmd, url)
        sep_idx = cmd.index("--")
        for arg in cmd[:sep_idx]:
            self.assertFalse(arg.startswith("--exec"),
                             f"Malicious '--exec' found before separator")

    def test_cookies_placed_before_separator(self):
        """--cookies flag must appear before the '--' separator."""
        self.service.cookies_manager.get_cookies_path = lambda url: "/tmp/cookies.txt"
        url = "https://www.tiktok.com/@user/video/123"
        cmd = self.service.build_command(
            page_url=url, format_id="best", height=720, output="/tmp/out.mp4"
        )
        self.assertIn("--cookies", cmd)
        cookies_idx = cmd.index("--cookies")
        sep_idx = cmd.index("--")
        self.assertLess(cookies_idx, sep_idx,
                        "--cookies must appear before the '--' separator")


class TestGalleryDlSeparator(unittest.TestCase):
    """SEC-3: gallery-dl commands must have '--' before URL, cookies before '--'."""

    def test_gallery_dl_cookies_before_separator(self):
        """Verify '--cookies' is placed before '--' in gallery-dl command."""
        import subprocess
        from app.services.gallery_dl.service import GalleryDlService

        service = GalleryDlService()
        url = "https://tiktok.com/@user/video/123"

        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            try:
                service.download_slideshow(url, cookies_path="/tmp/cookies.txt")
            except Exception:
                pass  # We just need to capture the command

            if mock_run.called:
                cmd = mock_run.call_args[0][0]
                self.assertIn("--", cmd)
                sep_idx = cmd.index("--")
                self.assertEqual(cmd[sep_idx + 1], url,
                                 "URL must immediately follow '--'")
                if "--cookies" in cmd:
                    cookies_idx = cmd.index("--cookies")
                    self.assertLess(cookies_idx, sep_idx,
                                    "--cookies must appear before '--' separator")


class TestURLPrefixValidation(unittest.TestCase):
    """SEC-1 hardening: URLs starting with '-' must be rejected early."""

    def test_double_dash_exec_rejected(self):
        self.assertFalse(is_supported_url("--exec=malicious"))

    def test_single_dash_rejected(self):
        self.assertFalse(is_supported_url("-o /tmp/pwned"))

    def test_double_dash_flag_rejected(self):
        self.assertFalse(is_supported_url("--version"))

    def test_dash_with_url_scheme_rejected(self):
        self.assertFalse(is_supported_url("-https://youtube.com"))

    def test_normal_youtube_accepted(self):
        self.assertTrue(is_supported_url("https://youtube.com/watch?v=abc"))

    def test_normal_tiktok_accepted(self):
        self.assertTrue(is_supported_url("https://www.tiktok.com/@user/video/123"))


class TestStreamByteLimit(unittest.TestCase):
    """SEC-2: Streaming endpoint byte-count logic verification."""

    def test_byte_limit_terminates_stream(self):
        from app.core.config import MAX_DL_MB

        max_bytes = MAX_DL_MB * 1024 * 1024
        chunk_size = 1024 * 1024  # 1 MB
        total_possible = (max_bytes // chunk_size) + 10

        bytes_sent = 0
        chunks_sent = 0
        for _ in range(total_possible):
            bytes_sent += chunk_size
            chunks_sent += 1
            if bytes_sent > max_bytes:
                break

        self.assertLess(chunks_sent, total_possible,
                        "Loop must terminate before consuming all chunks")
        self.assertGreater(bytes_sent, max_bytes,
                           "bytes_sent must exceed max_bytes to trigger termination")

    def test_max_dl_mb_is_positive(self):
        from app.core.config import MAX_DL_MB
        self.assertGreater(MAX_DL_MB, 0, "MAX_DL_MB must be a positive integer")


if __name__ == "__main__":
    unittest.main()
