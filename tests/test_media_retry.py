"""A failed Telegram request can be repeated without resending its URL."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Message

from app.core import state
from app.core.storage.memory import MemoryStorage
from app.core.storage.redis_storage import RedisStorage
from app.services.media.models import (
    YOUTUBE_SHORT_DEFAULT_VARIANT,
    DeliveredItem,
    DeliveryReceipt,
    DeliveryStatus,
    MediaItem,
    MediaKind,
)
from app.services.media.pipeline import MediaPipelineError, build_media_request

URL = "https://www.youtube.com/shorts/R24MYBczPAY"


class WireStorage(MemoryStorage):
    """Exercise the production serialization used across callback updates."""

    async def set(self, key, value, ttl=None):
        await super().set(key, RedisStorage._encode(value), ttl)

    async def get(self, key, type_hint=None):
        value = await super().get(key)
        return RedisStorage._decode(value, type_hint) if value is not None else None


class Pipeline:
    def __init__(self, failures=1):
        self.failures = failures
        self.requests = []
        self.targets = []
        self.captions = []

    async def deliver(self, request, target, **options):
        self.requests.append(request)
        self.targets.append(target)
        self.captions.append(options.get("caption"))
        if self.failures:
            self.failures -= 1
            raise MediaPipelineError("ytdlp: media route extraction timed out")
        return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)


@pytest.fixture
def retry_env(monkeypatch, tmp_path):
    from app.core.job_store import JobStore

    pipeline = Pipeline()
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    store = JobStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(state, "job_store", store, raising=False)
    cache = WireStorage(maxsize=100, ttl=3600)
    monkeypatch.setattr(state, "link_cache", cache)
    monkeypatch.setattr(
        state,
        "limiter",
        SimpleNamespace(
            allow_user=AsyncMock(return_value=True),
            allow_chat=AsyncMock(return_value=True),
        ),
    )
    status = MagicMock(spec=Message)
    status.message_id = 100
    status.chat_id = 9
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    message = SimpleNamespace(
        text=URL,
        message_id=50,
        reply_text=AsyncMock(return_value=status),
        delete=AsyncMock(),
    )
    user = SimpleNamespace(id=7, username="tester", mention_html=lambda: "tester")
    update = SimpleNamespace(
        effective_user=user, effective_chat=SimpleNamespace(id=9), message=message
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(send_chat_action=AsyncMock(), delete_message=AsyncMock())
    )
    return SimpleNamespace(
        pipeline=pipeline,
        cache=cache,
        status=status,
        update=update,
        context=context,
        store=store,
    )


def query_for(env, data, *, user_id=7, chat_id=9, message_id=100):
    message = MagicMock(spec=Message)
    message.chat_id = chat_id
    message.message_id = message_id
    message.edit_text = env.status.edit_text
    message.delete = env.status.delete
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=message,
        answer=AsyncMock(),
    )


async def invoke_entrypoint(entrypoint, env, monkeypatch):
    from app.bot import callbacks, commands, group_logic, messages

    for module in (commands, group_logic, messages):
        monkeypatch.setattr(
            module, "extract_url_from_update", lambda message: (URL, "10-20")
        )
    monkeypatch.setattr(
        messages, "get_prefs", AsyncMock(return_value={"default_quality": 720})
    )

    class Queue:
        async def enqueue(self, *args, **kwargs):
            return True

        def release(self):
            pass

    monkeypatch.setattr(state, "download_queue", Queue())
    if entrypoint == "picker":
        from app.core.models import DownloadContext

        await env.cache.set(
            "picked", DownloadContext(page_url=URL, height=720, section="*10-20")
        )
        query = query_for(env, "m2|send|picked")
        query.edit_message_reply_markup = AsyncMock()
        query.edit_message_text = env.status.edit_text
        context = SimpleNamespace(bot=env.context.bot, user_data={})
        await callbacks.on_send(SimpleNamespace(callback_query=query), context)
        return
    handlers = {
        "private": messages.on_message,
        "group": group_logic.handle_group_message,
        "mp3": commands.cmd_mp3,
        "mp4": commands.cmd_mp4,
    }
    await handlers[entrypoint](env.update, env.context)


@pytest.mark.parametrize("entrypoint", ["private", "group", "mp3", "mp4"])
async def test_failure_button_repeats_the_original_request_in_the_same_message(
    entrypoint, retry_env, monkeypatch
):
    env = retry_env
    await invoke_entrypoint(entrypoint, env, monkeypatch)

    markup = env.status.edit_text.await_args.kwargs.get("reply_markup")
    assert markup is not None, "A failed request must offer a retry button"
    button = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("m2|retry|")
    )
    assert button.text == "🔄 Повторить"
    assert len(button.callback_data.encode()) <= 64

    from app.bot.retry import on_retry

    query = query_for(env, button.callback_data)
    await on_retry(SimpleNamespace(callback_query=query), env.context)

    assert env.pipeline.requests == [env.pipeline.requests[0], env.pipeline.requests[0]]
    assert env.pipeline.targets[1] == env.pipeline.targets[0]
    assert env.pipeline.captions[1] == env.pipeline.captions[0]
    assert env.update.message.reply_text.await_count == 1
    env.status.delete.assert_awaited_once()
    # Queued callbacks from the same button cannot send the media again.
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(env.pipeline.requests) == 2


@pytest.mark.parametrize("entrypoint", ["private", "group", "mp3", "mp4", "picker"])
async def test_extraction_failure_remains_retryable_in_the_durable_worker(
    entrypoint, retry_env, monkeypatch, tmp_path
):
    from app.bot.retry import on_retry
    from app.core.job_store import JobState, JobStore, delivery_job_context
    from tests.core.test_job_store import update_payload

    env = retry_env
    store = JobStore(tmp_path / "jobs.sqlite3")
    await store.accept_update(update_payload(1))
    original = await store.claim_next("worker")
    with delivery_job_context(store, original.id, owner_id="worker") as execution:
        await invoke_entrypoint(entrypoint, env, monkeypatch)
    assert isinstance(execution.error, MediaPipelineError)
    assert await store.fail(original.id, str(execution.error), owner_id="worker")

    markup = env.status.edit_text.await_args.kwargs["reply_markup"]
    button = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("m2|retry|")
    )
    await store.accept_update(update_payload(2))
    job = await store.claim_next("worker")
    query = query_for(env, button.callback_data)
    with delivery_job_context(store, job.id, owner_id="worker") as execution:
        await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert execution.error is None
    assert await store.complete(job.id, owner_id="worker")
    assert len(env.pipeline.requests) == 2
    assert (await store.get_update(1)).state is JobState.COMPLETED
    env.status.delete.assert_awaited_once()


async def test_retry_snapshot_preserves_all_media_parameters_and_drops_old_deadline(
    retry_env,
):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    env.pipeline.failures = 0
    original = build_media_request(
        URL,
        kind="audio",
        audio_format="mp3",
        audio_language="uk",
        quality=720,
        clip="10-20",
        album_selection=(2, 0),
        exact=True,
        caller_scope="command",
        output_variant=YOUTUBE_SHORT_DEFAULT_VARIANT,
        deadline=0,
    )
    markup = await save_retry_request(
        original, user_id=7, chat_id=9, message_id=100, caption="🎵"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    await on_retry(SimpleNamespace(callback_query=query), env.context)

    assert env.pipeline.requests == [replace(original, deadline=None)]


@pytest.mark.parametrize(
    "overrides", [{"user_id": 8}, {"chat_id": 10}, {"message_id": 101}]
)
async def test_retry_is_bound_to_the_request_author_chat_and_status(
    retry_env, overrides
):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    markup = await save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data, **overrides)
    await on_retry(SimpleNamespace(callback_query=query), env.context)

    assert not env.pipeline.requests
    env.status.edit_text.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_failed_retry_and_rate_limit_keep_the_button_usable(retry_env):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    markup = await save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    state.limiter.allow_user.return_value = False
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert not env.pipeline.requests
    env.status.edit_text.assert_not_awaited()

    state.limiter.allow_user.return_value = True
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    restored = env.status.edit_text.await_args.kwargs["reply_markup"]
    assert restored.inline_keyboard[0][0].callback_data == query.data
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(env.pipeline.requests) == 2
    env.status.delete.assert_awaited_once()


async def test_double_click_starts_only_one_retry(retry_env):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    entered, finish = asyncio.Event(), asyncio.Event()

    class SlowPipeline(Pipeline):
        async def deliver(self, request, target, **options):
            entered.set()
            await finish.wait()
            return await super().deliver(request, target, **options)

    env.pipeline = SlowPipeline(failures=0)
    state.media_pipeline = env.pipeline
    markup = await save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    first = asyncio.create_task(
        on_retry(SimpleNamespace(callback_query=query), env.context)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        await on_retry(SimpleNamespace(callback_query=query), env.context)
    finally:
        finish.set()
        await first
    assert len(env.pipeline.requests) == 1


async def test_delayed_cache_read_cannot_repeat_a_completed_retry(
    retry_env, monkeypatch
):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    entered, finish = asyncio.Event(), asyncio.Event()
    read_snapshot, return_snapshot = asyncio.Event(), asyncio.Event()

    class SlowPipeline(Pipeline):
        async def deliver(self, request, target, **options):
            entered.set()
            await finish.wait()
            return await super().deliver(request, target, **options)

    pipeline = SlowPipeline(failures=0)
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    markup = await save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    reads = 0
    real_get = env.cache.get

    async def delayed_get(key, type_hint=None):
        nonlocal reads
        raw = await real_get(key, type_hint)
        reads += 1
        if reads == 2:
            read_snapshot.set()
            await return_snapshot.wait()
        return raw

    monkeypatch.setattr(env.cache, "get", delayed_get)
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    first = asyncio.create_task(
        on_retry(SimpleNamespace(callback_query=query), env.context)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = asyncio.create_task(
        on_retry(SimpleNamespace(callback_query=query), env.context)
    )
    try:
        await asyncio.wait_for(read_snapshot.wait(), timeout=1)
        finish.set()
        await first
    finally:
        finish.set()
        return_snapshot.set()
        await asyncio.gather(first, second)
    assert len(pipeline.requests) == 1


@pytest.mark.parametrize(
    "data", ["m2|retry|missing", "m2|retry", "m2|retry|a|b", "m2|send|a"]
)
async def test_expired_or_malformed_retry_does_not_start_work(retry_env, data):
    from app.bot.retry import on_retry

    env = retry_env
    query = query_for(env, data)
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert not env.pipeline.requests
    assert query.answer.await_count == 1


@pytest.mark.parametrize("status", [DeliveryStatus.PARTIAL, DeliveryStatus.UNCERTAIN])
async def test_manual_retry_never_repeats_a_partial_or_uncertain_delivery(
    retry_env, status
):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    request = build_media_request(URL)

    class PartialPipeline(Pipeline):
        async def deliver(self, request, target, **kwargs):
            self.requests.append(request)
            items = (
                DeliveredItem(
                    MediaItem("one", MediaKind.VIDEO, URL),
                    DeliveryStatus.SUCCESS
                    if status is DeliveryStatus.PARTIAL
                    else DeliveryStatus.UNCERTAIN,
                    MediaKind.VIDEO,
                ),
                DeliveredItem(
                    MediaItem("two", MediaKind.VIDEO, URL),
                    DeliveryStatus.FAILED,
                    MediaKind.VIDEO,
                    item_index=1,
                    error="send failed",
                ),
            )
            return DeliveryReceipt(target, items, status)

    pipeline = PartialPipeline()
    state.media_pipeline = pipeline
    markup = await save_retry_request(
        request, user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert env.status.edit_text.await_args.kwargs["reply_markup"] is None
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(pipeline.requests) == 1


async def test_download_picker_failure_offers_a_retry_without_losing_download_links(
    retry_env, monkeypatch
):
    from app.bot import callbacks
    from app.bot.retry import on_retry
    from app.core.models import DownloadContext

    env = retry_env
    await env.cache.set(
        "picked", DownloadContext(page_url=URL, height=720, section="*10-20")
    )

    class Queue:
        async def enqueue(self, *args, **kwargs):
            return True

        def release(self):
            pass

    monkeypatch.setattr(state, "download_queue", Queue())
    query = query_for(env, "m2|send|picked")
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = env.status.edit_text
    context = SimpleNamespace(bot=env.context.bot, user_data={})
    await callbacks.on_send(SimpleNamespace(callback_query=query), context)

    markup = env.status.edit_text.await_args.kwargs["reply_markup"]
    assert any(button.url for row in markup.inline_keyboard for button in row)
    retry = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("m2|retry|")
    )
    await on_retry(
        SimpleNamespace(callback_query=query_for(env, retry.callback_data)), context
    )
    assert env.pipeline.requests == [env.pipeline.requests[0], env.pipeline.requests[0]]


async def test_failed_picker_retry_preserves_the_download_and_back_controls(
    retry_env, monkeypatch
):
    from app.bot import callbacks
    from app.bot.retry import on_retry
    from app.core.models import DownloadContext

    env = retry_env
    env.pipeline.failures = 2
    await env.cache.set("picked", DownloadContext(page_url=URL))
    monkeypatch.setattr(state.download_queue, "enqueue", AsyncMock(return_value=True))
    monkeypatch.setattr(state.download_queue, "release", lambda: None)
    query = query_for(env, "m2|send|picked")
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = env.status.edit_text
    context = SimpleNamespace(bot=env.context.bot, user_data={})
    await callbacks.on_send(SimpleNamespace(callback_query=query), context)
    original = env.status.edit_text.await_args.kwargs["reply_markup"]
    retry = original.inline_keyboard[0][0]
    await on_retry(
        SimpleNamespace(callback_query=query_for(env, retry.callback_data)), context
    )
    restored = env.status.edit_text.await_args.kwargs["reply_markup"]
    assert restored == original


async def test_lease_failure_during_telegram_send_does_not_offer_a_whole_retry(
    retry_env, monkeypatch
):
    from app.bot import messages
    from app.services.media.pipeline import MediaPipeline
    from app.services.media.registry import ProviderRegistry

    env = retry_env
    real_pipeline = MediaPipeline(
        ProviderRegistry([]), None, None, lease_renew_interval=0.001
    )

    def lost_lease():
        raise RuntimeError("lease lost")

    async def hanging_send():
        await asyncio.Event().wait()

    class PipelineWithLostLease:
        async def deliver(self, *args, **kwargs):
            return await real_pipeline._with_lease_renewals(
                (SimpleNamespace(renew_lease=lost_lease),), hanging_send()
            )

    monkeypatch.setattr(state, "media_pipeline", PipelineWithLostLease())
    monkeypatch.setattr(messages, "get_prefs", AsyncMock(return_value={}))
    await messages._deliver_private_pipeline(env.update, env.context, URL, None)
    assert env.status.edit_text.await_args.kwargs.get("reply_markup") is None


async def test_successful_retry_is_not_repeated_if_cache_cleanup_fails(
    retry_env, monkeypatch
):
    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    env.pipeline.failures = 0
    markup = await save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    monkeypatch.setattr(
        env.cache, "delete", AsyncMock(side_effect=OSError("cache unavailable"))
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(env.pipeline.requests) == 1


@pytest.mark.parametrize("status", [DeliveryStatus.SUCCESS, DeliveryStatus.UNCERTAIN])
async def test_polling_retry_remains_consumed_after_restart_and_cache_cleanup_failure(
    retry_env, monkeypatch, status
):
    from app.bot import retry
    from app.core.job_store import JobStore

    env = retry_env

    class DeliveredPipeline(Pipeline):
        async def deliver(self, request, target, **options):
            self.requests.append(request)
            return DeliveryReceipt(target, (), status)

    pipeline = DeliveredPipeline()
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    markup = await retry.save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    monkeypatch.setattr(
        env.cache, "delete", AsyncMock(side_effect=OSError("cache unavailable"))
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    await retry.on_retry(SimpleNamespace(callback_query=query), env.context)
    # A restarted process retains Redis and SQLite, but no in-memory tombstones.
    retry._finished_retries.clear()
    monkeypatch.setattr(state, "job_store", JobStore(env.store.path))
    await retry.on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(pipeline.requests) == 1


async def test_interrupted_polling_retry_cannot_send_again_after_restart(
    retry_env, monkeypatch
):
    from app.bot import retry
    from app.core.job_store import JobStore

    env = retry_env
    entered = asyncio.Event()

    class InterruptedPipeline(Pipeline):
        async def deliver(self, request, target, **options):
            self.requests.append(request)
            if len(self.requests) == 1:
                entered.set()
                await asyncio.Event().wait()
            return DeliveryReceipt(target, (), DeliveryStatus.SUCCESS)

    pipeline = InterruptedPipeline()
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    markup = await retry.save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    running = asyncio.create_task(
        retry.on_retry(SimpleNamespace(callback_query=query), env.context)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    retry._finished_retries.clear()
    monkeypatch.setattr(state, "job_store", JobStore(env.store.path))
    await retry.on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(pipeline.requests) == 1


@pytest.mark.parametrize("operation", ["answer", "edit_text"])
async def test_polling_retry_can_recover_a_telegram_error_before_media_started(
    retry_env, operation
):
    from telegram.error import NetworkError

    from app.bot.retry import on_retry, save_retry_request

    env = retry_env
    env.pipeline.failures = 0
    markup = await save_retry_request(
        build_media_request(URL), user_id=7, chat_id=9, message_id=100, caption="📹"
    )
    query = query_for(env, markup.inline_keyboard[0][0].callback_data)
    failing = query.answer if operation == "answer" else env.status.edit_text
    failing.side_effect = NetworkError("temporary Telegram error")
    with pytest.raises(NetworkError):
        await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert not env.pipeline.requests
    failing.side_effect = None
    await on_retry(SimpleNamespace(callback_query=query), env.context)
    assert len(env.pipeline.requests) == 1


@pytest.mark.parametrize("drop_advancement", [False, True])
async def test_manual_retry_retires_failed_jobs_before_worker_restart(
    retry_env, monkeypatch, tmp_path, drop_advancement
):
    from app.bot import messages
    from app.bot.retry import on_retry
    from app.core.job_store import (
        DeliveryOutcome,
        JobStore,
        begin_current_delivery,
        delivery_job_context,
        record_current_delivery,
    )
    from tests.core.test_job_store import update_payload

    env = retry_env
    store = JobStore(tmp_path / "jobs.sqlite3")
    await store.accept_update(update_payload(1))
    assert await store.acquire_worker("worker")
    original = await store.claim_next("worker")

    class DurablePipeline(Pipeline):
        async def deliver(self, request, target, **options):
            await begin_current_delivery(("media",))
            await record_current_delivery(
                "media",
                DeliveryOutcome.FAILED if self.failures else DeliveryOutcome.SUCCESS,
            )
            return await super().deliver(request, target, **options)

    pipeline = DurablePipeline(failures=2)
    monkeypatch.setattr(state, "media_pipeline", pipeline)
    monkeypatch.setattr(messages, "get_prefs", AsyncMock(return_value={}))
    with delivery_job_context(store, original.id, owner_id="worker"):
        await messages._deliver_private_pipeline(env.update, env.context, URL, None)
    assert await store.fail(original.id, "send failed", owner_id="worker")
    button = env.status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0][
        0
    ]
    query = query_for(env, button.callback_data)

    real_set = env.cache.set

    async def save_with_transient_failure(key, value, ttl=None):
        nonlocal drop_advancement
        if drop_advancement:
            # RedisStorage logs and swallows a failed SET in production.
            drop_advancement = False
            return
        await real_set(key, value, ttl)

    monkeypatch.setattr(env.cache, "set", save_with_transient_failure)
    for update_id in (2, 3):
        await store.accept_update(update_payload(update_id))
        job = await store.claim_next("worker")
        with delivery_job_context(store, job.id, owner_id="worker"):
            await on_retry(SimpleNamespace(callback_query=query), env.context)
        if update_id == 2:
            assert await store.fail(job.id, "send failed", owner_id="worker")
        else:
            assert await store.complete(job.id, owner_id="worker")

    assert len(pipeline.requests) == 3
    assert await store.requeue_failed_deliveries() == 0
    assert await store.claim_next("worker") is None
