import asyncio
import os
import time
import logging
from dataclasses import dataclass

from app.core.config import TEMP_DIR, JANITOR_INTERVAL_SEC, MAX_TEMP_AGE_SEC, DISK_USAGE_HIGH_WATERMARK
from app.core.utils import safe_remove

logger = logging.getLogger("app.tasks.janitor")


@dataclass
class JanitorMetrics:
    deleted_files_total: int = 0
    orphan_files_count: int = 0


metrics = JanitorMetrics()


def _should_remove(path: str, now: float) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (now - st.st_mtime) > MAX_TEMP_AGE_SEC


def run_cleanup_once() -> tuple[int, int]:
    now = time.time()
    deleted = 0
    orphan = 0

    try:
        disk_usage = os.statvfs(TEMP_DIR)
        used_ratio = 1.0 - (disk_usage.f_bavail / disk_usage.f_blocks)
    except OSError:
        used_ratio = 0.0

    for name in os.listdir(TEMP_DIR):
        path = os.path.join(TEMP_DIR, name)
        if not os.path.isfile(path):
            continue

        if _should_remove(path, now) or used_ratio >= DISK_USAGE_HIGH_WATERMARK:
            safe_remove(path)
            deleted += 1
        else:
            orphan += 1

    metrics.deleted_files_total += deleted
    metrics.orphan_files_count = orphan
    return deleted, orphan


async def janitor_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            deleted, orphan = await asyncio.to_thread(run_cleanup_once)
            logger.info("janitor_pass", extra={"deleted_files_total": deleted, "orphan_files_count": orphan})
        except Exception:
            logger.exception("janitor_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=JANITOR_INTERVAL_SEC)
        except asyncio.TimeoutError:
            continue
