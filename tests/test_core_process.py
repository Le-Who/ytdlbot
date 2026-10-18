import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from app.core import process


@contextmanager
def _mock_process_boundary(platform, proc):
    group_signal = Mock()
    taskkill = Mock(return_value=SimpleNamespace(returncode=1))
    with (
        patch.object(process, "sys", SimpleNamespace(platform=platform)),
        patch.object(process, "os", SimpleNamespace(killpg=group_signal)),
        patch.object(process, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9)),
        patch.object(
            process,
            "subprocess",
            SimpleNamespace(
                CREATE_NEW_PROCESS_GROUP=0x200,
                run=taskkill,
                SubprocessError=subprocess.SubprocessError,
            ),
        ),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch.object(process, "_create_windows_job", return_value=None),
    ):
        yield group_signal, taskkill


class TestProcess(unittest.IsolatedAsyncioTestCase):
    async def test_run_subprocess_success(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                proc = AsyncMock()
                proc.pid = 43210
                proc.returncode = 0
                proc.stderr.readline.side_effect = [b"err\n", b""]
                proc.terminate = Mock()
                proc.kill = Mock()
                with _mock_process_boundary(platform, proc) as (group_signal, taskkill):
                    async with process.run_subprocess(["echo", "ok"]) as handle:
                        self.assertIs(handle.proc, proc)
                    self.assertEqual(list(handle.stderr_data), [b"err\n"])
                if platform == "linux":
                    group_signal.assert_called_once_with(43210, 9)
                else:
                    group_signal.assert_not_called()
                taskkill.assert_not_called()
                proc.kill.assert_not_called()

    async def test_run_subprocess_cancel(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                proc = AsyncMock()
                proc.pid = 43210
                proc.returncode = None
                proc.stderr.readline.side_effect = [b""]
                proc.kill = Mock()
                proc.terminate = Mock()
                proc.wait.side_effect = [TimeoutError(), TimeoutError(), 0, 0, 0]
                with _mock_process_boundary(platform, proc) as (group_signal, taskkill):
                    async with process.run_subprocess(["sleep", "10"]) as handle:
                        await handle.cancel()
                if platform == "linux":
                    self.assertEqual(
                        group_signal.call_args_list, [call(43210, 15), call(43210, 9)]
                    )
                    taskkill.assert_not_called()
                else:
                    group_signal.assert_not_called()
                    taskkill.assert_called_once_with(
                        ["taskkill", "/F", "/T", "/PID", "43210"],
                        capture_output=True,
                        timeout=0.5,
                    )
                proc.kill.assert_called()


if __name__ == "__main__":
    unittest.main()
