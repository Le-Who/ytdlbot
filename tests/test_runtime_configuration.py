from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from app import main
from app.core.job_store import JobStore


@pytest.mark.parametrize("value", ["invalid", "-1", "nan", "inf", "-inf"])
async def test_invalid_drain_timeout_rejects_lifespan_before_acquiring_resources(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", value)
    construction = Mock(side_effect=AssertionError("runtime resource constructed"))
    monkeypatch.setattr(main, "JobStore", construction)
    monkeypatch.setattr(main, "_build_telegram_application_builder", construction)

    # A rejected start must release lifespan ownership so another start can
    # report its configuration error, rather than seeing a phantom live worker.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="DRAIN_TIMEOUT_SECONDS"):
            async with main.lifespan(FastAPI()):
                pytest.fail("invalid drain configuration started the application")

    assert construction.call_count == 0


@pytest.mark.parametrize("value", [None, "0", "0.25"])
async def test_lifespan_captures_valid_drain_timeout_before_environment_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DRAIN_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", value)
    store = JobStore(tmp_path / "jobs.sqlite3")
    workers = []
    background_tasks = []
    stop = asyncio.Event()

    class RecordingWorker(main.DurableUpdateWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)

    async def background(stop_event: asyncio.Event) -> None:
        background_tasks.append(asyncio.current_task())
        await stop_event.wait()
        stop.set()

    application = SimpleNamespace(
        bot=SimpleNamespace(set_webhook=AsyncMock()),
        add_handler=Mock(),
        add_error_handler=Mock(),
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        process_update=AsyncMock(),
    )
    monkeypatch.setattr(main, "JobStore", lambda: store)
    monkeypatch.setattr(main, "DurableUpdateWorker", RecordingWorker)
    monkeypatch.setattr(main, "janitor_loop", background)
    monkeypatch.setattr(
        main,
        "_build_telegram_application_builder",
        lambda: SimpleNamespace(build=lambda: application),
    )
    monkeypatch.setattr(main.config, "WEBHOOK_URL", "https://bot.example")
    monkeypatch.setattr(main.config, "TELEGRAM_LOCAL_ENDPOINT", "")
    monkeypatch.setattr(main.state, "redis_client", None)
    monkeypatch.setattr(main.state, "bot_app", None)
    monkeypatch.setattr(main.state, "job_store", None)
    monkeypatch.setattr(main.state, "media_pipeline", None)
    monkeypatch.setattr(main, "_active_lifespan", None)
    monkeypatch.setattr(
        "app.services.media.pipeline.build_default_pipeline", lambda _: object()
    )
    try:
        async with main.lifespan(FastAPI()):
            # Shutdown must use the already validated value, even if another
            # component changes the environment after resources are acquired.
            monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "invalid-after-startup")
        assert stop.is_set()
        assert all(task.done() for task in background_tasks)
        assert main.state.bot_app is None
        assert main.state.job_store is None
        assert main._active_lifespan is None
        assert await store.acquire_worker("replacement") is True
        await store.release_worker("replacement")
    finally:
        for worker in workers:
            await worker.stop()
        for task in background_tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        main.configure_job_store(None)
        await store.close()
