import asyncio
import os
import shutil
import time
import logging

from app.core.config import TEMP_DIR, MAX_TEMP_AGE_SECONDS, JANITOR_INTERVAL_SECONDS
from app.core.utils import safe_remove

logger = logging.getLogger("app.tasks.janitor")


def cleanup_temp_dir() -> tuple[int, int]:
    now = time.time()
    deleted = 0
    orphan = 0
    if not os.path.isdir(TEMP_DIR):
        return deleted, orphan

    for name in os.listdir(TEMP_DIR):
        path = os.path.join(TEMP_DIR, name)

        # Clean slideshow directories (slideshow_* subdirs)
        if name.startswith("slideshow_") and os.path.isdir(path):
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age > MAX_TEMP_AGE_SECONDS:
                orphan += 1
                try:
                    shutil.rmtree(path, ignore_errors=True)
                    deleted += 1
                except Exception:
                    pass
            continue

        # Clean bot-created files: ytdl_* and concat_*
        if not (name.startswith("ytdl_") or name.startswith("concat_")):
            continue
        if not os.path.isfile(path):
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age > MAX_TEMP_AGE_SECONDS:
            orphan += 1
            safe_remove(path)
            deleted += 1

    return deleted, orphan


async def janitor_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        deleted, orphan = await asyncio.to_thread(cleanup_temp_dir)
        logger.info(
            "janitor run",
            extra={
                "op": "janitor",
                "deleted_files_total": deleted,
                "orphan_files_count": orphan,
            },
        )
        try:
            from app.core.metrics import metrics as app_metrics
            app_metrics.log_summary()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=JANITOR_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
