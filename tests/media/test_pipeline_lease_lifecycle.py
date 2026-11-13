import asyncio
from types import SimpleNamespace

import pytest

from app.services.media.pipeline import MediaPipeline
from app.services.media.registry import ProviderRegistry


async def test_repeated_cancellation_joins_delivery_before_releasing_lease_owner():
    pipeline = MediaPipeline(ProviderRegistry(), SimpleNamespace(), SimpleNamespace())
    started = asyncio.Event()
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()
    settled = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish_cleanup.wait()
            settled.set()

    task = asyncio.create_task(pipeline._with_lease_renewals((), operation()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done(), (
            "the lease owner returned before delivery cleanup settled"
        )
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert settled.is_set()
    finally:
        finish_cleanup.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
