from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from telegram import Message
from telegram.error import BadRequest, NetworkError

from app import main as main_module
from app.api import routes
from app.bot import callbacks
from app.core import drain as drain_module
from app.core import state
from app.core.job_store import (
    DeliveryOutcome,
    JobState,
    JobStore,
    delivery_job_context,
)
from app.main import lifespan, shutdown_runtime, stop_telegram_ingress
from app.services.media.delivery import DeliveryAsset, TelegramDelivery
from app.services.media.models import (
    DeliveryStatus,
    DeliveryTarget,
    MediaItem,
    MediaKind,
    MediaRequest,
)
from app.services.media.pipeline import encode_callback_data
from app.services.sender import TelegramSender

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


class AsyncCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value):
        self[key] = value


def gif_callback(token: str, bot: object):
    message = MagicMock(spec=Message)
    message.chat_id = 42
    message.message_id = 77
    message.animation = None
    message.video = None
    message.edit_reply_markup = AsyncMock()
    message.reply_text = AsyncMock()
    query = MagicMock()
    query.data = encode_callback_data("giffile", token)
    query.message = message
    query.answer = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    context = SimpleNamespace(bot=bot)
    return update, context


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


@pytest.mark.asyncio
async def test_shutdown_cleanup_continues_after_drain_and_worker_failures(monkeypatch):
    class BrokenDrain:
        async def drain(self, *, deadline_seconds: float) -> None:
            raise OSError("checkpoint volume unavailable")

    class BrokenWorker:
        async def stop(self) -> None:
            raise RuntimeError("worker stop failed")

    stop_event = asyncio.Event()

    async def background() -> None:
        await stop_event.wait()

    background_task = asyncio.create_task(background())
    bot_app = SimpleNamespace(
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        updater=None,
    )
    store = SimpleNamespace(close=AsyncMock())
    redis_client = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(state, "media_pipeline", object())
    routes.configure_job_store(store)

    await shutdown_runtime(
        drain_controller=BrokenDrain(),
        durable_worker=BrokenWorker(),
        stop_event=stop_event,
        background_tasks=(background_task,),
        bot_app=bot_app,
        webhook_enabled=True,
        job_store=store,
        redis_client=redis_client,
        drain_timeout_seconds=0,
    )

    assert stop_event.is_set()
    assert background_task.done()
    bot_app.stop.assert_awaited_once()
    bot_app.shutdown.assert_awaited_once()
    store.close.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()
    assert state.media_pipeline is None
    assert routes._job_store is None


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_runtime", [False, True, "overlap"])
async def test_lifespan_retains_dependencies_while_cancelled_handler_is_still_alive(
    tmp_path, monkeypatch, replacement_runtime
):
    """A bounded worker stop cannot close resources still used by its handler."""
    monkeypatch.setattr(drain_module, "CLEANUP_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setenv("DRAIN_TIMEOUT_SECONDS", "0")
    overlap_lifespan = replacement_runtime == "overlap"
    replacement_runtime = replacement_runtime is True
    entered = asyncio.Event()
    finish = asyncio.Event()
    dependencies_used = asyncio.Event()
    created_workers = []
    pipeline = object()
    replacement_bot = object()
    replacement_pipeline = object()
    replacement_store = object()

    class RecordingStore(JobStore):
        closed = False
        close_calls = 0

        async def close(self):
            self.closed = True
            self.close_calls += 1
            await super().close()

    class Redis:
        closed = False
        close_calls = 0

        async def ping(self):
            # redis-py can reconnect at a later legitimate lifespan after close.
            self.closed = False

        async def aclose(self):
            self.closed = True
            self.close_calls += 1

    class BotApp:
        closed = False
        stopped = False
        stop_calls = 0
        shutdown_calls = 0
        initialized = False
        updater = None
        bot = SimpleNamespace(set_webhook=AsyncMock())

        def add_handler(self, handler):
            pass

        def add_error_handler(self, handler):
            pass

        async def initialize(self):
            self.initialized = True

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True
            self.stop_calls += 1

        async def shutdown(self):
            self.closed = True
            self.shutdown_calls += 1

        async def process_update(self, update):
            entered.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    continue
            assert not self.closed and not self.stopped
            assert not store.closed and not redis.closed
            assert state.bot_app is (replacement_bot if replacement_runtime else self)
            assert state.media_pipeline is (
                replacement_pipeline if replacement_runtime else pipeline
            )
            assert state.job_store is (
                replacement_store if replacement_runtime else store
            )
            assert routes._job_store is (
                replacement_store if replacement_runtime else store
            )
            dependencies_used.set()

    class RecordingWorker(main_module.DurableUpdateWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_workers.append(self)

    store = RecordingStore(tmp_path / "jobs.sqlite3")
    await store.accept_update(UPDATE)
    redis = Redis()
    bot_app = BotApp()

    async def background(stop_event):
        await stop_event.wait()

    monkeypatch.setattr(main_module, "JobStore", lambda: store)
    monkeypatch.setattr(main_module, "DurableUpdateWorker", RecordingWorker)
    monkeypatch.setattr(main_module, "janitor_loop", background)
    monkeypatch.setattr(
        main_module,
        "_build_telegram_application_builder",
        lambda: SimpleNamespace(build=lambda: bot_app),
    )
    monkeypatch.setattr(main_module.config, "WEBHOOK_URL", "https://bot.example")
    monkeypatch.setattr(main_module.config, "TELEGRAM_LOCAL_ENDPOINT", None)
    monkeypatch.setattr(state, "redis_client", redis)
    monkeypatch.setattr(state, "job_store", None)
    monkeypatch.setattr(state, "bot_app", None)
    monkeypatch.setattr(state, "media_pipeline", None)
    monkeypatch.setattr(
        "app.services.media.pipeline.build_default_pipeline", lambda _: pipeline
    )
    try:
        async with lifespan(FastAPI()):
            await asyncio.wait_for(entered.wait(), 1)
        worker = created_workers[0]
        assert not bot_app.closed and not bot_app.stopped
        assert not store.closed and not redis.closed
        assert state.job_store is store
        assert state.bot_app is bot_app
        assert state.media_pipeline is pipeline
        assert routes._job_store is store
        assert not worker.shutdown_complete
        successor_bot = None
        successor_store = None
        if overlap_lifespan:
            successor_bot = BotApp()
            successor_store = RecordingStore(store.path)
            monkeypatch.setattr(main_module, "JobStore", lambda: successor_store)
            monkeypatch.setattr(
                main_module,
                "_build_telegram_application_builder",
                lambda: SimpleNamespace(build=lambda: successor_bot),
            )
            # Actual overlapping lifespan, same persistent SQLite and shared
            # Redis. Rejection must precede all resource/binding acquisition.
            with pytest.raises(RuntimeError):
                async with lifespan(FastAPI()):
                    raise AssertionError("overlapping runtime must not start")
            assert not redis.closed and redis.close_calls == 0
            await redis.ping()
            assert state.bot_app is bot_app
            assert state.job_store is store
            assert state.media_pipeline is pipeline
            assert routes._job_store is store
            assert not bot_app.closed and not store.closed
            assert not successor_bot.initialized and not successor_bot.closed
            assert not successor_store._initialized and not successor_store.closed
            assert len(created_workers) == 1
        if replacement_runtime:
            replacement_bot = BotApp()
            replacement_store = RecordingStore(tmp_path / "replacement.sqlite3")
            state.bot_app = replacement_bot
            state.media_pipeline = replacement_pipeline
            state.job_store = replacement_store
            routes.configure_job_store(replacement_store)
        finish.set()
        await asyncio.wait_for(dependencies_used.wait(), 1)
        async with asyncio.timeout(1):
            while not worker.shutdown_complete:
                await asyncio.sleep(0.005)
            while not bot_app.closed:
                await asyncio.sleep(0.005)
        assert store.closed
        assert bot_app.stop_calls == bot_app.shutdown_calls == 1
        assert store.close_calls == 1
        assert redis.close_calls == (0 if replacement_runtime else 1)
        assert state.bot_app is (replacement_bot if replacement_runtime else None)
        assert state.media_pipeline is (
            replacement_pipeline if replacement_runtime else None
        )
        assert state.job_store is (replacement_store if replacement_runtime else None)
        assert routes._job_store is (replacement_store if replacement_runtime else None)
        if overlap_lifespan:
            cleanup = main_module._deferred_shutdown_tasks.get(worker)
            if cleanup is not None:
                await cleanup
            assert redis.close_calls == 1
            # The rejected successor can acquire the runtime after the old
            # handler and its deferred cleanup have actually finished.
            successor_bot.process_update = AsyncMock()
            async with lifespan(FastAPI()):
                assert state.bot_app is successor_bot
                assert state.job_store is successor_store
                assert successor_bot.initialized
                assert not redis.closed
                assert len(created_workers) == 2
            successor_worker = created_workers[-1]
            await successor_worker.wait_stopped()
            successor_cleanup = main_module._deferred_shutdown_tasks.get(
                successor_worker
            )
            if successor_cleanup is not None:
                await successor_cleanup
            assert successor_bot.closed and successor_store.closed
            # One close for each separate successful lifespan; denied overlap
            # performed no close or other resource acquisition.
            assert redis.close_calls == 2
        if replacement_runtime:
            # Lifespans adopt the same import-time Redis client. Old cleanup
            # must leave it usable until the replacement runtime shuts down.
            assert state.redis_client is redis
            await redis.ping()
            replacement_controller = drain_module.DrainController(replacement_store)
            replacement_worker = RecordingWorker(
                replacement_store,
                replacement_controller,
                lambda payload: asyncio.sleep(0),
            )
            await replacement_worker.start()
            await shutdown_runtime(
                drain_controller=replacement_controller,
                durable_worker=replacement_worker,
                stop_event=asyncio.Event(),
                background_tasks=(),
                bot_app=replacement_bot,
                webhook_enabled=True,
                job_store=replacement_store,
                redis_client=redis,
                drain_timeout_seconds=0,
            )
            await replacement_worker.wait_stopped()
            async with asyncio.timeout(1):
                while not replacement_bot.closed or not redis.closed:
                    await asyncio.sleep(0.005)
            assert replacement_bot.closed and replacement_store.closed
            assert redis.closed and redis.close_calls == 1
            assert state.bot_app is state.job_store is state.media_pipeline is None
            assert routes._job_store is None
    finally:
        finish.set()
        for worker in created_workers:
            if worker._task is not None:
                await asyncio.gather(worker._task, return_exceptions=True)
            await worker.stop()
            await worker.wait_stopped()
            cleanup = main_module._deferred_shutdown_tasks.get(worker)
            if cleanup is not None:
                await asyncio.gather(cleanup, return_exceptions=True)
        routes.configure_job_store(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["builder", "worker", "webhook", "polling"])
async def test_lifespan_cleans_partial_startup_failure(
    tmp_path, monkeypatch, failure_phase
):
    class RecordingStore(JobStore):
        close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    store = RecordingStore(tmp_path / "jobs.sqlite3")
    created_workers = []
    background_tasks = []
    background_stops = []

    class RecordingWorker(main_module.DurableUpdateWorker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            created_workers.append(self)

        async def start(self) -> None:
            await super().start()
            if failure_phase == "worker":
                raise OSError("worker startup failed after acquiring lease")

    async def background(stop_event: asyncio.Event) -> None:
        background_tasks.append(asyncio.current_task())
        background_stops.append(stop_event)
        await stop_event.wait()

    bot = SimpleNamespace(
        set_webhook=AsyncMock(
            side_effect=(
                OSError("setWebhook unavailable")
                if failure_phase == "webhook"
                else None
            )
        ),
        delete_webhook=AsyncMock(),
    )
    updater = SimpleNamespace(
        start_polling=AsyncMock(
            side_effect=(
                OSError("polling unavailable") if failure_phase == "polling" else None
            )
        ),
        stop=AsyncMock(),
    )
    bot_app = SimpleNamespace(
        bot=bot,
        updater=updater,
        add_handler=MagicMock(),
        add_error_handler=MagicMock(),
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        process_update=AsyncMock(),
    )
    builder = SimpleNamespace(build=lambda: bot_app)

    def build_application():
        if failure_phase == "builder":
            raise OSError("application builder unavailable")
        return builder

    pipeline = object()
    monkeypatch.setattr(main_module, "JobStore", lambda: store)
    monkeypatch.setattr(main_module, "DurableUpdateWorker", RecordingWorker)
    monkeypatch.setattr(main_module, "janitor_loop", background)
    monkeypatch.setattr(
        main_module, "_build_telegram_application_builder", build_application
    )
    monkeypatch.setattr(
        main_module.config,
        "WEBHOOK_URL",
        "" if failure_phase == "polling" else "https://bot.example",
    )
    monkeypatch.setattr(main_module.config, "TELEGRAM_LOCAL_ENDPOINT", None)
    monkeypatch.setattr(main_module.state, "redis_client", None)
    monkeypatch.setattr(main_module.state, "bot_app", None)
    monkeypatch.setattr(
        "app.services.media.pipeline.build_default_pipeline",
        lambda _: pipeline,
    )

    try:
        with pytest.raises(OSError):
            async with lifespan(FastAPI()):
                raise AssertionError("startup failure must happen before yield")
        await asyncio.sleep(0)

        assert routes._job_store is None
        if failure_phase == "builder":
            assert created_workers == []
            assert background_tasks == []
            assert store._initialized is False
            assert store.close_calls == 0
        else:
            worker = created_workers[0]
            assert worker._task is None
            # Runtime dependency updates belong to the immutable image build;
            # only the janitor may be launched as a background maintenance task.
            assert len(background_tasks) == 1
            assert all(task.done() for task in background_tasks)
            assert store.close_calls == 1
        assert await store.acquire_worker("worker-b") is True
        await store.release_worker("worker-b")
        if failure_phase == "builder":
            bot_app.stop.assert_not_awaited()
            bot_app.shutdown.assert_not_awaited()
        else:
            bot_app.stop.assert_awaited_once()
            bot_app.shutdown.assert_awaited_once()
        assert state.media_pipeline is None
        assert state.bot_app is None
    finally:
        routes.configure_job_store(None)
        for stop_event in background_stops:
            stop_event.set()
        await asyncio.gather(
            *(task for task in background_tasks if task is not None),
            return_exceptions=True,
        )
        for worker in created_workers:
            if worker._task is not None:
                await worker.stop()
        state.media_pipeline = None
        state.bot_app = None


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


def video_asset(path: Path, *, media_id: str = "video-1") -> DeliveryAsset:
    return DeliveryAsset(
        item=MediaItem(media_id, MediaKind.VIDEO, "https://media.example/video-1"),
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


@pytest.mark.asyncio
async def test_provider_change_after_crash_uses_stable_request_operation_key(tmp_path):
    class CrashWindowBot(RecordingBot):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def send_video(self, **kwargs: object) -> object:
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await asyncio.Event().wait()
            video = SimpleNamespace(file_id="file-3", file_unique_id="unique-3")
            return SimpleNamespace(message_id=93, video=video)

    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 804})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    path = tmp_path / "rerace.mp4"
    path.write_bytes(b"video")
    request = MediaRequest.from_url("https://youtube.com/watch?v=stable-id")
    bot = CrashWindowBot()
    delivery = TelegramDelivery(bot, media_dir=tmp_path, local_mode=True)
    target = DeliveryTarget("42")

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        interrupted = asyncio.create_task(
            delivery.deliver(
                video_asset(path, media_id="provider-a-id"),
                target,
                request=request,
            )
        )
        await asyncio.wait_for(bot.entered.wait(), timeout=1)
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted

    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        replay = await delivery.deliver(
            video_asset(path, media_id="provider-b-id"),
            target,
            request=request,
        )

    assert replay.status is DeliveryStatus.UNCERTAIN
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_cached_gif_network_error_is_uncertain_without_generated_fallback(
    tmp_path, monkeypatch
):
    class ForbiddenFileCache:
        def get(self, key):
            raise AssertionError("cached uncertain delivery must not generate a GIF")

    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 805})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    token = "cached-gif"
    bot = MagicMock()
    bot.send_document = AsyncMock(side_effect=NetworkError("upload disconnected"))
    update, context = gif_callback(token, bot)
    monkeypatch.setattr(state, "link_cache", AsyncCache())
    monkeypatch.setattr(state, "gifdoc_cache", AsyncCache({f"gifdoc:{token}": "fid"}))
    monkeypatch.setattr(state, "file_cache", ForbiddenFileCache())

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        await callbacks.on_save_as_gif_file(update, context)

    key = f"gif-document:42:gifdoc:{token}"
    assert (
        await store.delivery_outcome("805", item_key=key) is DeliveryOutcome.UNCERTAIN
    )
    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        await callbacks.on_save_as_gif_file(update, context)

    assert bot.send_document.await_count == 1


@pytest.mark.asyncio
async def test_cached_gif_receipt_write_failure_propagates_without_regeneration(
    tmp_path, monkeypatch
):
    class FailingFinalizationStore(JobStore):
        def _finalize_delivery_attempt_sync(self, *args, **kwargs):
            raise sqlite3.OperationalError("synthetic receipt write failure")

    store = FailingFinalizationStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 901})
    claimed = await store.claim_next("worker-a")
    assert claimed is not None
    token = "cached-gif-receipt-failure"
    sent = SimpleNamespace(
        message_id=95, document=SimpleNamespace(file_id="cached-fid")
    )
    bot = MagicMock()
    bot.send_document = AsyncMock(return_value=sent)
    bot.get_file = AsyncMock(
        side_effect=AssertionError("receipt write failure must not recover a source")
    )
    update, context = gif_callback(token, bot)
    file_cache = MagicMock(spec=dict)
    file_cache.get.return_value = None
    convert = AsyncMock(
        side_effect=AssertionError("receipt write failure must not regenerate a GIF")
    )
    monkeypatch.setattr(state, "link_cache", AsyncCache())
    monkeypatch.setattr(
        state, "gifdoc_cache", AsyncCache({f"gifdoc:{token}": "cached-fid"})
    )
    monkeypatch.setattr(state, "file_cache", file_cache)
    monkeypatch.setattr(
        "app.services.converter.MediaConverter.convert_to_native_gif", convert
    )

    with (
        delivery_job_context(store, claimed.id, owner_id="worker-a"),
        pytest.raises(
            sqlite3.OperationalError, match="synthetic receipt write failure"
        ),
    ):
        await callbacks.on_save_as_gif_file(update, context)

    assert bot.send_document.await_count == 1
    key = f"gif-document:42:gifdoc:{token}"
    assert (
        await store.delivery_outcome(claimed.id, item_key=key)
        is DeliveryOutcome.UNCERTAIN
    )
    file_cache.get.assert_not_called()
    bot.get_file.assert_not_awaited()
    convert.assert_not_awaited()
    update.callback_query.message.reply_text.assert_not_awaited()
    # The initial spinner is allowed; no done/error/restored button follows the failure.
    update.callback_query.message.edit_reply_markup.assert_awaited_once()

    # Reservation uncertainty remains authoritative despite the finalization fault.
    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        outcome, replay_sent = await callbacks._send_gif_document_durable(
            bot, key, chat_id=42, document="cached-fid"
        )
    assert outcome is DeliveryOutcome.UNCERTAIN
    assert replay_sent is None
    assert bot.send_document.await_count == 1


@pytest.mark.asyncio
async def test_generated_gif_network_error_is_uncertain_and_not_replayed(
    tmp_path, monkeypatch
):
    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 806})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    token = "generated-gif"
    gif_path = tmp_path / "ready.gif"
    gif_path.write_bytes(b"GIF89a")
    bot = MagicMock()
    bot.send_document = AsyncMock(side_effect=NetworkError("upload disconnected"))
    update, context = gif_callback(token, bot)
    monkeypatch.setattr(state, "link_cache", AsyncCache())
    monkeypatch.setattr(state, "gifdoc_cache", AsyncCache())
    monkeypatch.setattr(state, "file_cache", {token: str(gif_path)})
    monkeypatch.setattr(state, "processing_gifs", set())

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        await callbacks.on_save_as_gif_file(update, context)

    key = f"gif-document:42:gifdoc:{token}"
    assert (
        await store.delivery_outcome("806", item_key=key) is DeliveryOutcome.UNCERTAIN
    )
    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        await callbacks.on_save_as_gif_file(update, context)

    assert bot.send_document.await_count == 1


@pytest.mark.asyncio
async def test_generated_gif_receipt_write_failure_propagates_and_cleans_owned_output(
    tmp_path, monkeypatch
):
    class FailingFinalizationStore(JobStore):
        def _finalize_delivery_attempt_sync(self, *args, **kwargs):
            raise sqlite3.OperationalError("synthetic receipt write failure")

    store = FailingFinalizationStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 902})
    claimed = await store.claim_next("worker-a")
    assert claimed is not None
    token = "generated-gif-receipt-failure"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic input video")
    generated = tmp_path / "generated.gif"
    generated.write_bytes(b"GIF89a")
    sent = SimpleNamespace(
        message_id=96, document=SimpleNamespace(file_id="generated-fid")
    )
    bot = MagicMock()
    bot.send_document = AsyncMock(return_value=sent)
    update, context = gif_callback(token, bot)
    gifdoc_cache = AsyncCache()
    processing = set()
    convert = AsyncMock(return_value=str(generated))
    monkeypatch.setattr(state, "link_cache", AsyncCache())
    monkeypatch.setattr(state, "gifdoc_cache", gifdoc_cache)
    monkeypatch.setattr(state, "file_cache", {token: str(source)})
    monkeypatch.setattr(state, "processing_gifs", processing)
    monkeypatch.setattr(state, "gif_file_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(
        "app.services.converter.MediaConverter.convert_to_native_gif", convert
    )

    with (
        delivery_job_context(store, claimed.id, owner_id="worker-a"),
        pytest.raises(
            sqlite3.OperationalError, match="synthetic receipt write failure"
        ),
    ):
        await callbacks.on_save_as_gif_file(update, context)

    assert bot.send_document.await_count == 1
    key = f"gif-document:42:gifdoc:{token}"
    assert (
        await store.delivery_outcome(claimed.id, item_key=key)
        is DeliveryOutcome.UNCERTAIN
    )
    convert.assert_awaited_once_with(str(source))
    assert not generated.exists()
    assert source.read_bytes() == b"synthetic input video"
    assert processing == set()
    assert gifdoc_cache == {}
    update.callback_query.message.reply_text.assert_not_awaited()
    update.callback_query.message.edit_reply_markup.assert_awaited_once()

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        outcome, replay_sent = await callbacks._send_gif_document_durable(
            bot, key, chat_id=42, document="generated-fid"
        )
    assert outcome is DeliveryOutcome.UNCERTAIN
    assert replay_sent is None
    assert bot.send_document.await_count == 1


@pytest.mark.asyncio
async def test_rejected_cached_gif_is_recorded_failed_then_generated_once(
    tmp_path, monkeypatch
):
    store = JobStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 807})
    claimed = await store.claim_next("worker-a")
    assert claimed is not None
    token = "rejected-cached-gif"
    gif_path = tmp_path / "replacement.gif"
    gif_path.write_bytes(b"GIF89a")
    sent = SimpleNamespace(
        message_id=94,
        document=SimpleNamespace(file_id="replacement-file-id"),
    )
    bot = MagicMock()
    bot.send_document = AsyncMock(
        side_effect=[BadRequest("wrong file identifier"), sent]
    )
    update, context = gif_callback(token, bot)
    gifdoc_cache = AsyncCache({f"gifdoc:{token}": "stale-file-id"})
    monkeypatch.setattr(state, "link_cache", AsyncCache())
    monkeypatch.setattr(state, "gifdoc_cache", gifdoc_cache)
    monkeypatch.setattr(state, "file_cache", {token: str(gif_path)})
    monkeypatch.setattr(state, "processing_gifs", set())

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        await callbacks.on_save_as_gif_file(update, context)

    key = f"gif-document:42:gifdoc:{token}"
    assert await store.delivery_outcome("807", item_key=key) is DeliveryOutcome.SUCCESS
    assert bot.send_document.await_count == 2
    assert gifdoc_cache[f"gifdoc:{token}"] == "replacement-file-id"


@pytest.mark.asyncio
async def test_compatibility_sender_operation_key_survives_temp_path_change(tmp_path):
    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 808})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    first_path = tmp_path / "provider-a-uuid.mp4"
    second_path = tmp_path / "provider-b-uuid.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    bot = RecordingBot(error=NetworkError("upload disconnected"))

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        first = await TelegramSender.send_file(
            bot,
            42,
            str(first_path),
            operation_key="youtube:https://youtu.be/stable:video",
        )

    assert first.status is DeliveryStatus.UNCERTAIN
    operation_digest = hashlib.sha256(
        b"compat-file:video:youtube:https://youtu.be/stable:video"
    ).hexdigest()
    assert (
        await store.delivery_outcome(
            "808", item_key=f"42:operation:{operation_digest}:0"
        )
        is DeliveryOutcome.UNCERTAIN
    )
    assert (
        await store.delivery_outcome("808", item_key="42:0:provider-a-uuid.mp4") is None
    )
    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        replay = await TelegramSender.send_file(
            bot,
            42,
            str(second_path),
            operation_key="youtube:https://youtu.be/stable:video",
        )

    assert replay.status is DeliveryStatus.UNCERTAIN
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_slideshow_operation_key_survives_generated_uuid_paths(tmp_path):
    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update({"update_id": 809})
    claimed = await store.claim_next("worker-a", lease_seconds=1)
    assert claimed is not None
    first_images = []
    second_images = []
    for generation, paths in (("a", first_images), ("b", second_images)):
        for index in range(2):
            path = tmp_path / f"{generation}-uuid-{index}.jpg"
            path.write_bytes(b"image")
            paths.append(str(path))
    bot = MagicMock()
    bot.send_media_group = AsyncMock(side_effect=NetworkError("upload disconnected"))

    with delivery_job_context(store, claimed.id, owner_id="worker-a"):
        first = await TelegramSender.send_slideshow_photos(
            bot,
            42,
            first_images,
            operation_key="slideshow:https://example.test/post:photos",
        )

    assert first.status is DeliveryStatus.UNCERTAIN
    operation_digest = hashlib.sha256(
        b"compat-slideshow-photos:slideshow:https://example.test/post:photos"
    ).hexdigest()
    assert (
        await store.delivery_outcome(
            "809", item_key=f"42:operation:{operation_digest}:0"
        )
        is DeliveryOutcome.UNCERTAIN
    )
    assert await store.delivery_outcome("809", item_key="42:0:photo:0") is None
    now[0] += 2
    recovered = await store.claim_next("worker-b")
    assert recovered is not None
    with delivery_job_context(store, recovered.id, owner_id="worker-b"):
        replay = await TelegramSender.send_slideshow_photos(
            bot,
            42,
            second_images,
            operation_key="slideshow:https://example.test/post:photos",
        )

    assert replay.status is DeliveryStatus.UNCERTAIN
    assert bot.send_media_group.await_count == 1
