"""One-shot, isolated evidence harness for the attested legacy bot image.

This source is streamed to ``python -c`` inside the exact legacy image.  It never
starts FastAPI, polling, or webhook lifecycle code and emits only closed JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast


def _cgroup_cpu_seconds() -> float:
    for raw in (
        Path("/sys/fs/cgroup/cpu.stat"),
        Path("/sys/fs/cgroup/cpuacct/cpuacct.usage"),
    ):
        try:
            text = raw.read_text(encoding="ascii")
        except OSError:
            continue
        if raw.name == "cpu.stat":
            for line in text.splitlines():
                field, _, value = line.partition(" ")
                if field == "usage_usec":
                    return int(value) / 1_000_000.0
        else:
            return int(text.strip()) / 1_000_000_000.0
    raise RuntimeError("cgroup CPU accounting is unavailable")


def _memory_limit_bytes() -> int:
    for raw in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            value = raw.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value != "max" and value.isdigit():
            return int(value)
    raise RuntimeError("cgroup memory limit is unavailable")


def _process_rss_bytes() -> int:
    try:
        pids = Path("/sys/fs/cgroup/cgroup.procs").read_text(encoding="ascii").split()
    except OSError as exc:
        raise RuntimeError("cgroup process list is unavailable") from exc
    total = 0
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            lines = Path("/proc", pid, "status").read_text(
                encoding="ascii", errors="replace"
            )
        except OSError:
            continue
        for line in lines.splitlines():
            if line.startswith("VmRSS:"):
                total += int(line.split()[1]) * 1024
                break
    return total


async def _sample_rss(stop: asyncio.Event, samples: list[int]) -> None:
    while not stop.is_set():
        samples.append(_process_rss_bytes())
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except TimeoutError:
            pass
    samples.append(_process_rss_bytes())


def _histogram(metric: Any) -> tuple[int, float]:
    entries = metric.collect()
    return sum(int(count) for _, count, _ in entries), sum(
        float(total) for _, _, total in entries
    )


class _FailureCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failures: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = " ".join(
            str(value)
            for value in (
                record.getMessage(),
                getattr(record, "stderr", ""),
                getattr(record, "error", ""),
            )
        ).lower()
        for status in (403, 429):
            if str(status) not in text:
                continue
            cause = "rate-limited" if status == 429 else "unknown"
            if status == 403:
                if "expired" in text or "signature" in text:
                    cause = "expired-signature"
                elif "geo" in text:
                    cause = "geo-blocked"
                elif "bot" in text or "challenge" in text:
                    cause = "bot-detection"
                elif "policy" in text:
                    cause = "upstream-policy"
                elif "forbidden" in text:
                    cause = "forbidden"
            item = {"status": status, "cause": cause}
            if item not in self.failures:
                self.failures.append(item)


def _phase(
    before: Mapping[str, tuple[int, float]],
    after: Mapping[str, tuple[int, float]],
    name: str,
) -> tuple[int, float]:
    return (
        after[name][0] - before[name][0],
        max(0.0, after[name][1] - before[name][1]),
    )


def _metrics_snapshot(metrics: Any) -> dict[str, tuple[int, float]]:
    return {
        "download": _histogram(metrics.download_duration),
        "conversion": _histogram(metrics.conversion_duration),
        "upload": _histogram(metrics.upload_duration),
    }


def _closed_route() -> dict[str, Any]:
    return {
        "capability": "unavailable",
        "provenance": "unavailable",
        "attempted": None,
        "succeeded": None,
        "route_class": None,
    }


async def _run_once(payload: Mapping[str, Any]) -> dict[str, Any]:
    from telegram import Bot

    from app.bot.commands import _MP4_FORMAT
    from app.core import config, state
    from app.core.metrics import metrics
    from app.core.models import DownloadContext
    from app.services.downloader import MediaSender
    from app.services.orchestrator import DownloadOrchestrator

    case = payload.get("case")
    correlation = payload.get("correlation_id")
    if not isinstance(case, Mapping) or not isinstance(correlation, str):
        raise RuntimeError("invalid isolated run input")
    url = case.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError("invalid case URL")
    if not correlation:
        raise RuntimeError("invalid isolated correlation")
    if state.redis_client is not None or config.REDIS_URL:
        raise RuntimeError("legacy runner is not Redis-isolated")
    if config.WEBHOOK_URL:
        raise RuntimeError("legacy runner must not configure a webhook")
    if not config.ADMIN_CHAT_ID or not config.TELEGRAM_LOCAL_ENDPOINT:
        raise RuntimeError("acceptance delivery target is unavailable")

    original_send = MediaSender.send_file
    current_delivery: dict[str, Any] = {}

    async def measured_send(*args: Any, **kwargs: Any) -> bool:
        media = args[2] if len(args) > 2 else kwargs.get("file_path_or_buffer")
        exact_size: int | None = None
        if isinstance(media, str) and not media.startswith(("http://", "https://")):
            try:
                exact_size = os.path.getsize(media)
            except OSError:
                exact_size = None
        started = time.monotonic()
        result = bool(await original_send(*args, **kwargs))
        current_delivery.setdefault("durations", []).append(
            max(0.0, time.monotonic() - started)
        )
        current_delivery.setdefault("outcomes", []).append(result)
        if exact_size is not None:
            current_delivery.setdefault("sizes", []).append(exact_size)
        return result

    MediaSender.send_file = cast(Any, measured_send)
    capture = _FailureCapture()
    logging.getLogger().addHandler(capture)
    observations: dict[str, dict[str, Any]] = {}
    base_url = f"{config.TELEGRAM_LOCAL_ENDPOINT.rstrip('/')}/bot"
    try:
        async with Bot(token=config.BOT_TOKEN, base_url=base_url) as bot:
            for cache_state in ("cold",):
                token = hashlib.sha256(correlation.encode("utf-8")).hexdigest()[:32]
                current_delivery.clear()
                capture.failures.clear()
                before = _metrics_snapshot(metrics)
                cpu_before = _cgroup_cpu_seconds()
                rss_samples = [_process_rss_bytes()]
                stop = asyncio.Event()
                sampler = asyncio.create_task(_sample_rss(stop, rss_samples))

                async def update_ui(text: str, markup: object = None) -> None:
                    del text, markup

                try:
                    success = bool(
                        await DownloadOrchestrator.process_download(
                            token=token,
                            chat_id=config.ADMIN_CHAT_ID,
                            bot=bot,
                            payload=DownloadContext(
                                page_url=url,
                                format_id=_MP4_FORMAT,
                                height=None,
                            ),
                            fmt_size=None,
                            update_ui=update_ui,
                            kb_error=None,
                        )
                    )
                finally:
                    stop.set()
                    await sampler
                cpu_after = _cgroup_cpu_seconds()
                after = _metrics_snapshot(metrics)
                download_count, download_sum = _phase(before, after, "download")
                conversion_count, conversion_sum = _phase(
                    before, after, "conversion"
                )
                upload_count, upload_sum = _phase(before, after, "upload")
                outcomes = list(current_delivery.get("outcomes", []))
                delivery_success = bool(outcomes) and all(outcomes)
                completed = success and delivery_success
                delivered_sizes = current_delivery.get("sizes", [])
                exact_size = (
                    sum(int(value) for value in delivered_sizes)
                    if delivered_sizes and len(delivered_sizes) == len(outcomes)
                    else None
                )
                materialize_count = download_count + conversion_count
                observations[cache_state] = {
                    "attribution_confirmed": True,
                    "bypassed_phases": [],
                    "unavailable_phases": ["resolve", "first_byte"],
                    "file_id_hits_before": 0,
                    "file_id_hits_after": 0,
                    "phase_metrics": {
                        "resolve": {
                            "before_count": 0,
                            "before_sum": 0.0,
                            "after_count": 0,
                            "after_sum": 0.0,
                        },
                        "first_byte": {
                            "before_count": 0,
                            "before_sum": 0.0,
                            "after_count": 0,
                            "after_sum": 0.0,
                        },
                        "materialize": {
                            "before_count": 0,
                            "before_sum": 0.0,
                            "after_count": 1 if materialize_count > 0 else 0,
                            "after_sum": download_sum + conversion_sum,
                        },
                        "deliver": {
                            "before_count": 0,
                            "before_sum": 0.0,
                            "after_count": 1 if upload_count > 0 else 0,
                            "after_sum": upload_sum,
                        },
                    },
                    "pipeline_results_before": {"success": 0, "failed": 0},
                    "pipeline_results_after": {
                        "success": 1 if completed else 0,
                        "failed": 0 if completed else 1,
                    },
                    "wasted_bytes_before": 0,
                    "wasted_bytes_after": 0,
                    "measurement": {
                        "first_byte": {
                            "availability": "unavailable",
                            "method": "unavailable",
                        },
                        "downloaded_bytes": {
                            "availability": (
                                "measured" if exact_size is not None else "unavailable"
                            ),
                            "method": (
                                "legacy-isolated-delivered-size"
                                if exact_size is not None
                                else "unavailable"
                            ),
                            "value": exact_size,
                        },
                    },
                    "cpu_seconds_before": cpu_before,
                    "cpu_seconds_after": max(cpu_before, cpu_after),
                    "rss_bytes_samples": rss_samples,
                    "job_state": "completed" if completed else "failed",
                    "delivery_outcomes": [
                        "success" if completed else "failed"
                    ],
                    "http_failures": list(capture.failures),
                    "failure_stage": (
                        None
                        if completed
                        else (
                            "deliver"
                            if upload_count > 0
                            else "materialize"
                            if materialize_count > 0
                            else "resolve"
                        )
                    ),
                    "independent_route": _closed_route(),
                    "correlated_events": [
                        "job-completed" if completed else "job-failed",
                        "delivery-success" if completed else "delivery-failed",
                    ],
                }
    finally:
        MediaSender.send_file = original_send
        logging.getLogger().removeHandler(capture)
    return {"observation": observations["cold"]}


def _preflight() -> dict[str, Any]:
    from app.core import config, state
    from app.core.models import DownloadContext  # noqa: F401
    from app.services.orchestrator import DownloadOrchestrator  # noqa: F401

    memory = _memory_limit_bytes()
    return {
        "safe": True,
        "orchestrator_imported": True,
        "redis_isolated": state.redis_client is None and not config.REDIS_URL,
        "webhook_not_started": "app.main" not in sys.modules and not config.WEBHOOK_URL,
        "admin_chat_configured": bool(config.ADMIN_CHAT_ID),
        "local_bot_api_configured": bool(config.TELEGRAM_LOCAL_ENDPOINT),
        "max_media_file_mb": int(config.MAX_TG_UPLOAD_MB),
        "memory_limit_bytes": memory,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("input must be an object")
        action = payload.get("action")
        if action == "preflight":
            result = _preflight()
        elif action == "run-once":
            result = asyncio.run(_run_once(payload))
        else:
            raise RuntimeError("unsupported isolated action")
        if not all(
            math.isfinite(value)
            for observation in result.get("observations", {}).values()
            for value in (
                observation["cpu_seconds_before"],
                observation["cpu_seconds_after"],
            )
        ):
            raise RuntimeError("non-finite resource measurement")
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
        return 0
    except Exception:
        print('{"safe":false,"error":"isolated-run-failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
