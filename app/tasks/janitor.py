import asyncio
import logging
import os
import shutil
import time

from app.core.config import (
    JANITOR_INTERVAL_SECONDS,
    MAX_TEMP_AGE_SECONDS,
    MEDIA_DIR,
    TEMP_DIR,
)
from app.core.resource_budget import (
    LeaseOwnershipError,
    is_active_media_lease,
    media_lease_lock,
    media_lease_path,
)
from app.core.utils import safe_remove

logger = logging.getLogger("app.tasks.janitor")


def cleanup_temp_dir(root: str | None = None) -> tuple[int, int]:
    target_dir = root or TEMP_DIR
    now = time.time()
    deleted = 0
    orphan = 0
    if not os.path.isdir(target_dir):
        return deleted, orphan

    for name in os.listdir(target_dir):
        path = os.path.join(target_dir, name)

        if _is_lease_auxiliary(name):
            _cleanup_lease_auxiliary(target_dir, name, now)
            continue

        if name.endswith(".lease"):
            target = path.removesuffix(".lease")
            _cleanup_inactive_lease(target)
            continue

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
            or name.startswith("media_")
        ):
            continue
        if not os.path.isfile(path):
            continue
        if _delete_media_target(path, now=now, require_old=True):
            orphan += 1
            deleted += 1

    return deleted, orphan


def cleanup_media_dirs() -> tuple[int, int]:
    """Clean legacy temporary files and the configured transport media root."""
    deleted = orphan = 0
    for root in dict.fromkeys((TEMP_DIR, MEDIA_DIR)):
        root_deleted, root_orphan = cleanup_temp_dir(root)
        deleted += root_deleted
        orphan += root_orphan
    return deleted, orphan


def _aggressive_purge_temp(root: str | None = None) -> int:
    """Delete ALL bot-created files regardless of age. Used in critical disk state."""
    target_dir = root or TEMP_DIR
    deleted = 0
    if not os.path.isdir(target_dir):
        return deleted
    for name in os.listdir(target_dir):
        path = os.path.join(target_dir, name)
        if _is_lease_auxiliary(name):
            _cleanup_lease_auxiliary(target_dir, name, time.time())
            continue
        if name.endswith(".lease"):
            target = path.removesuffix(".lease")
            _cleanup_inactive_lease(target)
            continue
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
            or name.startswith("media_")
        ):
            continue
        if os.path.isfile(path) and _delete_media_target(
            path, now=time.time(), require_old=False
        ):
            deleted += 1
    return deleted


def _aggressive_purge_media_dirs() -> int:
    return sum(
        _aggressive_purge_temp(root) for root in dict.fromkeys((TEMP_DIR, MEDIA_DIR))
    )


def _is_lease_auxiliary(name: str) -> bool:
    return name.endswith(".lease.lock") or (".lease." in name and name.endswith(".tmp"))


def _cleanup_lease_auxiliary(target_dir: str, name: str, now: float) -> None:
    path = os.path.join(target_dir, name)
    target_name = (
        name.removesuffix(".lease.lock")
        if name.endswith(".lease.lock")
        else name.split(".lease.", 1)[0]
    )
    target = os.path.join(target_dir, target_name)
    try:
        with media_lease_lock(target, stale_after=MAX_TEMP_AGE_SECONDS):
            if name.endswith(".lease.lock"):
                return
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                return
            if age > MAX_TEMP_AGE_SECONDS:
                safe_remove(path)
    except LeaseOwnershipError:
        return


def _cleanup_inactive_lease(target: str) -> None:
    try:
        with media_lease_lock(target, stale_after=MAX_TEMP_AGE_SECONDS):
            if not is_active_media_lease(target):
                media_lease_path(target).unlink(missing_ok=True)
    except LeaseOwnershipError:
        return


def _delete_media_target(path: str, *, now: float, require_old: bool) -> bool:
    try:
        with media_lease_lock(path, stale_after=MAX_TEMP_AGE_SECONDS):
            if not os.path.isfile(path):
                return False
            if require_old:
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    return False
                if age <= MAX_TEMP_AGE_SECONDS:
                    return False
            if is_active_media_lease(path):
                return False
            safe_remove(path)
            if os.path.exists(path):
                return False
            media_lease_path(path).unlink(missing_ok=True)
            return True
    except LeaseOwnershipError:
        return False


# 0 = ok, 1 = warning sent, 2 = critical sent
_last_alert_level: int = 0


async def _notify_admin(message: str) -> None:
    """Send a disk alert to ADMIN_CHAT_ID (best-effort, never raises)."""
    try:
        from app.core import (
            config,
            state,
        )  # local import to avoid circular at module level

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
        usage = await asyncio.to_thread(shutil.disk_usage, MEDIA_DIR)
    except OSError as exc:
        logger.warning("disk_usage failed: %s", exc)
        return

    free_pct = (usage.free / usage.total) * 100
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)

    logger.info(
        "Disk check: %.1f%% free (%.2f / %.2f GB)",
        free_pct,
        free_gb,
        total_gb,
    )

    if free_pct <= config.DISK_CRITICAL_PCT:
        # ── Critical: aggressive purge + maintenance mode ────────────────
        state.disk_critical = True
        purged = await asyncio.to_thread(_aggressive_purge_media_dirs)
        logger.error(
            "DISK CRITICAL: %.1f%% free — aggressive purge removed %d files, maintenance mode ON",
            free_pct,
            purged,
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
            logger.info(
                "Disk space recovered to %.1f%% — maintenance mode OFF", free_pct
            )
            await _notify_admin(
                f"✅ <b>Disk Recovered</b>\n\n"
                f"Свободно: <b>{free_pct:.1f}%</b> ({free_gb:.2f} ГБ)\n"
                f"Режим обслуживания снят."
            )
        _last_alert_level = 0


async def janitor_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        deleted, orphan = await asyncio.to_thread(cleanup_media_dirs)
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
