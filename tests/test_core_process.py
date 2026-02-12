import asyncio

from app.core.process import run_subprocess


def test_run_subprocess_success():
    async def _run():
        async with run_subprocess(["bash", "-lc", "echo hello"]) as handle:
            out = await handle.proc.stdout.read()
            assert b"hello" in out
        assert handle.exit_code is not None

    asyncio.run(_run())


def test_run_subprocess_cancel():
    async def _run():
        async with run_subprocess(["sleep", "5"]) as handle:
            await handle.cancel()
        assert handle.exit_code is not None

    asyncio.run(_run())
