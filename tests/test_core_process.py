import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.core.process import run_subprocess

class TestProcess(unittest.IsolatedAsyncioTestCase):
    async def test_run_subprocess_success(self):
        proc = AsyncMock()
        proc.returncode = 0
        proc.stderr.readline.side_effect = [b"err\n", b""]
        proc.terminate = Mock()
        proc.kill = Mock()
        proc.terminate = Mock()
        proc.kill = Mock()
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            async with run_subprocess(["echo", "ok"]) as handle:
                self.assertEqual(handle.proc, proc)
            self.assertEqual(list(handle.stderr_data), [b"err\n"])

    async def test_run_subprocess_cancel(self):
        import asyncio

        proc = AsyncMock()
        proc.returncode = None
        proc.stderr.readline.side_effect = [b""]
        proc.kill = Mock()
        proc.wait = AsyncMock()
        proc.wait.side_effect = [asyncio.TimeoutError, 0, 0]
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            async with run_subprocess(["sleep", "10"]) as handle:
                await handle.cancel()
            proc.kill.assert_called()

if __name__ == "__main__":
    unittest.main()
