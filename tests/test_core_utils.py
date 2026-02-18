import unittest
import asyncio
import os
import sys
import importlib
from unittest.mock import MagicMock, patch, AsyncMock

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestCoreUtils(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mock environment variables
        self.env_patcher = patch.dict(os.environ, {
            "BOT_TOKEN": "test_token",
            "MAX_CONCURRENT_TASKS": "2"
        })
        self.env_patcher.start()

        # Mock dependencies to prevent side effects and ImportErrors
        # We need to mock app.core.state because utils imports from it
        self.mock_state_module = MagicMock()
        self.mock_state_module.active_processes_lock = asyncio.Lock()
        self.mock_state_module.active_processes = set()
        self.mock_state_module.user_rates = {}

        self.modules_patcher = patch.dict(sys.modules, {
            "app.core.state": self.mock_state_module,
        })
        self.modules_patcher.start()

        # Import the module under test
        # We must verify if it's already in sys.modules and reload it to pick up our mocks
        # or just import it if not present.
        import app.core.utils
        importlib.reload(app.core.utils)
        self.utils = app.core.utils

    def tearDown(self):
        self.modules_patcher.stop()
        self.env_patcher.stop()

    @patch("os.path.exists")
    @patch("os.unlink")
    def test_safe_remove_exists(self, mock_unlink, mock_exists):
        mock_exists.return_value = True
        self.utils.safe_remove("test_file")
        mock_unlink.assert_called_once_with("test_file")

    @patch("os.path.exists")
    @patch("os.unlink")
    def test_safe_remove_empty_or_none(self, mock_unlink, mock_exists):
        # Empty string
        self.utils.safe_remove("")
        mock_exists.assert_not_called()
        mock_unlink.assert_not_called()

        # None
        self.utils.safe_remove(None)
        mock_exists.assert_not_called()
        mock_unlink.assert_not_called()

    @patch("os.path.exists")
    @patch("os.unlink")
    def test_safe_remove_unexpected_error(self, mock_unlink, mock_exists):
        mock_exists.return_value = True
        mock_unlink.side_effect = RuntimeError("Unexpected error")

        # Should raise RuntimeError
        with self.assertRaises(RuntimeError):
            self.utils.safe_remove("test_file")

        mock_unlink.assert_called_once_with("test_file")

    @patch("os.path.exists")
    @patch("os.unlink")
    def test_safe_remove_not_exists(self, mock_unlink, mock_exists):
        mock_exists.return_value = False
        self.utils.safe_remove("test_file")
        mock_unlink.assert_not_called()

    @patch("os.path.exists")
    @patch("os.unlink")
    def test_safe_remove_error(self, mock_unlink, mock_exists):
        mock_exists.return_value = True
        mock_unlink.side_effect = OSError("Access denied")
        # Should not raise exception
        self.utils.safe_remove("test_file")
        mock_unlink.assert_called_once_with("test_file")

    @patch("os.path.exists")
    @patch("os.rename")
    def test_rename_if_exists_exists(self, mock_rename, mock_exists):
        mock_exists.return_value = True
        self.utils.rename_if_exists("src", "dst")
        mock_rename.assert_called_once_with("src", "dst")

    @patch("os.path.exists")
    @patch("os.rename")
    def test_rename_if_exists_not_exists(self, mock_rename, mock_exists):
        mock_exists.return_value = False
        self.utils.rename_if_exists("src", "dst")
        mock_rename.assert_not_called()

    async def test_run_subprocess_success(self):
        # Mock dependencies
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline.side_effect = [b"", b""] # Simulate EOF immediately
        mock_proc.stderr.readline.side_effect = [b"", b""]
        mock_proc.wait.return_value = None

        # Patch create_subprocess_exec
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            # We don't need to patch active_processes here because we injected our mock state module
            # But run_subprocess uses 'from app.core.state import ...' so it has references to what was in the module
            # at import time. Since we reloaded utils in setUp, it should have our mock_state_module attributes.

            # Execute
            async for proc, stderr_deque in self.utils.run_subprocess(["echo", "hello"]):
                self.assertEqual(proc, mock_proc)
                self.assertIn(proc, self.mock_state_module.active_processes)
                self.assertIsInstance(stderr_deque, self.utils.deque)

            # Verify cleanup
            mock_proc.wait.assert_awaited()
            self.assertNotIn(proc, self.mock_state_module.active_processes)

    async def test_run_subprocess_stderr_capture(self):
        # Mock dependencies
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline.side_effect = [b"", b""]
        # Simulate stderr output
        mock_proc.stderr.readline.side_effect = [b"Error line 1\n", b"Error line 2\n", b""]
        mock_proc.wait.return_value = None

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            async for proc, stderr_deque in self.utils.run_subprocess(["ls", "nonexistent"]):
                pass

            # Verify stderr content
            self.assertEqual(len(stderr_deque), 2)
            self.assertEqual(stderr_deque[0], b"Error line 1\n")
            self.assertEqual(stderr_deque[1], b"Error line 2\n")

    async def test_run_subprocess_cleanup_on_exception(self):
        # Mock dependencies
        mock_proc = AsyncMock()
        mock_proc.returncode = None # Still running
        mock_proc.stdout.readline.side_effect = [b"output\n", b""]
        mock_proc.stderr.readline.side_effect = [b""]
        mock_proc.wait.side_effect = [asyncio.TimeoutError(), None] # Simulate wait exception or normal exit after terminate
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            gen = self.utils.run_subprocess(["sleep", "10"])
            try:
                async for proc, stderr_deque in gen:
                    raise ValueError("Simulated error")
            except ValueError:
                pass

            # Force cleanup
            await gen.aclose()

            # Verify terminate called because returncode is None
            mock_proc.terminate.assert_called_once()
            self.assertNotIn(mock_proc, self.mock_state_module.active_processes)

    async def test_run_subprocess_no_stderr(self):
        # Mock dependencies
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline.side_effect = [b""]

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            async for proc, stderr_deque in self.utils.run_subprocess(["cmd"], collect_stderr=False):
                self.assertEqual(len(stderr_deque), 0)

            # Check create_subprocess_exec called with stderr=DEVNULL
            args, kwargs = mock_exec.call_args
            self.assertEqual(kwargs['stderr'], asyncio.subprocess.DEVNULL)

    def test_render_progressbar(self):
        """Test render_progressbar with various inputs."""
        # Test 0%
        # 0% of 15 is 0 blocks.
        # "░░░░░░░░░░░░░░░ 0.0%"
        self.assertEqual(self.utils.render_progressbar(0), "░" * 15 + " 0.0%")

        # Test 100%
        # "███████████████ 100.0%"
        self.assertEqual(self.utils.render_progressbar(100), "█" * 15 + " 100.0%")

        # Test 50%
        # 50% of 15 is 7.5 -> 7 blocks
        # "███████░░░░░░░░ 50.0%"
        self.assertEqual(self.utils.render_progressbar(50), "█" * 7 + "░" * 8 + " 50.0%")

        # Test negative (clamp to 0)
        self.assertEqual(self.utils.render_progressbar(-10), "░" * 15 + " 0.0%")

        # Test overflow (clamp to 100)
        self.assertEqual(self.utils.render_progressbar(150), "█" * 15 + " 100.0%")

        # Test custom length
        # 50% of 10 is 5
        self.assertEqual(self.utils.render_progressbar(50, length=10), "█" * 5 + "░" * 5 + " 50.0%")

        # Test rounding
        # 25% of 10 is 2.5 -> 2
        self.assertEqual(self.utils.render_progressbar(25, length=10), "█" * 2 + "░" * 8 + " 25.0%")

    @patch("time.time")
    def test_check_rate_limit(self, mock_time):
        """Test rate limiting logic."""
        mock_time.return_value = 1000.0
        user_id = 12345
        limit = 2

        # 1st call: OK
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))

        # 2nd call: OK
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))

        # 3rd call: Fail
        self.assertFalse(self.utils.check_rate_limit(user_id, limit))

        # Check that user_rates was updated correctly
        # Key should be "12345:16" (1000/60 = 16.66 -> 16)
        expected_key = "12345:16"
        self.assertEqual(self.mock_state_module.user_rates[expected_key], 2)

        # Move to next minute
        mock_time.return_value = 1060.0
        # Should be OK again
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))
        expected_key_next = "12345:17"
        self.assertEqual(self.mock_state_module.user_rates[expected_key_next], 1)

if __name__ == '__main__':
    unittest.main()
