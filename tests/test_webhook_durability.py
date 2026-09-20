from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from telegram.error import NetworkError

from app.api import routes
from app.core import state
from app.core.job_store import (
    DeliveryOutcome,
    JobState,
    JobStore,
    delivery_job_context,
)
from app.main import stop_telegram_ingress
from app.services.media.delivery import DeliveryAsset, TelegramDelivery
from app.services.media.models import (
    DeliveryStatus,
    DeliveryTarget,
    MediaItem,
    MediaKind,
)

AUTH = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
UPDATE = {
    "update_id": 801,
    "message": {
        "message_id": 801,
        "date": 1_700_000_000,
        "chat": {"id": 42, "type": "private"},
        "text": "https://youtu.be/example",
    },
}


class AllowingLimiter:
    async def allow_ip(self, ip: str) -> bool:
        return True


@pytest.fixture()
def webhook_app(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(state, "limiter", AllowingLimiter())
    yield app
    routes.configure_job_store(None)


@pytest.mark.asyncio
async def test_webhook_returns_success_only_after_transactional_insert(
    webhook_app, tmp_path
):
    store = JobStore(tmp_path / "jobs.sqlite3")
    routes.configure_job_store(store)
    transport = httpx.ASGITransport(app=webhook_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook", json=UPDATE, headers=AUTH)

    persisted = await store.get_update(801)
    assert response.status_code == 200
    assert persisted is not None
    assert persisted.state is JobState.ACCEPTED


@pytest.mark.asyncio
async def test_webhook_does_not_acknowledge_before_store_operation_finishes(
    webhook_app,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingStore:
        async def accept_update(self, payload: dict[str, Any]) -> object:
            entered.set()
            await release.wait()
            return object()

    routes.configure_job_store(BlockingStore())
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = asyncio.create_task(
            client.post("/webhook", json=UPDATE, headers=AUTH)
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert request.done() is False
        release.set()
        response = await request

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_webhook_returns_retryable_failure_when_persistence_fails(webhook_app):
    class FailingStore:
        async def accept_update(self, payload: dict[str, Any]) -> object:
            raise OSError("disk full")

    routes.configure_job_store(FailingStore())
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook", json=UPDATE, headers=AUTH)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_normal_webhook_shutdown_keeps_registered_webhook():
    class Bot:
        def __init__(self) -> None:
            self.delete_calls = 0

        async def delete_webhook(self) -> None:
            self.delete_calls += 1

    class BotApp:
        def __init__(self) -> None:
            self.bot = Bot()
            self.updater = None

    bot_app = BotApp()
    await stop_telegram_ingress(bot_app, webhook_enabled=True)
    assert bot_app.bot.delete_calls == 0


class RecordingBot:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls = 0
        self.error = error

    async def send_video(self, **kwargs: object) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        video = SimpleNamespace(file_id="file-1", file_unique_id="unique-1")
        return SimpleNamespace(message_id=91, video=video)


def video_asset(path: Path) -> DeliveryAsset:
    return DeliveryAsset(
        item=MediaItem("video-1", MediaKind.VIDEO, "https://media.example/video-1"),
        source=path,
        item_index=0,
        size_bytes=path.stat().st_size,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_outcome"),
    [
        (None, DeliveryStatus.SUCCESS, DeliveryOutcome.SUCCESS),
        (
            NetworkError("connection lost after upload"),
            DeliveryStatus.UNCERTAIN,
            DeliveryOutcome.UNCERTAIN,
        ),
    ],
)
async def test_delivery_outcome_is_persisted_before_completion_and_not_replayed(
    tmp_path,
    error,
    expected_status,
    expected_outcome,
):
    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 802})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    bot = RecordingBot(error=error)
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)
    target = DeliveryTarget("42")

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        first = await delivery.deliver(video_asset(path), target)

    assert first.status is expected_status
    assert (
        await store.delivery_outcome("802", item_key="42:0:video-1") is expected_outcome
    )
    still_running = await store.get_update(802)
    assert still_running is not None
    assert still_running.state is JobState.RUNNING
    assert await store.delivery_outcome("802", item_key="__job__") is None

    # Simulate a hard crash: no checkpoint/release runs, then the lease expires.
    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        replay = await delivery.deliver(video_asset(path), target)

    assert replay.status is expected_status
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_crash_during_telegram_send_is_recovered_as_uncertain_without_replay(
    tmp_path,
):
    class CrashWindowBot(RecordingBot):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def send_video(self, **kwargs: object) -> object:
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await asyncio.Event().wait()
            video = SimpleNamespace(file_id="file-2", file_unique_id="unique-2")
            return SimpleNamespace(message_id=92, video=video)

    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 803})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    path = tmp_path / "crash.mp4"
    path.write_bytes(b"video")
    bot = CrashWindowBot()
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)
    target = DeliveryTarget("42")

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        interrupted = asyncio.create_task(delivery.deliver(video_asset(path), target))
        await asyncio.wait_for(bot.entered.wait(), timeout=1)
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted

    assert (
        await store.delivery_outcome("803", item_key="42:0:video-1")
        is DeliveryOutcome.UNCERTAIN
    )
    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        replay = await delivery.deliver(video_asset(path), target)

    assert replay.status is DeliveryStatus.UNCERTAIN
    assert bot.calls == 1
