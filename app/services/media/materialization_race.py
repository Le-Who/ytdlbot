"""Race complete route attempts and transfer ownership of one verified result."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from app.core.asyncio_utils import await_owned
from app.core.metrics import metrics

from .models import ResolvedMedia
from .transport import MaterializedItem

Result = tuple[ResolvedMedia, MaterializedItem]
Attempt = Callable[[], Awaitable[Result]]


class MaterializationRaceError(RuntimeError):
    """Every route failed before producing verified media."""


async def race_materializations(attempts: Sequence[Attempt]) -> Result:
    tasks = [asyncio.ensure_future(attempt()) for attempt in attempts]
    pending = set(tasks)
    winner: Result | None = None
    failures: list[Exception] = []

    async def discard_losers() -> None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cleanup_error: BaseException | None = None
        for result in results:
            if isinstance(result, tuple) and result is not winner:
                item = result[1]
                metrics.race_wasted_bytes.inc(
                    item.size_bytes, stage="youtube_route", outcome="lost"
                )
                try:
                    await item.release(delete=True)
                except BaseException as error:  # noqa: BLE001 - release every owned loser
                    cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error

    try:
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in tasks:
                    if task not in done:
                        continue
                    try:
                        result = task.result()
                    except Exception as error:  # noqa: BLE001 - retain the other route
                        failures.append(error)
                    else:
                        if winner is None:
                            winner = result
                if winner is not None:
                    break
        finally:
            for task in pending:
                task.cancel()
            await await_owned(discard_losers())
    except BaseException:
        if winner is not None:
            await await_owned(winner[1].release(delete=True))
        raise
    if winner is None:
        details = "; ".join(str(error) for error in failures)
        raise MaterializationRaceError(details or "no eligible media route")
    return winner
