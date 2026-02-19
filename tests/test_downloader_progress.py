import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import importlib
import asyncio

class TestDownloaderProgress(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Create mocks for dependencies
        self.mock_cachetools = MagicMock()
        self.mock_cachetools.TTLCache = MagicMock

        self.mock_dotenv = MagicMock()

        self.mock_telegram = MagicMock()
        self.mock_telegram.Bot = MagicMock

        # Mock InlineKeyboardMarkup properly
        class MockInlineKeyboardMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard
        self.mock_telegram.InlineKeyboardMarkup = MockInlineKeyboardMarkup
        self.mock_telegram.InlineKeyboardButton = MagicMock

        self.mock_telegram_error = MagicMock()
        self.mock_telegram_error.NetworkError = Exception
        self.mock_telegram_error.RetryAfter = Exception
        self.mock_telegram_error.TimedOut = Exception

        self.mock_config = MagicMock()
        self.mock_config.TEMP_DIR = "/tmp/ytdlbot_safe"
        self.mock_config.BASE_URL = "http://localhost:8000"
        self.mock_config.LINK_TTL_MINUTES = 30
        self.mock_config.ENABLE_TELEGRAM_UPLOAD = False
        self.mock_config.MAX_TG_UPLOAD_MB = 45
        self.mock_config.MAX_CONCURRENT_TASKS = 2
        self.mock_config.BOT_TOKEN = "dummy_token"

        self.mock_state = MagicMock()
        self.mock_state.file_cache = {}
        self.mock_state.cancel_cache = {}
        self.mock_state.ytdlp = MagicMock()
        self.mock_state.conversion_lock = AsyncMock()
        self.mock_state.conversion_lock.__aenter__.return_value = None
        self.mock_state.conversion_lock.__aexit__.return_value = None
        self.mock_state.active_processes_lock = AsyncMock()
        self.mock_state.active_processes_lock.__aenter__.return_value = None
        self.mock_state.active_processes_lock.__aexit__.return_value = None
        self.mock_state.active_processes = set()

        self.mock_constants = MagicMock()
        self.mock_constants.AUDIO_FORMAT_ID = "audio"
        self.mock_constants.GIF_FORMAT_ID = "gif"
        self.mock_constants.SUPPORTED_PLATFORMS = []
        self.mock_constants.SUPPORTED_PLATFORMS_SUFFIXES = ()

        self.mock_utils = MagicMock()
        def render_progressbar(percent, length=15):
            return f"[|||||] {percent}%"
        self.mock_utils.render_progressbar = render_progressbar
        import re
        self.mock_utils.PROGRESS_RE = re.compile(r"(\d+\.\d+)%")
        self.mock_utils.PROGRESS_DETAILS_RE = re.compile(r"at\s+(\S+).*?ETA\s+(\S+)")

        # Patch sys.modules
        self.modules_patcher = patch.dict(sys.modules, {
            "cachetools": self.mock_cachetools,
            "dotenv": self.mock_dotenv,
            "telegram": self.mock_telegram,
            "telegram.error": self.mock_telegram_error,
            "telegram.ext": MagicMock(),
            "telegram.constants": MagicMock(),
            "app.core.config": self.mock_config,
            "app.core.state": self.mock_state,
            "app.constants": self.mock_constants,
            "app.core.utils": self.mock_utils,
        })
        self.modules_patcher.start()

        # Import module under test (reloading to ensure patches apply)
        import app.services.downloader
        importlib.reload(app.services.downloader)
        self.downloader_module = app.services.downloader
        self.MediaSender = self.downloader_module.MediaSender

    def tearDown(self):
        self.modules_patcher.stop()

    async def test_download_video_progress_format(self):
        """
        Verifies that the progress message format includes the heartbeat icon
        and removes the redundant cancel text.
        """
        mock_proc = AsyncMock()
        # Simulate stdout lines
        mock_proc.stdout.readline.side_effect = [
            b"[download]  25.0% of 10.00MiB at  2.50MiB/s ETA 00:30\n",
            b"[download]  50.0% of 10.00MiB at  5.00MiB/s ETA 00:15\n",
            b"[download]  75.0% of 10.00MiB at  7.50MiB/s ETA 00:05\n",
            b"" # End of stream
        ]
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0

        async def mock_run_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        # Sequence designed to toggle int(now) % 2
        time_vals = [
            100.0,
            101.0, 104.0, # Loop 1 -> 104 -> Even -> ⏳
            105.0, 109.0, # Loop 2 -> 109 -> Odd -> ⌛
            110.0, 114.0, # Loop 3 -> 114 -> Even -> ⏳
            115.0, 120.0, 125.0, 130.0
        ]

        # Patch run_subprocess in the IMPORTED module namespace
        with patch.object(self.downloader_module, "run_subprocess", side_effect=mock_run_subprocess_gen), \
             patch.object(self.downloader_module.os.path, "exists", return_value=True), \
             patch.object(self.downloader_module.os.path, "getsize", return_value=1024), \
             patch.object(self.downloader_module, "safe_remove"), \
             patch("time.time", side_effect=time_vals):

            # Reset state mocks just in case
            self.downloader_module.state.file_cache = {}
            self.downloader_module.state.cancel_cache = {}
            self.downloader_module.state.ytdlp.build_command.return_value = ["cmd"]

            mock_callback = AsyncMock()

            await self.MediaSender.download_video(
                "http://example.com",
                "format_id",
                720,
                "token",
                progress_callback=mock_callback
            )

            self.assertTrue(mock_callback.called)
            calls = mock_callback.call_args_list

            self.assertGreaterEqual(len(calls), 3)

            # Call 0 (Time 104 -> Even -> ⏳)
            self.assertIn("⏳", calls[0][0][0])
            self.assertIn("Скачиваю...", calls[0][0][0])
            self.assertNotIn("Нажмите отмена", calls[0][0][0])

            # Call 1 (Time 109 -> Odd -> ⌛)
            self.assertIn("⌛", calls[1][0][0])

            # Call 2 (Time 114 -> Even -> ⏳)
            self.assertIn("⏳", calls[2][0][0])

if __name__ == "__main__":
    unittest.main()
