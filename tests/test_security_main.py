import unittest
from unittest.mock import patch, AsyncMock, MagicMock
import asyncio
from fastapi.testclient import TestClient
import os

# Ensure env vars are set before app import
os.environ["BOT_TOKEN"] = "test_token_123"

from app.main import api, link_cache, GIF_FORMAT_ID


class TestSecurityMain(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api)

    @patch("app.main.asyncio.create_subprocess_exec")
    @patch("app.main.ytdlp.build_command")
    @patch("app.main.safe_remove")  # Mock file removal
    def test_download_gif_consumes_stderr(self, mock_remove, mock_build, mock_exec):
        # Setup
        token = "test_token_gif"
        link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": GIF_FORMAT_ID,
            "height": 720,
            "title": "Test GIF",
        }

        mock_build.return_value = ["echo", "test"]

        # Mock process for yt-dlp
        mock_proc_ytdlp = MagicMock()
        mock_proc_ytdlp.returncode = 0
        mock_proc_ytdlp.wait = AsyncMock(return_value=None)
        mock_proc_ytdlp.stderr = AsyncMock()
        # Simulate stderr output
        mock_proc_ytdlp.stderr.readline.side_effect = [b"log1\n", b"log2\n", b""]
        mock_proc_ytdlp.stdout = AsyncMock()
        mock_proc_ytdlp.stdout.read.return_value = b""

        # Mock process for ffmpeg
        mock_proc_ffmpeg = MagicMock()
        mock_proc_ffmpeg.returncode = 0
        mock_proc_ffmpeg.wait = AsyncMock(return_value=None)
        mock_proc_ffmpeg.stderr = AsyncMock()
        mock_proc_ffmpeg.stderr.readline.side_effect = [b""]
        mock_proc_ffmpeg.stdout = AsyncMock()
        mock_proc_ffmpeg.stdout.read.side_effect = [b"gif_data", b""]

        mock_exec.side_effect = [mock_proc_ytdlp, mock_proc_ffmpeg]

        # Call endpoint
        response = self.client.get(f"/dl/{token}")

        # Assertions
        self.assertEqual(response.status_code, 200)

        # Consume the stream to trigger execution
        list(response.iter_bytes())

        # Check if stderr.readline was called for yt-dlp process
        # In vulnerable code: it is NOT called (wait() is called, then stderr.read() if fail)
        # In fixed code: it IS called by background task

        # We check if readline was called at least once
        self.assertTrue(
            mock_proc_ytdlp.stderr.readline.called,
            "stderr.readline() should be called to prevent deadlock",
        )

        # Verify stdout was DEVNULL for ytdlp
        # First call to create_subprocess_exec
        args, kwargs = mock_exec.call_args_list[0]
        self.assertEqual(
            kwargs["stdout"],
            asyncio.subprocess.DEVNULL,
            "stdout should be DEVNULL to avoid deadlock",
        )
