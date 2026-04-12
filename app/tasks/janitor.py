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

        # Clean bot-created files
        if not (
            name.startswith("ytdl_")
            or name.startswith("concat_")
            or name.startswith("tikwm_")
            or name.startswith("gdl_video_")
            or name.startswith("info_")
        ):
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


def _aggressive_purge_temp() -> int:
    """Delete ALL bot-created files regardless of age. Used in critical disk state."""
    deleted = 0
    if not os.path.isdir(TEMP_DIR):
        return deleted
    for name in os.listdir(TEMP_DIR):
        path = os.path.join(TEMP_DIR, name)
        if name.startswith("slideshow_") and os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
                deleted += 1
            except Exception:
                pass
            continue
        if not (
            name.startswith("ytdl_")
            or name.startswith("concat_")
            or name.startswith("tikwm_")
            or name.startswith("gdl_video_")
            or name.startswith("info_")
        ):
            continue
        if os.path.isfile(path):
            safe_remove(path)
            deleted += 1
    return deleted


# 0 = ok, 1 = warning sent, 2 = critical sent
_last_alert_level: int = 0


async def _notify_admin(message: str) -> None:
    """Send a disk alert to ADMIN_CHAT_ID (best-effort, never raises)."""
    try:
        from app.core import config, state  # local import to avoid circular at module level

        if not config.ADMIN_CHAT_ID or not state.bot_app:
            return
        await state.bot_app.bot.send_message(
            chat_id=config.ADMIN_CHAT_ID,
            text=message,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Disk alert admin notification failed: %s", exc)


async def check_disk_space() -> None:
    """Check free disk space and take action based on thresholds."""
    global _last_alert_level

    from app.core import config, state  # local import

    try:
        usage = await asyncio.to_thread(shutil.disk_usage, TEMP_DIR)
    except OSError as exc:
        logger.warning("disk_usage failed: %s", exc)
        return

    free_pct = (usage.free / usage.total) * 100
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)

    logger.info(
        "Disk check: %.1f%% free (%.2f / %.2f GB)",
        free_pct, free_gb, total_gb,
    )

    if free_pct <= config.DISK_CRITICAL_PCT:
        # ── Critical: aggressive purge + maintenance mode ────────────────
        state.disk_critical = True
        purged = await asyncio.to_thread(_aggressive_purge_temp)
        logger.error(
            "DISK CRITICAL: %.1f%% free — aggressive purge removed %d files, maintenance mode ON",
            free_pct, purged,
        )
        if _last_alert_level < 2:
            _last_alert_level = 2
            await _notify_admin(
                f"🚨 <b>DISK CRITICAL</b>\n\n"
                f"Свободно: <b>{free_pct:.1f}%</b> ({free_gb:.2f} ГБ из {total_gb:.2f} ГБ)\n"
                f"Удалено временных файлов: {purged}\n\n"
                f"Бот перешёл в режим обслуживания — новые загрузки приостановлены."
            )

    elif free_pct <= config.DISK_WARNING_PCT:
        # ── Warning: alert only ──────────────────────────────────────────
        logger.warning("DISK WARNING: %.1f%% free (%.2f GB)", free_pct, free_gb)
        if _last_alert_level < 1:
            _last_alert_level = 1
            await _notify_admin(
                f"⚠️ <b>Disk Warning</b>\n\n"
                f"Свободно: <b>{free_pct:.1f}%</b> ({free_gb:.2f} ГБ из {total_gb:.2f} ГБ)\n\n"
                f"При достижении {config.DISK_CRITICAL_PCT}% бот перейдёт в\n"
                f"режим обслуживания."
            )

    else:
        # ── Recovery ─────────────────────────────────────────────────────
        if state.disk_critical:
            state.disk_critical = False
            logger.info("Disk space recovered to %.1f%% — maintenance mode OFF", free_pct)
            await _notify_admin(
                f"✅ <b>Disk Recovered</b>\n\n"
                f"Свободно: <b>{free_pct:.1f}%</b> ({free_gb:.2f} ГБ)\n"
                f"Режим обслуживания снят."
            )
        _last_alert_level = 0


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

        await check_disk_space()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=JANITOR_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
