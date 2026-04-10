"""Background task: periodically self-updates yt-dlp.

Uses the same stop_event pattern as janitor_loop.
Controlled by YTDLP_UPDATE_INTERVAL_HOURS env var (default: 24h).
On success, logs old/new version and increments the Prometheus counter.
On failure, logs a WARNING — never raises / never crashes the loop.
"""

import asyncio
import logging
import sys

from app.core.config import YTDLP_UPDATE_INTERVAL_HOURS

logger = logging.getLogger("app.tasks.auto_updater")


async def _get_ytdlp_version() -> str:
    """Return the current yt-dlp version string, or 'unknown'."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yt_dlp",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return stdout.decode().strip() if stdout else "unknown"
    except Exception as exc:
        logger.debug("Could not read yt-dlp version: %s", exc)
        return "unknown"


async def _run_update() -> tuple[str, str]:
    """Execute `yt-dlp -U` and return (version_before, version_after).

    Both values are the raw version strings (e.g. '2026.03.03').
    """
    version_before = await _get_ytdlp_version()

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yt_dlp",
            "-U",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        combined = (stdout + stderr).decode(errors="ignore")

        if proc.returncode not in (0, 1):
            # returncode 1 can mean "already up-to-date" in some yt-dlp builds
            logger.warning(
                "yt-dlp -U exited with code %s: %s",
                proc.returncode,
                combined[-300:],
            )
    except asyncio.TimeoutError:
        logger.warning("yt-dlp -U timed out after 120 s")
        return version_before, version_before
    except Exception as exc:
        logger.warning("yt-dlp -U failed: %s", exc)
        return version_before, version_before

    version_after = await _get_ytdlp_version()
    return version_before, version_after


async def auto_updater_loop(stop_event: asyncio.Event) -> None:
    """Run yt-dlp self-update on startup and then every YTDLP_UPDATE_INTERVAL_HOURS."""
    logger.info(
        "Auto-updater started (interval: %dh)", YTDLP_UPDATE_INTERVAL_HOURS
    )

    # Run once immediately on startup so the first check is fresh
    await _do_update()

    while not stop_event.is_set():
        interval_seconds = YTDLP_UPDATE_INTERVAL_HOURS * 3600
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        await _do_update()

    logger.info("Auto-updater stopped")


async def _do_update() -> None:
    """Single update attempt with structured logging."""
    logger.info("Auto-updater: checking for yt-dlp updates...")
    try:
        before, after = await _run_update()

        if before != after and after != "unknown":
            logger.info(
                "yt-dlp updated: %s → %s",
                before,
                after,
                extra={"op": "ytdlp_update", "old": before, "new": after},
            )
            # Increment Prometheus counter if metrics are available
            try:
                from app.core.metrics import metrics as _m

                _m.ytdlp_updates_total.inc()
            except Exception:
                pass
        else:
            logger.info(
                "yt-dlp is up-to-date: %s",
                after,
                extra={"op": "ytdlp_update", "version": after},
            )
    except Exception as exc:
        # Never crash the loop — just warn
        logger.warning("Auto-updater: unexpected error: %s", exc, exc_info=True)
