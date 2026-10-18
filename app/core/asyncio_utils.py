"""Settle owned asynchronous work before releasing its resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def await_owned[T](operation: Awaitable[T]) -> T:
    """Join an owned operation across cancellation, then restore cancellation.

    In particular, cancelling an asyncio wrapper cannot stop its disk worker
    thread. The caller must retain handles and paths until that worker settles.
    """
    task: asyncio.Future[T] = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except BaseException:
            if cancellation is None:
                raise
            break
    if cancellation is not None:
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()
