import unittest
import asyncio
import os
import sys
import importlib
from unittest.mock import MagicMock, patch, AsyncMock

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestCleanup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mock dependencies
        self.mock_state_module = MagicMock()
        # file_cache should be a mock to allow mocking .values()
        self.mock_state_module.file_cache = MagicMock()

        self.modules_patcher = patch.dict(sys.modules, {
            "app.core.state": self.mock_state_module,
        })
        self.modules_patcher.start()

        # Mock app.core.config
        self.mock_config_module = MagicMock()
        self.mock_config_module.TEMP_DIR = "/mock/temp/dir"

        self.modules_patcher_config = patch.dict(sys.modules, {
             "app.core.config": self.mock_config_module,
        })
        self.modules_patcher_config.start()

        import app.services.cleanup
        importlib.reload(app.services.cleanup)
        self.cleanup = app.services.cleanup

    def tearDown(self):
        self.modules_patcher_config.stop()
        self.modules_patcher.stop()

    @patch("asyncio.sleep")
    @patch("os.path.exists")
    @patch("os.listdir")
    @patch("os.path.isfile")
    @patch("os.path.getmtime")
    @patch("os.unlink")
    async def test_cleanup_loop_logic(self, mock_unlink, mock_mtime, mock_isfile, mock_listdir, mock_exists, mock_sleep):
        # Setup
        mock_sleep.side_effect = [None, asyncio.CancelledError()] # Run once then cancel
        mock_exists.return_value = True

        # Files in temp dir
        mock_listdir.return_value = ["ytdl_1.mp4", "ytdl_2.mp4", "other_file.txt", "ytdl_active.mp4"]

        # File checks
        mock_isfile.return_value = True

        # Time setup
        now = 10000.0
        with patch("time.time", return_value=now):
            # Grace period is 600s. Cutoff is 9400.

            def mtime_side_effect(path):
                if "ytdl_1" in path: return 9000.0 # Stale
                if "ytdl_2" in path: return 9900.0 # New
                if "ytdl_active" in path: return 9000.0 # Old but active
                return 0.0

            mock_mtime.side_effect = mtime_side_effect

            # Setup cache to contain ytdl_active
            # The cleanup loop calls set(state.file_cache.values())
            # So file_cache.values() must return an iterable
            self.mock_state_module.file_cache.values.return_value = ["/mock/temp/dir/ytdl_active.mp4"]

            # Run
            try:
                await self.cleanup.cleanup_loop()
            except asyncio.CancelledError:
                pass

            # Verify
            # Only ytdl_1 should be deleted
            mock_unlink.assert_called_once_with("/mock/temp/dir/ytdl_1.mp4")

            # Verify mtime calls logic
            mtime_calls = [c[0][0] for c in mock_mtime.call_args_list]
            self.assertTrue(any("ytdl_1" in p for p in mtime_calls))
            self.assertTrue(any("ytdl_2" in p for p in mtime_calls))
            # ytdl_active is active, so should be skipped before mtime check
            self.assertFalse(any("ytdl_active" in p for p in mtime_calls))
            # other_file starts with "other_", so should be skipped before everything
            self.assertFalse(any("other_file" in p for p in mtime_calls))

    @patch("asyncio.sleep")
    @patch("os.path.exists")
    async def test_cleanup_loop_no_dir(self, mock_exists, mock_sleep):
        mock_sleep.side_effect = [None, asyncio.CancelledError()]
        mock_exists.return_value = False

        with patch("os.listdir") as mock_listdir:
            try:
                await self.cleanup.cleanup_loop()
            except asyncio.CancelledError:
                pass
            mock_listdir.assert_not_called()

if __name__ == '__main__':
    unittest.main()
