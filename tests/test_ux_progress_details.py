import unittest
import asyncio
import sys
import time
from unittest.mock import MagicMock, AsyncMock, patch, call
import os
import importlib

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestUXProgressDetails(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mock telegram module
        self.mock_telegram = MagicMock()
        self.mock_telegram.InlineKeyboardMarkup = MagicMock()
        self.mock_telegram.InlineKeyboardButton = MagicMock()
        self.mock_telegram.Bot = MagicMock()
        self.mock_telegram.error = MagicMock()

        # Mock dotenv
        self.mock_dotenv = MagicMock()
        self.mock_dotenv.load_dotenv = MagicMock()

        # Mock app.core.state
        self.mock_state = MagicMock()
        self.mock_state.file_cache = {}
        self.mock_state.cancel_cache = {}
        self.mock_state.ytdlp = MagicMock()
        self.mock_state.ytdlp.build_command.return_value = ["mock_cmd"]

        # Patch sys.modules
        self.modules_patcher = patch.dict(sys.modules, {
            "telegram": self.mock_telegram,
            "telegram.error": self.mock_telegram.error,
            "app.core.state": self.mock_state,
            "dotenv": self.mock_dotenv,
        })
        self.modules_patcher.start()

        # Mock environment variables for config BEFORE importing anything
        # We need to use patch.dict on os.environ directly because config module reads it at import time
        self.env_patcher = patch.dict(os.environ, {
            "BOT_TOKEN": "test_token",
            "TEMP_DIR": "/tmp",
            "BASE_URL": "http://test.com"
        })
        self.env_patcher.start()

        # Import module under test
        # We need to reload app.core.config too because it might have been imported differently
        import app.core.config
        importlib.reload(app.core.config)

        import app.services.downloader
        importlib.reload(app.services.downloader)
        self.downloader = app.services.downloader

    def tearDown(self):
        self.modules_patcher.stop()
        self.env_patcher.stop()

    async def test_progress_format(self):
        # Mock run_subprocess to yield a progress line
        mock_proc = AsyncMock()
        # It needs to return bytes for stdout.readline
        mock_proc.stdout.readline.side_effect = [
            b"[download]  53.2% of 10.00MiB at  2.50MiB/s ETA 00:45\n",
            b""
        ]
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        # Mock run_subprocess generator
        # It yields (proc, stderr_deque)
        async def mock_run_gen(*args, **kwargs):
            yield mock_proc, []

        mock_callback = AsyncMock()

        # Patch run_subprocess in app.services.downloader module namespace
        # Because the module does: from app.core.utils import run_subprocess
        # We must patch app.services.downloader.run_subprocess
        with patch("app.services.downloader.run_subprocess", side_effect=mock_run_gen) as mock_run:

            # Also mock time.time to control the toggle icon
            with patch("time.time") as mock_time:
                # First call: download_start = time.time()
                # Second call: check timeout
                # Third call: now = time.time() inside progress check
                # Fourth call: last_update > 3.0 check (needs to be True)

                # Let's just make time increase
                start_time = 1000.0
                mock_time.side_effect = [
                    start_time,          # download_start
                    start_time + 1,      # timeout check
                    start_time + 4.0,    # progress check 'now' (diff > 3.0)
                    start_time + 5.0,    # next loop timeout check
                    start_time + 6.0,    # next loop...
                ]

                # Mock os.path.exists to avoid file check errors (it checks if file exists after download)
                with patch("os.path.exists", return_value=True):
                    # Mock os.path.getsize to avoid size check error
                    with patch("os.path.getsize", return_value=1024):
                        await self.downloader.MediaSender.download_video(
                            "http://test.com", "format_id", 720, "token",
                            progress_callback=mock_callback
                        )

        # Verify callback was called
        self.assertTrue(mock_callback.called)

        # Check the message content
        # args[0] is the text
        # args[1] is the markup (kb_cancel)
        text = mock_callback.call_args[0][0]

        print(f"\nGenerated text: '{text}'")

        # Current format (to fail initially):
        # "⏳ Скачиваю: {bar} 53.2%\n🚀 2.50MiB/s • ⏱ ETA 00:45\n❌ Нажмите отмена..."

        # Verify it DOES contain redundant text (checking current state)
        # Or assert failure if I expect new state

        # I want this test to PASS when I implement changes.
        # So I assert the NEW state.

        # Expecting: "⏳ 2.50MiB/s • 00:45 left\n████████░░░░░░░ 53.2%"

        self.assertIn("2.50MiB/s", text)
        self.assertIn("00:45", text) # removed "ETA" prefix check if present in new
        self.assertIn("53.2%", text)

        # Verify it DOES NOT contain redundant text
        self.assertNotIn("Нажмите отмена", text)
        self.assertNotIn("Скачиваю:", text)

if __name__ == '__main__':
    unittest.main()
